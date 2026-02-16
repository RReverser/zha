"""Binary sensors on Zigbee Home Automation networks."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import functools
import logging
from typing import TYPE_CHECKING, Any

from zhaquirks.quirk_ids import DANFOSS_ALLY_THERMOSTAT
from zigpy.profiles import zha, zll
from zigpy.quirks.v2 import BinarySensorMetadata
import zigpy.types as t
from zigpy.zcl.clusters.general import BinaryInput as BinaryInputCluster, OnOff
from zigpy.zcl.clusters.hvac import Thermostat
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.clusters.security import IasZone

from zha.application import Platform
from zha.application.platforms import (
    BaseEntityInfo,
    ClusterMatch,
    EntityCategory,
    PlatformEntity,
    PlatformFeatureGroup,
    register_entity,
)
from zha.application.platforms.binary_sensor.const import (
    IAS_ZONE_CLASS_MAPPING,
    BinarySensorDeviceClass,
)
from zha.application.platforms.cluster_names import (
    AQARA_OPPLE_CLUSTER,
    ARGS,
    ATTRIBUTE_ID,
    ATTRIBUTE_NAME,
    ATTRIBUTE_VALUE,
    CLUSTER_ACCELEROMETER,
    CLUSTER_BINARY_INPUT,
    CLUSTER_HUE_OCCUPANCY,
    CLUSTER_ID,
    CLUSTER_OCCUPANCY,
    CLUSTER_ON_OFF,
    CLUSTER_THERMOSTAT,
    CLUSTER_ZONE,
    COMMAND,
    IKEA_AIR_PURIFIER_CLUSTER,
    PARAMS,
    SIGNAL_ATTR_UPDATED,
    SMARTTHINGS_ACCELERATION_CLUSTER,
    TUYA_MANUFACTURER_CLUSTER,
    UNIQUE_ID,
    UNKNOWN,
    VALUE,
)
from zha.application.platforms.helpers import validate_device_class
from zha.zigbee.cluster_events import ClusterAttributeUpdatedEvent, ClusterCommandEvent

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class BinarySensorEntityInfo(BaseEntityInfo):
    """Binary sensor entity info."""

    attribute_name: str
    device_class: BinarySensorDeviceClass | None


class BinarySensor(PlatformEntity):
    """ZHA BinarySensor."""

    _attr_device_class: BinarySensorDeviceClass | None
    _attribute_name: str
    _attribute_converter: Callable[[Any], Any] | None = None
    PLATFORM: Platform = Platform.BINARY_SENSOR

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs,
    ) -> None:
        """Initialize the ZHA binary sensor."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._cluster = self.get_cluster(self.get_primary_cluster_name())
        self._on_off_listener: asyncio.TimerHandle | None = None
        self._state: bool = self.is_on
        self.recompute_capabilities()

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.subscribe_cluster_attribute_updates(
            self.get_primary_cluster_name(),
            self.handle_cluster_attribute_updated,
        )
        self.subscribe_cluster_commands(
            self.get_primary_cluster_name(),
            self.handle_cluster_command,
        )

    def _init_from_quirks_metadata(self, entity_metadata: BinarySensorMetadata) -> None:
        """Init this entity from the quirks metadata."""
        super()._init_from_quirks_metadata(entity_metadata)
        self._attribute_name = entity_metadata.attribute_name
        if entity_metadata.attribute_converter is not None:
            self._attribute_converter = entity_metadata.attribute_converter
        if entity_metadata.device_class is not None:
            self._attr_device_class = validate_device_class(
                BinarySensorDeviceClass,
                entity_metadata.device_class,
                Platform.BINARY_SENSOR.value,
                _LOGGER,
            )

    @functools.cached_property
    def info_object(self) -> BinarySensorEntityInfo:
        """Return a representation of the binary sensor."""
        return BinarySensorEntityInfo(
            **super().info_object.__dict__,
            attribute_name=self._attribute_name,
        )

    @property
    def state(self) -> dict:
        """Return the state of the binary sensor."""
        response = super().state
        response["state"] = self.is_on
        return response

    @property
    def is_on(self) -> bool:
        """Return True if the switch is on based on the state machine."""
        self._state = raw_state = self._cluster.get(self._attribute_name)
        if raw_state is None:
            return False
        if self._attribute_converter:
            return self._attribute_converter(raw_state)
        return self.parse(raw_state)

    def handle_cluster_attribute_updated(
        self, event: ClusterAttributeUpdatedEvent
    ) -> None:
        """Handle attribute updates from the cluster."""
        if self._attribute_name is None or self._attribute_name != event.attribute_name:
            return
        self._state = bool(event.attribute_value)
        if self._cluster.is_client:
            self._emit_zha_event(
                SIGNAL_ATTR_UPDATED,
                {
                    ATTRIBUTE_ID: event.attribute_id,
                    ATTRIBUTE_NAME: event.attribute_name or "Unknown",
                    ATTRIBUTE_VALUE: event.attribute_value,
                    VALUE: event.attribute_value,
                },
            )
        self.maybe_emit_state_changed_event()

    def handle_cluster_command(self, event: ClusterCommandEvent) -> None:
        """Handle inbound cluster commands directly from the cluster proxy."""
        cluster = self._cluster
        if cluster.cluster_id == OnOff.cluster_id and cluster.is_client:
            self._handle_on_off_cluster_command(event.command_id, event.args)
            return

        if cluster.cluster_id == IasZone.cluster_id and cluster.is_server:
            self._handle_ias_zone_cluster_command(event.command_id, event.args)

    def _handle_ias_zone_cluster_command(
        self, command_id: int, args: list[Any]
    ) -> None:
        """Handle IAS Zone commands used by IAS binary sensors."""
        if command_id == IasZone.ClientCommandDefs.status_change_notification.id:
            if not args:
                return
            zone_status = args[0]
            self._cluster.update_attribute(
                IasZone.AttributeDefs.zone_status.id, zone_status
            )
            self.debug("Updated alarm state: %s", zone_status)
        elif command_id == IasZone.ClientCommandDefs.enroll.id:
            self.debug("Enroll requested")
            self._cluster.create_catching_task(
                self._cluster.enroll_response(
                    enroll_response_code=IasZone.EnrollResponse.Success,
                    zone_id=0,
                )
            )

    def _handle_on_off_cluster_command(self, command_id: int, args: list[Any]) -> None:
        """Handle client OnOff server commands (e.g. motion sensor traffic)."""
        command = self._cluster.server_commands.get(command_id)
        if command is None:
            return

        command_name = command.name
        self._emit_zha_event(command_name, args)

        if command_name in (
            OnOff.ServerCommandDefs.off.name,
            OnOff.ServerCommandDefs.off_with_effect.name,
        ):
            self._cluster.update_attribute(OnOff.AttributeDefs.on_off.id, t.Bool.false)
            return

        if command_name in (
            OnOff.ServerCommandDefs.on.name,
            OnOff.ServerCommandDefs.on_with_recall_global_scene.name,
        ):
            self._cluster.update_attribute(OnOff.AttributeDefs.on_off.id, t.Bool.true)
            return

        if command_name == OnOff.ServerCommandDefs.on_with_timed_off.name:
            if len(args) < 2:
                return
            should_accept = args[0]
            on_time = args[1]
            # 0 is always accept; 1 is accept only when already on.
            if should_accept == 0 or (
                should_accept == 1
                and bool(self._cluster.get(OnOff.AttributeDefs.on_off.name))
            ):
                if self._on_off_listener is not None:
                    if self._on_off_listener in self._tracked_handles:
                        self._tracked_handles.remove(self._on_off_listener)
                    self._on_off_listener.cancel()
                    self._on_off_listener = None
                self._cluster.update_attribute(
                    OnOff.AttributeDefs.on_off.id, t.Bool.true
                )
                if on_time > 0:
                    self._on_off_listener = asyncio.get_running_loop().call_later(
                        on_time / 10, self._set_to_off
                    )
                    self._tracked_handles.append(self._on_off_listener)
            return

        if command_name == OnOff.ServerCommandDefs.toggle.name:
            self._cluster.update_attribute(
                OnOff.AttributeDefs.on_off.id,
                not bool(self._cluster.get(OnOff.AttributeDefs.on_off.name)),
            )

    def _set_to_off(self) -> None:
        """Set the client OnOff cluster state to off."""
        if (
            self._on_off_listener is not None
            and self._on_off_listener in self._tracked_handles
        ):
            self._tracked_handles.remove(self._on_off_listener)
        self._on_off_listener = None
        self._cluster.update_attribute(OnOff.AttributeDefs.on_off.id, t.Bool.false)

    def _emit_zha_event(self, command: str, args: list[Any] | dict[str, Any]) -> None:
        """Emit endpoint zha_event payloads matching prior runtime behavior."""
        self.endpoint.emit_zha_event(
            {
                UNIQUE_ID: self._cluster.unique_id,
                CLUSTER_ID: self._cluster.cluster_id,
                COMMAND: command,
                ARGS: args,
                PARAMS: {},
            }
        )

    async def async_update(self) -> None:
        """Attempt to retrieve on off state from the binary sensor."""
        # this is a cached read to get the value for state mgt so there is no double read
        attr_value = await self.get_cluster_attribute_value(
            self._cluster, self._attribute_name
        )
        if attr_value is not None:
            self._state = attr_value
            self.maybe_emit_state_changed_event()

    @staticmethod
    def parse(value: bool | int) -> bool:
        """Parse the raw attribute into a bool state."""
        return bool(value)


@register_entity(SMARTTHINGS_ACCELERATION_CLUSTER)
class Accelerometer(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "acceleration"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.MOVING
    _attr_translation_key: str = "accelerometer"

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_ACCELEROMETER}))

    def handle_cluster_attribute_updated(
        self, event: ClusterAttributeUpdatedEvent
    ) -> None:
        """Mirror legacy acceleration event payloads for all attrs."""
        self._emit_zha_event(
            SIGNAL_ATTR_UPDATED,
            {
                ATTRIBUTE_ID: event.attribute_id,
                ATTRIBUTE_NAME: event.attribute_name or UNKNOWN,
                ATTRIBUTE_VALUE: event.attribute_value,
            },
        )
        super().handle_cluster_attribute_updated(event)


@register_entity(OccupancySensing.cluster_id)
class Occupancy(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "occupancy"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OCCUPANCY
    _attr_primary_weight = 2

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_OCCUPANCY}))


@register_entity(OccupancySensing.cluster_id)
class HueOccupancy(BinarySensor):
    """ZHA Hue occupancy."""

    _attribute_name = "occupancy"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OCCUPANCY
    _attr_primary_weight = 3

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_HUE_OCCUPANCY}))


@register_entity(OnOff.cluster_id)
class Opening(BinarySensor):
    """ZHA OnOff BinarySensor."""

    _attribute_name = "on_off"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OPENING
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        client_clusters=frozenset({CLUSTER_ON_OFF}),
        not_profile_device_types=frozenset(
            {
                (zha.PROFILE_ID, zha.DeviceType.COLOR_CONTROLLER),
                (zha.PROFILE_ID, zha.DeviceType.COLOR_DIMMER_SWITCH),
                (zha.PROFILE_ID, zha.DeviceType.COLOR_SCENE_CONTROLLER),
                (zha.PROFILE_ID, zha.DeviceType.DIMMER_SWITCH),
                (zha.PROFILE_ID, zha.DeviceType.LEVEL_CONTROL_SWITCH),
                (zha.PROFILE_ID, zha.DeviceType.NON_COLOR_CONTROLLER),
                (zha.PROFILE_ID, zha.DeviceType.NON_COLOR_SCENE_CONTROLLER),
                (zha.PROFILE_ID, zha.DeviceType.ON_OFF_SWITCH),
                (zha.PROFILE_ID, zha.DeviceType.ON_OFF_LIGHT_SWITCH),
                (zha.PROFILE_ID, zha.DeviceType.REMOTE_CONTROL),
                (zha.PROFILE_ID, zha.DeviceType.SCENE_SELECTOR),
                (zll.PROFILE_ID, zll.DeviceType.COLOR_CONTROLLER),
                (zll.PROFILE_ID, zll.DeviceType.COLOR_SCENE_CONTROLLER),
                (zll.PROFILE_ID, zll.DeviceType.CONTROL_BRIDGE),
                (zll.PROFILE_ID, zll.DeviceType.CONTROLLER),
                (zll.PROFILE_ID, zll.DeviceType.SCENE_CONTROLLER),
            }
        ),
        feature_priority=(PlatformFeatureGroup.BINARY_SENSOR, 0),
    )


@register_entity(BinaryInputCluster.cluster_id)
class BinaryInputWithDescription(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "present_value"

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_BINARY_INPUT}))

    def recompute_capabilities(self) -> None:
        """Recompute capabilities."""
        super().recompute_capabilities()
        self._attr_fallback_name = self._cluster.get("description")

    def _is_supported(self) -> bool:
        if self._cluster.get("description") is None:
            return False

        return super()._is_supported()


@register_entity(BinaryInputCluster.cluster_id)
class BinaryInput(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "present_value"
    _attr_translation_key: str = "binary_input"

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_BINARY_INPUT}))

    def _is_supported(self) -> bool:
        # Prefer to use the "WithDescription" variant above
        if self._cluster.get("description") is not None:
            return False

        return super()._is_supported()


@register_entity(OnOff.cluster_id)
class IkeaMotion(BinarySensor):
    """ZHA OnOff BinarySensor with motion device class for IKEA devices."""

    _attribute_name = "on_off"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.MOTION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        client_clusters=frozenset({CLUSTER_ON_OFF}),
        manufacturers=frozenset({"IKEA of Sweden"}),
        models=frozenset({"TRADFRI motion sensor"}),
        feature_priority=(PlatformFeatureGroup.BINARY_SENSOR, 1),
    )


@register_entity(OnOff.cluster_id)
class PhilipsMotion(BinarySensor):
    """ZHA OnOff BinarySensor with motion device class for Philips devices."""

    _attribute_name = "on_off"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.MOTION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        client_clusters=frozenset({CLUSTER_ON_OFF}),
        manufacturers=frozenset({"Philips"}),
        models=frozenset({"SML001", "SML002"}),
        feature_priority=(PlatformFeatureGroup.BINARY_SENSOR, 1),
    )


@register_entity(IasZone.cluster_id)
class IASZone(BinarySensor):
    """ZHA IAS BinarySensor."""

    _attribute_name = "zone_status"
    _attr_primary_weight = 3

    # TODO: split this sensor off into individual sensor classes per IASZone type

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_ZONE}))

    def recompute_capabilities(self) -> None:
        """Recompute capabilities."""
        super().recompute_capabilities()
        zone_type = self._cluster.get("zone_type")

        if zone_type is None:
            self._attr_translation_key = "ias_zone"
            self._attr_device_class = None
        else:
            zone_type = IasZone.ZoneType(zone_type)
            self._attr_translation_key = (
                None if zone_type in IAS_ZONE_CLASS_MAPPING else "ias_zone"
            )
            self._attr_device_class = IAS_ZONE_CLASS_MAPPING.get(zone_type)

    @staticmethod
    def parse(value: bool | int) -> bool:
        """Parse the raw attribute into a bool state."""
        # use only bit 0 and 1 for alarm state
        return BinarySensor.parse(value & 0b00000011)

    async def async_update(self) -> None:
        """Attempt to retrieve on off state from the IAS Zone sensor."""
        await self.get_cluster_attribute_value(
            self._cluster,
            self._attribute_name,
            from_cache=False,
        )


@register_entity(IasZone.cluster_id)
class SinopeLeakStatus(BinarySensor):
    """Sinope water leak sensor."""

    _attribute_name = "leak_status"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        clusters=frozenset({CLUSTER_ZONE}),
        models=frozenset({"WL4200", "WL4200S"}),
    )


@register_entity(TUYA_MANUFACTURER_CLUSTER)
class FrostLock(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "frost_lock"
    _unique_id_suffix = "frost_lock"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.LOCK
    _attr_translation_key: str = "frost_lock"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"tuya_manufacturer"}),
        manufacturers=frozenset({"_TZE200_htnnfasr"}),
    )


@register_entity(IKEA_AIR_PURIFIER_CLUSTER)
class ReplaceFilter(BinarySensor):
    """ZHA BinarySensor."""

    _attribute_name = "replace_filter"
    _unique_id_suffix = "replace_filter"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_translation_key: str = "replace_filter"

    _cluster_match = ClusterMatch(clusters=frozenset({"ikea_airpurifier"}))


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederErrorDetected(BinarySensor):
    """ZHA aqara pet feeder error detected binary sensor."""

    _attribute_name = "error_detected"
    _unique_id_suffix = "error_detected"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PROBLEM

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"aqara.feeder.acn001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class XiaomiPlugConsumerConnected(BinarySensor):
    """ZHA Xiaomi plug consumer connected binary sensor."""

    _attribute_name = "consumer_connected"
    _unique_id_suffix = "consumer_connected"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PLUG
    _attr_translation_key: str = "consumer_connected"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.plug.mmeu01", "lumi.plug.maeu01"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraThermostatWindowOpen(BinarySensor):
    """ZHA Aqara thermostat window open binary sensor."""

    _attribute_name = "window_open"
    _unique_id_suffix = "window_open"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.WINDOW

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.airrtc.agl001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraThermostatValveAlarm(BinarySensor):
    """ZHA Aqara thermostat valve alarm binary sensor."""

    _attribute_name = "valve_alarm"
    _unique_id_suffix = "valve_alarm"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PROBLEM
    _attr_translation_key: str = "valve_alarm"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.airrtc.agl001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraThermostatCalibrated(BinarySensor):
    """ZHA Aqara thermostat calibrated binary sensor."""

    _attribute_name = "calibrated"
    _unique_id_suffix = "calibrated"
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_translation_key: str = "calibrated"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.airrtc.agl001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraThermostatExternalSensor(BinarySensor):
    """ZHA Aqara thermostat external sensor binary sensor."""

    _attribute_name = "sensor"
    _unique_id_suffix = "sensor"
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_translation_key: str = "external_sensor"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.airrtc.agl001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraLinkageAlarmState(BinarySensor):
    """ZHA Aqara linkage alarm state binary sensor."""

    _attribute_name = "linkage_alarm_state"
    _unique_id_suffix = "linkage_alarm_state"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.SMOKE
    _attr_translation_key: str = "linkage_alarm_state"

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.sensor_smoke.acn03"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraE1CurtainMotorOpenedByHandBinarySensor(BinarySensor):
    """Opened by hand binary sensor."""

    _unique_id_suffix = "hand_open"
    _attribute_name = "hand_open"
    _attr_translation_key = "hand_open"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.curtain.agl001"}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossMountingModeActive(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether in mounting mode."""

    _unique_id_suffix = "mounting_mode_active"
    _attribute_name = "mounting_mode_active"
    _attr_translation_key: str = "mounting_mode_active"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OPENING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        clusters=frozenset({CLUSTER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossHeatRequired(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether heat is required."""

    _unique_id_suffix = "heat_required"
    _attribute_name = "heat_required"
    _attr_translation_key: str = "heat_required"

    _cluster_match = ClusterMatch(
        clusters=frozenset({CLUSTER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossPreheatStatus(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether in pre-heating mode."""

    _unique_id_suffix = "preheat_status"
    _attribute_name = "preheat_status"
    _attr_translation_key: str = "preheat_status"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        clusters=frozenset({CLUSTER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )
