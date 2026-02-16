"""Binary sensors on Zigbee Home Automation networks."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import functools
import logging
from typing import TYPE_CHECKING, Any

from zhaquirks.quirk_ids import DANFOSS_ALLY_THERMOSTAT
from zigpy.profiles import zha, zll
from zigpy.quirks.v2 import BinarySensorMetadata
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    Cluster,
)
from zigpy.zcl.clusters.general import BinaryInput as BinaryInputCluster, OnOff
from zigpy.zcl.clusters.hvac import Thermostat
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.clusters.security import IasZone

from zha.application import Platform
from zha.application.helpers import safe_read
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
from zha.application.platforms.helpers import validate_device_class
from zha.zigbee.const import (
    AQARA_OPPLE_CLUSTER,
    CLUSTER_HANDLER_ACCELEROMETER,
    CLUSTER_HANDLER_BINARY_INPUT,
    CLUSTER_HANDLER_HUE_OCCUPANCY,
    CLUSTER_HANDLER_OCCUPANCY,
    CLUSTER_HANDLER_ON_OFF,
    CLUSTER_HANDLER_THERMOSTAT,
    CLUSTER_HANDLER_ZONE,
    IKEA_AIR_PURIFIER_CLUSTER,
    REPORT_CONFIG_ASAP,
    REPORT_CONFIG_ATTR,
    REPORT_CONFIG_CONFIG,
    REPORT_CONFIG_DEFAULT,
    REPORT_CONFIG_IMMEDIATE,
    SMARTTHINGS_ACCELERATION_CLUSTER,
    TUYA_MANUFACTURER_CLUSTER,
)

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
    _cluster: Cluster
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
        self._cluster = clusters[0]
        self._state: bool = self.is_on
        self.recompute_capabilities()

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        for event_type in (
            AttributeReadEvent,
            AttributeReportedEvent,
            AttributeUpdatedEvent,
            AttributeWrittenEvent,
        ):
            self._on_remove_callbacks.append(
                self._cluster.on_event(
                    event_type.event_type,
                    self.handle_cluster_attribute_updated,
                )
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
        self,
        event: AttributeReadEvent
        | AttributeReportedEvent
        | AttributeUpdatedEvent
        | AttributeWrittenEvent,
    ) -> None:
        """Handle attribute updates from the cluster."""
        if self._attribute_name is None or self._attribute_name != event.attribute_name:
            return
        self._state = bool(event.value)
        self.maybe_emit_state_changed_event()

    async def async_update(self) -> None:
        """Attempt to retrieve on off state from the binary sensor."""
        await super().async_update()
        # this is a cached read to get the value for state mgt so there is no double read
        attribute = self._attribute_name
        attr_value = (
            await safe_read(
                self._cluster,
                [attribute],
                allow_cache=True,
                only_cache=True,
            )
        ).get(attribute)
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

    REPORT_CONFIG = {
        "accelerometer": (
            {
                REPORT_CONFIG_ATTR: "acceleration",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
            {REPORT_CONFIG_ATTR: "x_axis", REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP},
            {REPORT_CONFIG_ATTR: "y_axis", REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP},
            {REPORT_CONFIG_ATTR: "z_axis", REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP},
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "acceleration"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.MOVING
    _attr_translation_key: str = "accelerometer"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ACCELEROMETER})
    )


@register_entity(OccupancySensing.cluster_id)
class Occupancy(BinarySensor):
    """ZHA BinarySensor."""

    REPORT_CONFIG = {
        OccupancySensing.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: OccupancySensing.AttributeDefs.occupancy.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "occupancy"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OCCUPANCY
    _attr_primary_weight = 2

    _cluster_match = ClusterMatch(in_clusters=frozenset({CLUSTER_HANDLER_OCCUPANCY}))


@register_entity(OccupancySensing.cluster_id)
class HueOccupancy(BinarySensor):
    """ZHA Hue occupancy."""

    REPORT_CONFIG = {
        OccupancySensing.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: OccupancySensing.AttributeDefs.occupancy.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "occupancy"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OCCUPANCY
    _attr_primary_weight = 3

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_HUE_OCCUPANCY})
    )


@register_entity(OnOff.cluster_id)
class Opening(BinarySensor):
    """ZHA OnOff BinarySensor."""

    _attribute_name = "on_off"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OPENING
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        out_clusters=frozenset({CLUSTER_HANDLER_ON_OFF}),
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

    REPORT_CONFIG = {
        BinaryInputCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: BinaryInputCluster.AttributeDefs.present_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        BinaryInputCluster.ep_attribute: {
            BinaryInputCluster.AttributeDefs.description.name: True,
        },
    }
    _attribute_name = "present_value"

    _cluster_match = ClusterMatch(in_clusters=frozenset({CLUSTER_HANDLER_BINARY_INPUT}))

    def recompute_capabilities(self) -> None:
        """Recompute capabilities."""
        super().recompute_capabilities()
        self._attr_fallback_name = self._cluster.get(
            BinaryInputCluster.AttributeDefs.description.name
        )

    def _is_supported(self) -> bool:
        if self._cluster.get(BinaryInputCluster.AttributeDefs.description.name) is None:
            return False

        return super()._is_supported()


@register_entity(BinaryInputCluster.cluster_id)
class BinaryInput(BinarySensor):
    """ZHA BinarySensor."""

    REPORT_CONFIG = {
        BinaryInputCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: BinaryInputCluster.AttributeDefs.present_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        BinaryInputCluster.ep_attribute: {
            BinaryInputCluster.AttributeDefs.description.name: True,
        },
    }
    _attribute_name = "present_value"
    _attr_translation_key: str = "binary_input"

    _cluster_match = ClusterMatch(in_clusters=frozenset({CLUSTER_HANDLER_BINARY_INPUT}))

    def _is_supported(self) -> bool:
        # Prefer to use the "WithDescription" variant above
        if (
            self._cluster.get(BinaryInputCluster.AttributeDefs.description.name)
            is not None
        ):
            return False

        return super()._is_supported()


@register_entity(OnOff.cluster_id)
class IkeaMotion(BinarySensor):
    """ZHA OnOff BinarySensor with motion device class for IKEA devices."""

    _attribute_name = "on_off"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.MOTION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        out_clusters=frozenset({CLUSTER_HANDLER_ON_OFF}),
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
        out_clusters=frozenset({CLUSTER_HANDLER_ON_OFF}),
        manufacturers=frozenset({"Philips"}),
        models=frozenset({"SML001", "SML002"}),
        feature_priority=(PlatformFeatureGroup.BINARY_SENSOR, 1),
    )


@register_entity(IasZone.cluster_id)
class IASZone(BinarySensor):
    """ZHA IAS BinarySensor."""

    REPORT_CONFIG = {}
    ZCL_INIT_ATTRS = {
        IasZone.ep_attribute: {
            IasZone.AttributeDefs.zone_state.name: True,
            IasZone.AttributeDefs.zone_status.name: False,
            IasZone.AttributeDefs.zone_type.name: True,
        },
    }
    _attribute_name = "zone_status"
    _attr_primary_weight = 3

    # TODO: split this sensor off into individual sensor classes per IASZone type

    _cluster_match = ClusterMatch(in_clusters=frozenset({CLUSTER_HANDLER_ZONE}))

    def recompute_capabilities(self) -> None:
        """Recompute capabilities."""
        super().recompute_capabilities()
        zone_type = self._cluster.get(IasZone.AttributeDefs.zone_type.name)

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
        await PlatformEntity.async_update(self)
        attribute = self._attribute_name
        attr_value = (
            await safe_read(
                self._cluster,
                [attribute],
                allow_cache=False,
                only_cache=False,
            )
        ).get(attribute)
        if attr_value is not None:
            self._state = attr_value
            self.maybe_emit_state_changed_event()


@register_entity(IasZone.cluster_id)
class SinopeLeakStatus(BinarySensor):
    """Sinope water leak sensor."""

    REPORT_CONFIG = {}
    ZCL_INIT_ATTRS = {
        IasZone.ep_attribute: {
            IasZone.AttributeDefs.zone_state.name: True,
            IasZone.AttributeDefs.zone_status.name: False,
            IasZone.AttributeDefs.zone_type.name: True,
        },
    }
    _attribute_name = "leak_status"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ZONE}),
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
        in_clusters=frozenset({"tuya_manufacturer"}),
        manufacturers=frozenset({"_TZE200_htnnfasr"}),
    )


@register_entity(IKEA_AIR_PURIFIER_CLUSTER)
class ReplaceFilter(BinarySensor):
    """ZHA BinarySensor."""

    REPORT_CONFIG = {
        "ikea_airpurifier": (
            {
                REPORT_CONFIG_ATTR: "air_quality_25pm",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: "child_lock",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: "device_run_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "disable_led",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: "fan_mode",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: "fan_speed",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: "filter_life_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "filter_run_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "replace_filter",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "replace_filter"
    _unique_id_suffix = "replace_filter"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC
    _attr_translation_key: str = "replace_filter"

    _cluster_match = ClusterMatch(in_clusters=frozenset({"ikea_airpurifier"}))


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederErrorDetected(BinarySensor):
    """ZHA aqara pet feeder error detected binary sensor."""

    _attribute_name = "error_detected"
    _unique_id_suffix = "error_detected"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.PROBLEM

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.plug.mmeu01", "lumi.plug.maeu01"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraThermostatWindowOpen(BinarySensor):
    """ZHA Aqara thermostat window open binary sensor."""

    _attribute_name = "window_open"
    _unique_id_suffix = "window_open"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.WINDOW

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
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
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.curtain.agl001"}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossMountingModeActive(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether in mounting mode."""

    REPORT_CONFIG = {
        Thermostat.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: "adaptation_run_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "heat_required",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
            {
                REPORT_CONFIG_ATTR: "load_estimate",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.local_temperature.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "mounting_mode_active",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupancy.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "open_window_detection",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_cooling_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_heating_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: "preheat_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "preheat_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_state.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.system_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Thermostat.ep_attribute: {
            Thermostat.AttributeDefs.abs_max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.name: True,
            "adaptation_run_control": True,
            "adaptation_run_settings": True,
            "control_algorithm_scale_factor": True,
            Thermostat.AttributeDefs.ctrl_sequence_of_oper.name: False,
            "exercise_day_of_week": True,
            "exercise_trigger_time": True,
            "external_measured_room_sensor": False,
            "external_open_window_detected": True,
            "heat_available": True,
            "load_balancing_enable": True,
            "load_room_mean": False,
            Thermostat.AttributeDefs.local_temperature_calibration.name: True,
            Thermostat.AttributeDefs.max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_heat_setpoint_limit.name: True,
            "mounting_mode_control": False,
            "orientation": True,
            "radiator_covered": True,
            "regulation_setpoint_offset": True,
            Thermostat.AttributeDefs.setpoint_change_source.name: True,
            "window_open_feature": True,
        },
    }
    _unique_id_suffix = "mounting_mode_active"
    _attribute_name = "mounting_mode_active"
    _attr_translation_key: str = "mounting_mode_active"
    _attr_device_class: BinarySensorDeviceClass = BinarySensorDeviceClass.OPENING
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossHeatRequired(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether heat is required."""

    REPORT_CONFIG = {
        Thermostat.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: "adaptation_run_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "heat_required",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
            {
                REPORT_CONFIG_ATTR: "load_estimate",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.local_temperature.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "mounting_mode_active",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupancy.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "open_window_detection",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_cooling_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_heating_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: "preheat_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "preheat_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_state.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.system_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Thermostat.ep_attribute: {
            Thermostat.AttributeDefs.abs_max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.name: True,
            "adaptation_run_control": True,
            "adaptation_run_settings": True,
            "control_algorithm_scale_factor": True,
            Thermostat.AttributeDefs.ctrl_sequence_of_oper.name: False,
            "exercise_day_of_week": True,
            "exercise_trigger_time": True,
            "external_measured_room_sensor": False,
            "external_open_window_detected": True,
            "heat_available": True,
            "load_balancing_enable": True,
            "load_room_mean": False,
            Thermostat.AttributeDefs.local_temperature_calibration.name: True,
            Thermostat.AttributeDefs.max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_heat_setpoint_limit.name: True,
            "mounting_mode_control": False,
            "orientation": True,
            "radiator_covered": True,
            "regulation_setpoint_offset": True,
            Thermostat.AttributeDefs.setpoint_change_source.name: True,
            "window_open_feature": True,
        },
    }
    _unique_id_suffix = "heat_required"
    _attribute_name = "heat_required"
    _attr_translation_key: str = "heat_required"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossPreheatStatus(BinarySensor):
    """Danfoss TRV proprietary attribute exposing whether in pre-heating mode."""

    REPORT_CONFIG = {
        Thermostat.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: "adaptation_run_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "heat_required",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
            {
                REPORT_CONFIG_ATTR: "load_estimate",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.local_temperature.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "mounting_mode_active",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupancy.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.occupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: "open_window_detection",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_cooling_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.pi_heating_demand.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: "preheat_status",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "preheat_time",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.running_state.name,
                REPORT_CONFIG_CONFIG: (30, 900, 5),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.system_mode.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_cooling_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
            {
                REPORT_CONFIG_ATTR: Thermostat.AttributeDefs.unoccupied_heating_setpoint.name,
                REPORT_CONFIG_CONFIG: (30, 900, 25),
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Thermostat.ep_attribute: {
            Thermostat.AttributeDefs.abs_max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.abs_min_heat_setpoint_limit.name: True,
            "adaptation_run_control": True,
            "adaptation_run_settings": True,
            "control_algorithm_scale_factor": True,
            Thermostat.AttributeDefs.ctrl_sequence_of_oper.name: False,
            "exercise_day_of_week": True,
            "exercise_trigger_time": True,
            "external_measured_room_sensor": False,
            "external_open_window_detected": True,
            "heat_available": True,
            "load_balancing_enable": True,
            "load_room_mean": False,
            Thermostat.AttributeDefs.local_temperature_calibration.name: True,
            Thermostat.AttributeDefs.max_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.max_heat_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_cool_setpoint_limit.name: True,
            Thermostat.AttributeDefs.min_heat_setpoint_limit.name: True,
            "mounting_mode_control": False,
            "orientation": True,
            "radiator_covered": True,
            "regulation_setpoint_offset": True,
            Thermostat.AttributeDefs.setpoint_change_source.name: True,
            "window_open_feature": True,
        },
    }
    _unique_id_suffix = "preheat_status"
    _attribute_name = "preheat_status"
    _attr_translation_key: str = "preheat_status"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )
