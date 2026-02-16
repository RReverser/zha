"""Alarm control panels on Zigbee Home Automation networks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import functools
import logging
from typing import TYPE_CHECKING, Any, Final

from zigpy.profiles import zha
from zigpy.zcl.clusters.security import IasAce
from zigpy.zcl.foundation import CommandSchema

from zha.application import Platform
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
from zha.application.platforms.cluster_config import entity_cluster_configs_from_refs
from zha.application.platforms.cluster_names import (
    ARGS,
    CLUSTER_IAS_ACE,
    CLUSTER_ID,
    COMMAND,
    PARAMS,
    UNIQUE_ID,
)
from zha.zigbee.cluster_events import ClusterCommandEvent

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint

_LOGGER = logging.getLogger(__name__)
SIGNAL_ARMED_STATE_CHANGED: Final[str] = "zha_armed_state_changed"
SIGNAL_ALARM_TRIGGERED: Final[str] = "zha_armed_triggered"


class _IasAceEntityProtocol:
    """Entity-side IAS ACE protocol handling."""

    def __init__(self, entity: AlarmControlPanel, cluster: Any) -> None:
        self._entity = entity
        self._cluster = cluster
        self._endpoint = entity.endpoint

        self.command_map: dict[int, Callable[..., Any]] = {
            IasAce.ServerCommandDefs.arm.id: self.arm,
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
        self.arm_map: dict[IasAce.ArmMode, Callable[[str | None], Any]] = {
            IasAce.ArmMode.Disarm: self._disarm,
            IasAce.ArmMode.Arm_All_Zones: self._arm_away,
            IasAce.ArmMode.Arm_Day_Home_Only: self._arm_day,
            IasAce.ArmMode.Arm_Night_Sleep_Only: self._arm_night,
        }
        self.armed_state: IasAce.PanelStatus = IasAce.PanelStatus.Panel_Disarmed
        self.invalid_tries: int = 0

        # Configured by the alarm control panel entity.
        self.panel_code: str = "1234"
        self.code_required_arm_actions: bool = False
        self.max_invalid_tries: int = 3

        self.alarm_status: IasAce.AlarmStatus = IasAce.AlarmStatus.No_Alarm

    def _emit_zha_event(self, command: str, arg: list | dict | CommandSchema) -> None:
        """Emit a ZHA event matching the legacy cluster payload shape."""
        args: list | dict
        if isinstance(arg, CommandSchema):
            args = [a for a in arg if a is not None]
            params = arg.as_dict()
        elif isinstance(arg, (list, dict)):
            args = arg
            params = {}
        else:
            raise TypeError(f"Unexpected emit_zha_event {command!r} argument: {arg!r}")

        self._endpoint.emit_zha_event(
            {
                UNIQUE_ID: self._cluster.unique_id,
                CLUSTER_ID: self._cluster.cluster_id,
                COMMAND: command,
                ARGS: args,
                PARAMS: params,
            }
        )

    def handle_cluster_command(self, event: ClusterCommandEvent) -> None:
        """Handle commands received from IAS ACE clients."""
        command_id = event.command_id
        if command_id not in self.command_map:
            return
        self._entity.debug(
            "received command %s",
            self._cluster.server_commands[command_id].name,
        )
        self.command_map[command_id](*event.args)

    def arm(self, arm_mode: int, code: str | None, zone_id: int) -> None:
        """Handle the IAS ACE arm command."""
        mode = IasAce.ArmMode(arm_mode)

        self._emit_zha_event(
            IasAce.ServerCommandDefs.arm.name,
            {
                "arm_mode": mode.value,
                "arm_mode_description": mode.name,
                "code": code,
                "zone_id": zone_id,
            },
        )

        zigbee_reply = self.arm_map[mode](code)
        self._endpoint.device.gateway.async_create_task(zigbee_reply)

        if self.invalid_tries >= self.max_invalid_tries:
            self.alarm_status = IasAce.AlarmStatus.Emergency
            self.armed_state = IasAce.PanelStatus.In_Alarm
            self._emit_zha_event(
                f"{self._cluster.unique_id}_{SIGNAL_ALARM_TRIGGERED}", []
            )
        else:
            self._emit_zha_event(
                f"{self._cluster.unique_id}_{SIGNAL_ARMED_STATE_CHANGED}", []
            )
        self._emit_panel_status_changed()

    def _disarm(self, code: str | None):
        """Test the code and disarm the panel if the code is correct."""
        if (
            code != self.panel_code
            and self.armed_state != IasAce.PanelStatus.Panel_Disarmed
        ):
            self._entity.debug("Invalid code supplied to IAS ACE")
            self.invalid_tries += 1
            zigbee_reply = self._cluster.arm_response(
                IasAce.ArmNotification.Invalid_Arm_Disarm_Code
            )
        else:
            self.invalid_tries = 0
            if (
                self.armed_state == IasAce.PanelStatus.Panel_Disarmed
                and self.alarm_status == IasAce.AlarmStatus.No_Alarm
            ):
                self._entity.debug("IAS ACE already disarmed")
                zigbee_reply = self._cluster.arm_response(
                    IasAce.ArmNotification.Already_Disarmed
                )
            else:
                self._entity.debug("Disarming all IAS ACE zones")
                zigbee_reply = self._cluster.arm_response(
                    IasAce.ArmNotification.All_Zones_Disarmed
                )

            self.armed_state = IasAce.PanelStatus.Panel_Disarmed
            self.alarm_status = IasAce.AlarmStatus.No_Alarm
        return zigbee_reply

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
            self._entity.debug("Invalid code supplied to IAS ACE")
            zigbee_reply = self._cluster.arm_response(
                IasAce.ArmNotification.Invalid_Arm_Disarm_Code
            )
        else:
            self._entity.debug("Arming all IAS ACE zones")
            self.armed_state = panel_status
            zigbee_reply = self._cluster.arm_response(armed_type)
        return zigbee_reply

    def _bypass(self, zone_list: list[int], code: str | None) -> None:
        """Handle the IAS ACE bypass command."""
        self._emit_zha_event(
            IasAce.ServerCommandDefs.bypass.name,
            {"zone_list": zone_list, "code": code},
        )

    def _emergency(self) -> None:
        """Handle the IAS ACE emergency command."""
        self._set_alarm(IasAce.AlarmStatus.Emergency)

    def _fire(self) -> None:
        """Handle the IAS ACE fire command."""
        self._set_alarm(IasAce.AlarmStatus.Fire)

    def panic(self) -> None:
        """Handle the IAS ACE panic command."""
        self._set_alarm(IasAce.AlarmStatus.Emergency_Panic)

    def _set_alarm(self, status: IasAce.AlarmStatus) -> None:
        """Set the specified alarm status."""
        self.alarm_status = status
        self.armed_state = IasAce.PanelStatus.In_Alarm
        self._emit_panel_status_changed()

    def _get_zone_id_map(self) -> None:
        """Handle the IAS ACE zone id map command."""

    def _get_zone_info(self, zone_id: int) -> None:
        """Handle the IAS ACE zone info command."""

    def _send_panel_status_response(self) -> None:
        """Handle the IAS ACE panel status response command."""
        response = self._cluster.panel_status_response(
            self.armed_state,
            0x00,
            IasAce.AudibleNotification.Default_Sound,
            self.alarm_status,
        )
        self._endpoint.device.gateway.async_create_task(response)

    def _emit_panel_status_changed(self) -> None:
        """Handle IAS ACE panel status changed command."""
        response = self._cluster.panel_status_changed(
            self.armed_state,
            0x00,
            IasAce.AudibleNotification.Default_Sound,
            self.alarm_status,
        )
        self._endpoint.device.gateway.async_create_task(response)
        self._entity.maybe_emit_state_changed_event()

    def _get_bypassed_zone_list(self) -> None:
        """Handle IAS ACE bypassed zone list command."""

    def _get_zone_status(
        self,
        starting_zone_id: int,
        max_zone_ids: int,
        zone_status_mask_flag: int,
        zone_status_mask: int,
    ) -> None:
        """Handle IAS ACE zone status command."""


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
        client_clusters=frozenset({CLUSTER_IAS_ACE}),
    )
    _entity_cluster_configs = entity_cluster_configs_from_refs(
        (CLUSTER_IAS_ACE, True),
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

        alarm_options = device.gateway.config.config.alarm_control_panel_options
        self._ias_ace_cluster = self.get_cluster(CLUSTER_IAS_ACE)
        self._ias_ace_protocol = _IasAceEntityProtocol(
            entity=self,
            cluster=self._ias_ace_cluster,
        )
        self._ias_ace_protocol.panel_code = alarm_options.master_code
        self._ias_ace_protocol.code_required_arm_actions = (
            alarm_options.arm_requires_code
        )
        self._ias_ace_protocol.max_invalid_tries = alarm_options.failed_tries

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.subscribe_cluster_commands(
            CLUSTER_IAS_ACE,
            self._ias_ace_protocol.handle_cluster_command,
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
        response["state"] = IAS_ACE_STATE_MAP.get(
            self._ias_ace_protocol.armed_state, AlarmState.UNKNOWN
        )
        return response

    @property
    def code_arm_required(self) -> bool:
        """Whether the code is required for arm actions."""
        return self._ias_ace_protocol.code_required_arm_actions

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
        self._ias_ace_protocol.arm(IasAce.ArmMode.Disarm, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_home(self, code: str | None = None) -> None:
        """Send arm home command."""
        self._ias_ace_protocol.arm(IasAce.ArmMode.Arm_Day_Home_Only, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_away(self, code: str | None = None) -> None:
        """Send arm away command."""
        self._ias_ace_protocol.arm(IasAce.ArmMode.Arm_All_Zones, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_arm_night(self, code: str | None = None) -> None:
        """Send arm night command."""
        self._ias_ace_protocol.arm(IasAce.ArmMode.Arm_Night_Sleep_Only, code, 0)
        self.maybe_emit_state_changed_event()

    async def async_alarm_trigger(self, code: str | None = None) -> None:  # pylint: disable=unused-argument
        """Send alarm trigger command."""
        self._ias_ace_protocol.panic()
        self.maybe_emit_state_changed_event()
