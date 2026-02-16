"""Alarm control panels on Zigbee Home Automation networks."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import logging
from typing import TYPE_CHECKING, Any

from zigpy.profiles import zha
from zigpy.zcl import Cluster
from zigpy.zcl.clusters.security import IasAce

from zha.application import Platform
from zha.application.helpers import safe_cluster_command
from zha.application.platforms import (
    BaseEntityInfo,
    ClusterMatch,
    PlatformEntity,
    register_entity,
)
from zha.application.platforms.alarm_control_panel.const import (
    IAS_ACE_STATE_MAP,
    SUPPORT_ALARM_ARM_AWAY,
    SUPPORT_ALARM_ARM_HOME,
    SUPPORT_ALARM_ARM_NIGHT,
    SUPPORT_ALARM_TRIGGER,
    AlarmState,
    CodeFormat,
)
from zha.zigbee.const import (
    ARGS,
    CLUSTER_IAS_ACE,
    CLUSTER_ID,
    COMMAND,
    PARAMS,
    UNIQUE_ID,
)

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint

_LOGGER = logging.getLogger(__name__)
SIGNAL_ARMED_STATE_CHANGED = "zha_armed_state_changed"
SIGNAL_ALARM_TRIGGERED = "zha_armed_triggered"


@dataclass(frozen=True, kw_only=True)
class AlarmControlPanelEntityInfo(BaseEntityInfo):
    """Alarm control panel entity info."""

    code_arm_required: bool
    code_format: CodeFormat
    supported_features: int
    translation_key: str


@register_entity(IasAce.cluster_id)
class AlarmControlPanel(PlatformEntity):
    """Entity for ZHA alarm control devices."""

    _attr_translation_key: str = "alarm_control_panel"
    PLATFORM = Platform.ALARM_CONTROL_PANEL

    _cluster_match = ClusterMatch(
        out_clusters=frozenset({CLUSTER_IAS_ACE}),
    )

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs,
    ) -> None:
        """Initialize the ZHA alarm control device."""
        legacy_discovery_unique_id = (
            f"{endpoint.device.ieee}-{endpoint.id}-{int(IasAce.cluster_id)}"
            if (
                endpoint.zigpy_endpoint.device_type
                == zha.DeviceType.IAS_ANCILLARY_CONTROL
            )
            else f"{endpoint.device.ieee}-{endpoint.id}-{int(IasAce.cluster_id)}"
        )
        super().__init__(
            clusters,
            endpoint,
            device,
            **kwargs,
            legacy_discovery_unique_id=legacy_discovery_unique_id,
        )

        self._ias_ace_cluster: Cluster = clusters[0]
        self._cluster_unique_id = (
            f"{endpoint.unique_id.replace('-', ':')}:0x{IasAce.cluster_id:04x}_CLIENT"
        )

        alarm_options = device.gateway.config.config.alarm_control_panel_options
        self.panel_code: str = alarm_options.master_code
        self.code_required_arm_actions: bool = alarm_options.arm_requires_code
        self.max_invalid_tries: int = alarm_options.failed_tries

        self._armed_state: IasAce.PanelStatus = IasAce.PanelStatus.Panel_Disarmed
        self._invalid_tries: int = 0
        self._alarm_status: IasAce.AlarmStatus = IasAce.AlarmStatus.No_Alarm

        self._command_map: dict[int, Any] = {
            IasAce.ServerCommandDefs.arm.id: self._arm,
            IasAce.ServerCommandDefs.bypass.id: self._bypass,
            IasAce.ServerCommandDefs.emergency.id: self._emergency,
            IasAce.ServerCommandDefs.fire.id: self._fire,
            IasAce.ServerCommandDefs.panic.id: self.panic,
            IasAce.ServerCommandDefs.get_zone_id_map.id: self._get_zone_id_map,
            IasAce.ServerCommandDefs.get_zone_info.id: self._get_zone_info,
            IasAce.ServerCommandDefs.get_panel_status.id: self._send_panel_status_response,
            IasAce.ServerCommandDefs.get_bypassed_zone_list.id: self._get_bypassed_zone_list,
            IasAce.ServerCommandDefs.get_zone_status.id: self._get_zone_status,
        }
        self._arm_map: dict[IasAce.ArmMode, Any] = {
            IasAce.ArmMode.Disarm: self._disarm,
            IasAce.ArmMode.Arm_All_Zones: self._arm_away,
            IasAce.ArmMode.Arm_Day_Home_Only: self._arm_day,
            IasAce.ArmMode.Arm_Night_Sleep_Only: self._arm_night,
        }

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self._ias_ace_cluster.add_context_listener(self)
        self._on_remove_callbacks.append(self._remove_cluster_listener)

    def _remove_cluster_listener(self) -> None:
        """Remove this entity as cluster listener."""
        self._ias_ace_cluster.remove_listener(self)

    def cluster_command(
        self,
        cluster: Cluster,
        tsn: int,  # pylint: disable=unused-argument
        command_id: int,
        args: list[Any],
    ) -> None:
        """Handle cluster commands for IAS ACE."""
        if cluster is not self._ias_ace_cluster:
            return
        if command_id in self._command_map:
            self._command_map[command_id](*args)

    def _emit_cluster_event(self, command: str, args: list | dict) -> None:
        """Emit zha_event payload compatible with legacy cluster flow."""
        self.endpoint.emit_zha_event(
            {
                UNIQUE_ID: self._cluster_unique_id,
                CLUSTER_ID: self._ias_ace_cluster.cluster_id,
                COMMAND: command,
                ARGS: args,
                PARAMS: {},
            }
        )

    @functools.cached_property
    def info_object(self) -> AlarmControlPanelEntityInfo:
        """Return a representation of the alarm control panel."""
        return AlarmControlPanelEntityInfo(
            **super().info_object.__dict__,
            code_arm_required=self.code_arm_required,
            code_format=self.code_format,
            supported_features=self.supported_features,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Get the state of the alarm control panel."""
        response = super().state
        response["state"] = IAS_ACE_STATE_MAP.get(self._armed_state, AlarmState.UNKNOWN)
        return response

    @property
    def code_arm_required(self) -> bool:
        """Whether the code is required for arm actions."""
        return self.code_required_arm_actions

    @functools.cached_property
    def code_format(self) -> CodeFormat:
        """Code format or None if no code is required."""
        return CodeFormat.NUMBER

    @functools.cached_property
    def supported_features(self) -> int:
        """Return the list of supported features."""
        return (
            SUPPORT_ALARM_ARM_HOME
            | SUPPORT_ALARM_ARM_AWAY
            | SUPPORT_ALARM_ARM_NIGHT
            | SUPPORT_ALARM_TRIGGER
        )

    async def async_alarm_disarm(self, code: str | None = None) -> None:
        """Send disarm command."""
        self._arm(IasAce.ArmMode.Disarm, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        self._arm(IasAce.ArmMode.Arm_Day_Home_Only, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        self._arm(IasAce.ArmMode.Arm_All_Zones, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        self._arm(IasAce.ArmMode.Arm_Night_Sleep_Only, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_trigger(self, code: str | None = None) -> None:  # pylint: disable=unused-argument
        """Send alarm trigger command."""
        self.panic()
        self.maybe_emit_state_changed_event()

    def _arm(self, arm_mode: int, code: str | None, zone_id: int) -> None:
        """Handle IAS ACE arm command."""
        mode = IasAce.ArmMode(arm_mode)
        self._emit_cluster_event(
            IasAce.ServerCommandDefs.arm.name,
            {
                "arm_mode": mode.value,
                "arm_mode_description": mode.name,
                "code": code,
                "zone_id": zone_id,
            },
        )

        zigbee_reply = self._arm_map[mode](code)
        self.device.gateway.async_create_task(zigbee_reply)

        if self._invalid_tries >= self.max_invalid_tries:
            self._alarm_status = IasAce.AlarmStatus.Emergency
            self._armed_state = IasAce.PanelStatus.In_Alarm
            self._emit_cluster_event(
                f"{self._cluster_unique_id}_{SIGNAL_ALARM_TRIGGERED}", []
            )
        else:
            self._emit_cluster_event(
                f"{self._cluster_unique_id}_{SIGNAL_ARMED_STATE_CHANGED}", []
            )
        self._emit_panel_status_changed()

    def _disarm(self, code: str | None):
        """Test code and disarm the panel if code is correct."""
        if (
            code != self.panel_code
            and self._armed_state != IasAce.PanelStatus.Panel_Disarmed
        ):
            self.debug("Invalid code supplied to IAS ACE")
            self._invalid_tries += 1
            return safe_cluster_command(
                self._ias_ace_cluster,
                "arm_response",
                IasAce.ArmNotification.Invalid_Arm_Disarm_Code,
            )

        self._invalid_tries = 0
        if (
            self._armed_state == IasAce.PanelStatus.Panel_Disarmed
            and self._alarm_status == IasAce.AlarmStatus.No_Alarm
        ):
            self.debug("IAS ACE already disarmed")
            response = IasAce.ArmNotification.Already_Disarmed
        else:
            self.debug("Disarming all IAS ACE zones")
            response = IasAce.ArmNotification.All_Zones_Disarmed

        self._armed_state = IasAce.PanelStatus.Panel_Disarmed
        self._alarm_status = IasAce.AlarmStatus.No_Alarm
        return safe_cluster_command(self._ias_ace_cluster, "arm_response", response)

    def _arm_day(self, code: str | None):
        """Arm the panel for day/home zones."""
        return self._handle_arm(
            code,
            IasAce.PanelStatus.Armed_Stay,
            IasAce.ArmNotification.Only_Day_Home_Zones_Armed,
        )

    def _arm_night(self, code: str | None):
        """Arm the panel for night/sleep zones."""
        return self._handle_arm(
            code,
            IasAce.PanelStatus.Armed_Night,
            IasAce.ArmNotification.Only_Night_Sleep_Zones_Armed,
        )

    def _arm_away(self, code: str | None):
        """Arm the panel for away mode."""
        return self._handle_arm(
            code,
            IasAce.PanelStatus.Armed_Away,
            IasAce.ArmNotification.All_Zones_Armed,
        )

    def _handle_arm(
        self,
        code: str | None,
        panel_status: IasAce.PanelStatus,
        armed_type: IasAce.ArmNotification,
    ):
        """Arm the panel with the specified statuses."""
        if self.code_required_arm_actions and code != self.panel_code:
            self.debug("Invalid code supplied to IAS ACE")
            return safe_cluster_command(
                self._ias_ace_cluster,
                "arm_response",
                IasAce.ArmNotification.Invalid_Arm_Disarm_Code,
            )

        self.debug("Arming all IAS ACE zones")
        self._armed_state = panel_status
        return safe_cluster_command(self._ias_ace_cluster, "arm_response", armed_type)

    def _bypass(self, zone_list, code) -> None:
        """Handle IAS ACE bypass command."""
        self._emit_cluster_event(
            IasAce.ServerCommandDefs.bypass.name,
            {"zone_list": zone_list, "code": code},
        )

    def _emergency(self) -> None:
        """Handle IAS ACE emergency command."""
        self._set_alarm(IasAce.AlarmStatus.Emergency)

    def _fire(self) -> None:
        """Handle IAS ACE fire command."""
        self._set_alarm(IasAce.AlarmStatus.Fire)

    def panic(self) -> None:
        """Handle IAS ACE panic command."""
        self._set_alarm(IasAce.AlarmStatus.Emergency_Panic)

    def _set_alarm(self, status: IasAce.AlarmStatus) -> None:
        """Set the specified alarm status."""
        self._alarm_status = status
        self._armed_state = IasAce.PanelStatus.In_Alarm
        self._emit_panel_status_changed()

    def _get_zone_id_map(self):
        """Handle IAS ACE zone id map command."""

    def _get_zone_info(self, zone_id):
        """Handle IAS ACE zone info command."""

    def _send_panel_status_response(self) -> None:
        """Handle IAS ACE panel status response command."""
        response = safe_cluster_command(
            self._ias_ace_cluster,
            "panel_status_response",
            self._armed_state,
            0x00,
            IasAce.AudibleNotification.Default_Sound,
            self._alarm_status,
        )
        self.device.gateway.async_create_task(response)

    def _emit_panel_status_changed(self) -> None:
        """Handle IAS ACE panel status changed command."""
        response = safe_cluster_command(
            self._ias_ace_cluster,
            "panel_status_changed",
            self._armed_state,
            0x00,
            IasAce.AudibleNotification.Default_Sound,
            self._alarm_status,
        )
        self.device.gateway.async_create_task(response)
        self.maybe_emit_state_changed_event()

    def _get_bypassed_zone_list(self):
        """Handle IAS ACE bypassed zone list command."""

    def _get_zone_status(
        self, starting_zone_id, max_zone_ids, zone_status_mask_flag, zone_status_mask
    ):
        """Handle IAS ACE zone status command."""
