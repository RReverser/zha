"""Sensors on Zigbee Home Automation networks."""  # pylint: disable=too-many-lines

from __future__ import annotations

from asyncio import Task
import contextlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import enum
import functools
import logging
import numbers
import typing
from typing import TYPE_CHECKING, Any, cast

from zhaquirks.danfoss import thermostat as danfoss_thermostat
from zhaquirks.quirk_ids import DANFOSS_ALLY_THERMOSTAT
from zigpy import types
from zigpy.quirks.v2 import ZCLEnumMetadata, ZCLSensorMetadata
from zigpy.state import Counter, State
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    Cluster,
    foundation,
)
from zigpy.zcl.clusters.closures import WindowCovering
from zigpy.zcl.clusters.general import (
    AnalogInput,
    Basic,
    DeviceTemperature as DeviceTemperatureCluster,
    PowerConfiguration,
)
from zigpy.zcl.clusters.general_const import ApplicationType
from zigpy.zcl.clusters.homeautomation import Diagnostic, ElectricalMeasurement
from zigpy.zcl.clusters.hvac import Thermostat
from zigpy.zcl.clusters.measurement import (
    PM25 as PM25Cluster,
    CarbonDioxideConcentration as CarbonDioxideConcentrationCluster,
    CarbonMonoxideConcentration as CarbonMonoxideConcentrationCluster,
    ElectricalConductivity as ElectricalConductivityCluster,
    FlowMeasurement,
    FormaldehydeConcentration as FormaldehydeConcentrationCluster,
    IlluminanceMeasurement,
    LeafWetness as LeafWetnessCluster,
    PressureMeasurement,
    RelativeHumidity,
    SoilMoisture as SoilMoistureCluster,
    TemperatureMeasurement,
    WindSpeed as WindSpeedCluster,
)
from zigpy.zcl.clusters.smartenergy import (
    Metering,
    MeteringUnitofMeasure,
    NumberFormatting,
)

from zha.application import Platform
from zha.application.helpers import safe_read
from zha.application.platforms import (
    BaseEntity,
    BaseEntityInfo,
    BaseIdentifiers,
    ClusterMatch,
    EntityCategory,
    PlatformEntity,
    PlatformFeatureGroup,
    register_entity,
)
from zha.application.platforms.climate.const import HVACAction
from zha.application.platforms.helpers import validate_device_class
from zha.application.platforms.number.bacnet import BACNET_UNITS_TO_HA_UNITS
from zha.application.platforms.sensor.const import (
    ANALOG_INPUT_APPTYPE_DEV_CLASS,
    ANALOG_INPUT_APPTYPE_UNITS,
    ZCL_EPOCH,
    SensorDeviceClass,
    SensorStateClass,
)
from zha.application.platforms.sensor.helpers import (
    create_number_formatter,
    resolution_to_decimal_precision,
)
from zha.decorators import periodic
from zha.units import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_BILLION,
    CONCENTRATION_PARTS_PER_MILLION,
    LIGHT_LUX,
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfApparentPower,
    UnitOfConductivity,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfMass,
    UnitOfPower,
    UnitOfPressure,
    UnitOfSpeed,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
    UnitOfVolumeFlowRate,
)
from zha.zigbee.const import (
    AQARA_OPPLE_CLUSTER,
    CLUSTER_HANDLER_ANALOG_INPUT,
    CLUSTER_HANDLER_BASIC,
    CLUSTER_HANDLER_COVER,
    CLUSTER_HANDLER_DEVICE_TEMPERATURE,
    CLUSTER_HANDLER_DIAGNOSTIC,
    CLUSTER_HANDLER_ELECTRICAL_CONDUCTIVITY,
    CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT,
    CLUSTER_HANDLER_FLOW,
    CLUSTER_HANDLER_HUMIDITY,
    CLUSTER_HANDLER_ILLUMINANCE,
    CLUSTER_HANDLER_INOVELLI,
    CLUSTER_HANDLER_LEAF_WETNESS,
    CLUSTER_HANDLER_POWER_CONFIGURATION,
    CLUSTER_HANDLER_PRESSURE,
    CLUSTER_HANDLER_SMARTENERGY_METERING,
    CLUSTER_HANDLER_SOIL_MOISTURE,
    CLUSTER_HANDLER_TEMPERATURE,
    CLUSTER_HANDLER_THERMOSTAT,
    CLUSTER_HANDLER_WIND_SPEED,
    CLUSTER_READS_PER_REQ,
    IKEA_AIR_PURIFIER_CLUSTER,
    INOVELLI_CLUSTER,
    REPORT_CONFIG_ASAP,
    REPORT_CONFIG_ATTR,
    REPORT_CONFIG_BATTERY_SAVE,
    REPORT_CONFIG_CONFIG,
    REPORT_CONFIG_DEFAULT,
    REPORT_CONFIG_IMMEDIATE,
    REPORT_CONFIG_OP,
    SMARTTHINGS_HUMIDITY_CLUSTER,
    SONOFF_CLUSTER,
    TUYA_MANUFACTURER_CLUSTER,
)

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint

DEFAULT_FORMATTING = NumberFormatting(
    num_digits_right_of_decimal=1,
    num_digits_left_of_decimal=15,
    suppress_leading_zeros=1,
)

BATTERY_SIZES = {
    0: "No battery",
    1: "Built in",
    2: "Other",
    3: "AA",
    4: "AAA",
    5: "C",
    6: "D",
    7: "CR2",
    8: "CR123A",
    9: "CR2450",
    10: "CR2032",
    11: "CR1632",
    255: "Unknown",
}

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class SensorEntityInfo(BaseEntityInfo):
    """Sensor entity info."""

    suggested_display_precision: int | None = None
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None


@dataclass(frozen=True, kw_only=True)
class DeviceCounterEntityInfo(BaseEntityInfo):
    """Device counter entity info."""

    device_ieee: str
    suggested_display_precision: int
    available: bool
    counter: str
    counter_value: int
    counter_groups: str
    counter_group: str


@dataclass(frozen=True, kw_only=True)
class DeviceCounterSensorIdentifiers(BaseIdentifiers):
    """Device counter sensor identifiers."""

    device_ieee: str


class Sensor(PlatformEntity):
    """Base ZHA sensor."""

    PLATFORM = Platform.SENSOR
    _attribute_name: int | str | None = None
    _attribute_converter: typing.Callable[[typing.Any], typing.Any] | None = None
    _divisor: int | float | None = None
    _multiplier: int | float | None = None
    _attr_suggested_display_precision: int | None = None
    _attr_native_unit_of_measurement: str | None = None
    _attr_device_class: SensorDeviceClass | None = None
    _attr_state_class: SensorStateClass | None = None
    _skip_creation_if_no_attr_cache: bool = False

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this sensor."""
        self._cluster: Cluster = clusters[0]
        self._attr_def: foundation.ZCLAttributeDef | None = None

        if self._attribute_name is not None:
            # If the attribute definition does not exist, this entity will be filtered
            # out via `is_supported`
            with contextlib.suppress(KeyError):
                self._attr_def = self._cluster.find_attribute(self._attribute_name)

        super().__init__(clusters, endpoint, device, **kwargs)
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

    def _is_supported(self) -> bool:
        if (
            self._attribute_name not in self._cluster.attributes_by_name
        ) or self._cluster.is_attribute_unsupported(self._attribute_name):
            _LOGGER.debug(
                "%s is not supported - skipping %s entity creation",
                self._attribute_name,
                self.__class__.__name__,
            )
            return False

        if (
            self._skip_creation_if_no_attr_cache
            and self._cluster.get(self._attribute_name) is None
        ):
            return False

        return super()._is_supported()

    def _validate_state_class(
        self,
        state_class_value: SensorStateClass,
    ) -> SensorStateClass | None:
        """Validate and return a state class."""
        try:
            return SensorStateClass(state_class_value.value)
        except ValueError as ex:
            _LOGGER.warning(
                "Quirks provided an invalid state class: %s: %s",
                state_class_value,
                ex,
            )
            return None

    def _init_from_quirks_metadata(self, entity_metadata: ZCLSensorMetadata) -> None:
        """Init this entity from the quirks metadata."""
        super()._init_from_quirks_metadata(entity_metadata)
        self._attribute_name = entity_metadata.attribute_name
        if entity_metadata.attribute_converter is not None:
            self._attribute_converter = entity_metadata.attribute_converter
        if entity_metadata.divisor is not None and entity_metadata.divisor != 1:
            self._divisor = entity_metadata.divisor
        if entity_metadata.multiplier is not None and entity_metadata.multiplier != 1:
            self._multiplier = entity_metadata.multiplier
        if entity_metadata.device_class is not None:
            self._attr_device_class = validate_device_class(
                SensorDeviceClass,
                entity_metadata.device_class,
                Platform.SENSOR.value,
                _LOGGER,
            )
        if entity_metadata.state_class is not None:
            self._attr_state_class = self._validate_state_class(
                entity_metadata.state_class
            )
        if entity_metadata.unit is not None:
            self._attr_native_unit_of_measurement = entity_metadata.unit

    @functools.cached_property
    def info_object(self) -> SensorEntityInfo:
        """Return a representation of the sensor."""
        return SensorEntityInfo(
            **super().info_object.__dict__,
            suggested_display_precision=self._attr_suggested_display_precision,
            unit=(
                getattr(self, "entity_description").native_unit_of_measurement
                if getattr(self, "entity_description", None) is not None
                else self._attr_native_unit_of_measurement
            ),
        )

    @property
    def state(self) -> dict:
        """Return the state for this sensor."""
        response = super().state
        native_value = self.native_value
        response["state"] = native_value
        return response

    @property
    def native_value(self) -> date | datetime | str | int | float | None:
        """Return the state of the entity."""
        assert self._attribute_name is not None
        raw_state = self._cluster.get(self._attribute_name)
        if raw_state is None:
            return None
        if self._is_non_value(raw_state):
            return None
        if self._attribute_converter:
            return self._attribute_converter(raw_state)
        return self.formatter(raw_state)

    async def async_update(self) -> None:
        """Retrieve latest state from the sensor cluster."""
        await super().async_update()

        attrs_to_read: list[str] = []
        cluster_name = self.endpoint.resolve_cluster_name(self._cluster)
        polling_attrs = getattr(self, "_polling_attrs", None)
        if polling_attrs:
            attrs_to_read.extend(polling_attrs)
        else:
            report_entries = self.entity_report_config.get(cluster_name, ())
            if report_entries:
                attrs_to_read.extend(
                    cast(str, report_entry[REPORT_CONFIG_ATTR])
                    for report_entry in report_entries
                )
            elif self._attribute_name is not None:
                attrs_to_read.append(self._attribute_name)

        deduped_attrs: list[str] = []
        seen: set[str] = set()
        for attr in attrs_to_read:
            if attr in seen:
                continue
            seen.add(attr)
            if attr not in self._cluster.attributes_by_name:
                continue
            if self._cluster.is_attribute_unsupported(attr):
                continue
            deduped_attrs.append(attr)

        if deduped_attrs:
            chunk = deduped_attrs[:CLUSTER_READS_PER_REQ]
            rest = deduped_attrs[CLUSTER_READS_PER_REQ:]
            while chunk:
                await safe_read(
                    self._cluster,
                    chunk,
                    allow_cache=False,
                    only_cache=False,
                )
                chunk = rest[:CLUSTER_READS_PER_REQ]
                rest = rest[CLUSTER_READS_PER_REQ:]
            self.maybe_emit_state_changed_event()

    def handle_cluster_attribute_updated(
        self,
        event: AttributeReadEvent
        | AttributeReportedEvent
        | AttributeUpdatedEvent
        | AttributeWrittenEvent,
    ) -> None:
        """Handle attribute updates from the cluster handler."""
        if (
            event.attribute_name == self._attribute_name
            or (
                hasattr(self, "_attr_extra_state_attribute_names")
                and event.attribute_name
                in getattr(self, "_attr_extra_state_attribute_names")
            )
            or self._attribute_name is None
        ):
            self.maybe_emit_state_changed_event()

    def _is_non_value(
        self, value: int | float, *, attr_def: foundation.ZCLAttributeDef | None = None
    ) -> bool:
        """Ignore non-value numerical values."""
        if attr_def is None:
            attr_def = self._attr_def

        if attr_def is None:
            return False

        data_type = foundation.DataType.from_type_id(attr_def.zcl_type)
        return value == data_type.non_value

    def formatter(self, value: Any) -> date | datetime | int | float | str | None:
        """Numeric pass-through formatter."""
        if self._multiplier is not None:
            value *= self._multiplier

        if self._divisor is not None:
            value /= self._divisor

        return value


class TimestampSensor(Sensor):
    """Timestamp ZHA sensor."""


class PollableSensor(Sensor):
    """Base ZHA sensor that polls for state."""

    _REFRESH_INTERVAL = (30, 45)
    _use_custom_polling: bool = True
    __polling_interval: int

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this sensor."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._polling_task: Task | None = None

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.maybe_start_polling()

    @property
    def should_poll(self) -> bool:
        """Return True if we need to poll for state changes."""
        return self._use_custom_polling

    def maybe_start_polling(self) -> None:
        """Start polling if necessary."""
        if self.should_poll:
            self._polling_task = self.device.gateway.async_create_background_task(
                self._refresh(),
                name=f"sensor_state_poller_{self.unique_id}_{self.__class__.__name__}",
                eager_start=True,
                untracked=True,
            )
            self._tracked_tasks.append(self._polling_task)
            self.debug(
                "started polling with refresh interval of %s",
                getattr(self, "__polling_interval"),
            )

    def enable(self) -> None:
        """Enable the entity."""
        super().enable()
        self.maybe_start_polling()

    def disable(self) -> None:
        """Disable the entity."""
        super().disable()
        if self._polling_task:
            self._tracked_tasks.remove(self._polling_task)
            self._polling_task.cancel()
            self._polling_task = None

    @periodic(_REFRESH_INTERVAL)
    async def _refresh(self):
        """Call async_update at a constrained random interval."""
        if self.device.available and self.device.gateway.config.allow_polling:
            self.debug("polling for updated state")
            await self.async_update()
            self.maybe_emit_state_changed_event()
        else:
            self.debug(
                "skipping polling for updated state, available: %s, allow polled requests: %s",
                self.device.available,
                self.device.gateway.config.allow_polling,
            )


class DeviceCounterSensor(BaseEntity):
    """Device counter sensor."""

    PLATFORM = Platform.SENSOR
    _attr_state_class: SensorStateClass = SensorStateClass.TOTAL
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        zha_device: Device,
        counter_groups: str,
        counter_group: str,
        counter: str,
    ) -> None:
        """Init this sensor."""

        # XXX: ZHA uses the IEEE address of the device passed through `slugify`!
        slugified_device_id = zha_device.unique_id.replace(":", "-")
        super().__init__(
            unique_id=f"{slugified_device_id}_{counter_groups}_{counter_group}_{counter}"
        )

        self._device: Device = zha_device
        state: State = self._device.gateway.application_controller.state
        self._zigpy_counter: Counter = (
            getattr(state, counter_groups).get(counter_group, {}).get(counter, None)
        )
        self._zigpy_counter_groups: str = counter_groups
        self._zigpy_counter_group: str = counter_group

        self._attr_fallback_name: str = self._zigpy_counter.name
        self._always_supported: bool = True

        # TODO: why do entities get created with " None" as a name suffix instead of
        # falling back to `fallback_name`? We should be able to provide translation keys
        # even if they do not exist.
        # self._attr_translation_key = f"counter_{self._zigpy_counter.name.lower()}"

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self._device.gateway.global_updater.register_update_listener(self.update)
        self._on_remove_callbacks.append(
            lambda: self._device.gateway.global_updater.remove_update_listener(
                self.update
            )
        )

    @functools.cached_property
    def identifiers(self) -> DeviceCounterSensorIdentifiers:
        """Return a dict with the information necessary to identify this entity."""
        return DeviceCounterSensorIdentifiers(
            **super().identifiers.__dict__, device_ieee=str(self._device.ieee)
        )

    @functools.cached_property
    def info_object(self) -> DeviceCounterEntityInfo:
        """Return a representation of the platform entity."""
        return DeviceCounterEntityInfo(
            **super().info_object.__dict__,
            suggested_display_precision=self._attr_suggested_display_precision,
            counter=self._zigpy_counter.name,
            counter_value=self._zigpy_counter.value,
            counter_groups=self._zigpy_counter_groups,
            counter_group=self._zigpy_counter_group,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Return the state for this sensor."""
        response = super().state
        response["state"] = self._zigpy_counter.value
        return response

    @property
    def native_value(self) -> int | None:
        """Return the state of the entity."""
        return self._zigpy_counter.value

    @property
    def available(self) -> bool:
        """Return entity availability."""
        return self._device.available

    @functools.cached_property
    def device(self) -> Device:
        """Return the device."""
        return self._device

    def enable(self) -> None:
        """Enable the entity."""
        super().enable()
        self._device.gateway.global_updater.register_update_listener(self.update)

    def disable(self) -> None:
        """Disable the entity."""
        super().disable()
        self._device.gateway.global_updater.remove_update_listener(self.update)

    def update(self):
        """Call async_update at a constrained random interval."""
        if self._device.available and self._device.gateway.config.allow_polling:
            self.debug("polling for updated state")
            self.maybe_emit_state_changed_event()
        else:
            self.debug(
                "skipping polling for updated state, available: %s, allow polled requests: %s",
                self._device.available,
                self._device.gateway.config.allow_polling,
            )


class EnumSensor(Sensor):
    """Sensor with value from enum."""

    _attr_device_class: SensorDeviceClass = SensorDeviceClass.ENUM
    _enum: type[enum.Enum]

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this sensor."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._attr_options = [e.name for e in self._enum]

        # XXX: This class is not meant to be initialized directly, as `unique_id`
        # depends on the value of `_attribute_name`

    def _init_from_quirks_metadata(self, entity_metadata: ZCLEnumMetadata) -> None:
        """Init this entity from the quirks metadata."""
        self._attribute_name = entity_metadata.attribute_name
        self._enum = entity_metadata.enum

        PlatformEntity._init_from_quirks_metadata(self, entity_metadata)  # pylint: disable=protected-access

    def formatter(self, value: int) -> str | None:
        """Use name of enum."""
        assert self._enum is not None
        return self._enum(value).name


@register_entity(AnalogInput.cluster_id)
class DigiAnalogInput(Sensor):
    """Sensor that displays analog input values."""

    REPORT_CONFIG = {
        AnalogInput.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: AnalogInput.AttributeDefs.present_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        AnalogInput.ep_attribute: {
            AnalogInput.AttributeDefs.application_type.name: True,
            AnalogInput.AttributeDefs.description.name: True,
            AnalogInput.AttributeDefs.engineering_units.name: True,
            AnalogInput.AttributeDefs.max_present_value.name: True,
            AnalogInput.AttributeDefs.min_present_value.name: True,
            AnalogInput.AttributeDefs.out_of_service.name: True,
            AnalogInput.AttributeDefs.reliability.name: True,
            AnalogInput.AttributeDefs.resolution.name: True,
            AnalogInput.AttributeDefs.status_flags.name: True,
        },
    }
    _attribute_name = "present_value"
    _attr_translation_key: str = "analog_input"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ANALOG_INPUT}),
        manufacturers=frozenset({"Digi"}),
    )


@register_entity(AnalogInput.cluster_id)
class AnalogInputSensor(Sensor):
    """Sensor that displays analog input values."""

    REPORT_CONFIG = {
        AnalogInput.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: AnalogInput.AttributeDefs.present_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        AnalogInput.ep_attribute: {
            AnalogInput.AttributeDefs.application_type.name: True,
            AnalogInput.AttributeDefs.description.name: True,
            AnalogInput.AttributeDefs.engineering_units.name: True,
            AnalogInput.AttributeDefs.max_present_value.name: True,
            AnalogInput.AttributeDefs.min_present_value.name: True,
            AnalogInput.AttributeDefs.out_of_service.name: True,
            AnalogInput.AttributeDefs.reliability.name: True,
            AnalogInput.AttributeDefs.resolution.name: True,
            AnalogInput.AttributeDefs.status_flags.name: True,
        },
    }
    _attribute_name = "present_value"
    _unique_id_suffix = "analog_input"
    _attr_state_class = SensorStateClass.MEASUREMENT

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ANALOG_INPUT}),
    )

    def recompute_capabilities(self) -> None:
        """Recompute capabilities."""
        super().recompute_capabilities()
        description = self._cluster.get(AnalogInput.AttributeDefs.description.name)
        application_type = self._cluster.get(
            AnalogInput.AttributeDefs.application_type.name
        )
        engineering_units = self._cluster.get(
            AnalogInput.AttributeDefs.engineering_units.name
        )
        resolution = self._cluster.get(AnalogInput.AttributeDefs.resolution.name)

        self._attr_fallback_name = description

        if application_type is not None:
            # The application type encodes a tiny bit more info but it's mostly
            # irrelevant, just use the `type` sub-field
            app_type = ApplicationType(int(application_type)).type
            self._attr_device_class = ANALOG_INPUT_APPTYPE_DEV_CLASS.get(app_type)

            # Application type units take precedence
            self._attr_native_unit_of_measurement = ANALOG_INPUT_APPTYPE_UNITS.get(
                app_type
            )
        else:
            self._attr_native_unit_of_measurement = BACNET_UNITS_TO_HA_UNITS.get(
                engineering_units
            )

        # Resolution indicates the minimum change in value that can be detected
        if resolution is not None:
            self._attr_suggested_display_precision = resolution_to_decimal_precision(
                resolution
            )

    def _is_supported(self) -> bool:
        """Return True if this sensor is supported."""
        if self._cluster.get(AnalogInput.AttributeDefs.description.name) is None:
            return False

        # The units are determined by one of these
        if (
            self._cluster.get(AnalogInput.AttributeDefs.application_type.name) is None
            and self._cluster.get(AnalogInput.AttributeDefs.engineering_units.name)
            is None
        ):
            return False

        return super()._is_supported()


@register_entity(PowerConfiguration.cluster_id)
class Battery(Sensor):
    """Battery sensor of power configuration cluster."""

    REPORT_CONFIG = {
        PowerConfiguration.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: PowerConfiguration.AttributeDefs.battery_percentage_remaining.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_BATTERY_SAVE,
            },
            {
                REPORT_CONFIG_ATTR: PowerConfiguration.AttributeDefs.battery_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_BATTERY_SAVE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        PowerConfiguration.ep_attribute: {
            PowerConfiguration.AttributeDefs.battery_size.name: True,
            PowerConfiguration.AttributeDefs.battery_quantity.name: True,
        },
    }
    _attribute_name = "battery_percentage_remaining"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.BATTERY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_suggested_display_precision: int = 0
    _attr_extra_state_attribute_names: set[str] = {
        "battery_size",
        "battery_quantity",
        "battery_voltage",
    }

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_POWER_CONFIGURATION}),
    )

    def _is_supported(self) -> bool:
        # XXX: We intentionally ignore the presence of this attribute
        return PlatformEntity._is_supported(self) and not self.device.is_mains_powered

    @staticmethod
    def formatter(value: int) -> float | None:  # pylint: disable=arguments-differ
        """Return the state of the entity."""
        # per zcl specs battery percent is reported at 200% ¯\_(ツ)_/¯
        if not isinstance(value, numbers.Number) or value in (-1, 255):
            return None
        return value / 2

    @property
    def state(self) -> dict[str, Any]:
        """Return the state for battery sensors."""
        response = super().state
        battery_size = self._cluster.get("battery_size")
        if battery_size is not None:
            response["battery_size"] = BATTERY_SIZES.get(battery_size, "Unknown")
        battery_quantity = self._cluster.get("battery_quantity")
        if battery_quantity is not None:
            response["battery_quantity"] = battery_quantity
        battery_voltage = self._cluster.get("battery_voltage")
        if battery_voltage is not None:
            response["battery_voltage"] = round(battery_voltage / 10, 2)
        return response


class BaseElectricalMeasurement(PollableSensor):
    """Base class for electrical measurement."""

    AC_POWER_DIVISOR_ATTR = ElectricalMeasurement.AttributeDefs.ac_power_divisor.name
    AC_POWER_MULTIPLIER_ATTR = (
        ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name
    )
    DC_POWER_DIVISOR_ATTR = ElectricalMeasurement.AttributeDefs.dc_power_divisor.name
    DC_POWER_MULTIPLIER_ATTR = (
        ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name
    )
    POWER_DIVISOR_ATTR = ElectricalMeasurement.AttributeDefs.power_divisor.name
    POWER_MULTIPLIER_ATTR = ElectricalMeasurement.AttributeDefs.power_multiplier.name

    POWER_DIVISOR_FALLBACK_ATTRS: dict[str, tuple[str, ...]] = {
        AC_POWER_DIVISOR_ATTR: (POWER_DIVISOR_ATTR,),
        DC_POWER_DIVISOR_ATTR: (POWER_DIVISOR_ATTR,),
    }
    POWER_MULTIPLIER_FALLBACK_ATTRS: dict[str, tuple[str, ...]] = {
        AC_POWER_MULTIPLIER_ATTR: (POWER_MULTIPLIER_ATTR,),
        DC_POWER_MULTIPLIER_ATTR: (POWER_MULTIPLIER_ATTR,),
    }

    _polling_attrs: tuple[str, ...] = (
        ElectricalMeasurement.AttributeDefs.ac_frequency.name,
        ElectricalMeasurement.AttributeDefs.ac_frequency_max.name,
        ElectricalMeasurement.AttributeDefs.active_power.name,
        ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
        ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
        ElectricalMeasurement.AttributeDefs.active_power_max.name,
        ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name,
        ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name,
        ElectricalMeasurement.AttributeDefs.total_active_power.name,
        ElectricalMeasurement.AttributeDefs.apparent_power.name,
        ElectricalMeasurement.AttributeDefs.power_factor.name,
        ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name,
        ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name,
        ElectricalMeasurement.AttributeDefs.rms_current.name,
        ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
        ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
        ElectricalMeasurement.AttributeDefs.rms_current_max.name,
        ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name,
        ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage_max.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name,
        ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name,
        ElectricalMeasurement.AttributeDefs.dc_voltage.name,
        ElectricalMeasurement.AttributeDefs.dc_current.name,
        ElectricalMeasurement.AttributeDefs.dc_power.name,
    )

    class MeasurementType(enum.IntFlag):
        """Measurement type bitmask names for state output compatibility."""

        ACTIVE_MEASUREMENT = 1
        REACTIVE_MEASUREMENT = 2
        APPARENT_MEASUREMENT = 4
        PHASE_A_MEASUREMENT = 8
        PHASE_B_MEASUREMENT = 16
        PHASE_C_MEASUREMENT = 32
        DC_MEASUREMENT = 64
        HARMONICS_MEASUREMENT = 128
        POWER_QUALITY_MEASUREMENT = 256

    _use_custom_polling: bool = False
    _attr_suggested_display_precision = 1
    _attr_max_attribute_name: str | None = None
    _divisor_attribute_name: str | None = None
    _multiplier_attribute_name: str | None = None
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this sensor."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._attr_extra_state_attribute_names: set[str] = {"measurement_type"}
        if self._attr_max_attribute_name is not None:
            self._attr_extra_state_attribute_names.add(self._attr_max_attribute_name)

    @property
    def state(self) -> dict[str, Any]:
        """Return the state for this sensor."""
        response = super().state
        if (
            measurement_type := self._cluster.get(
                ElectricalMeasurement.AttributeDefs.measurement_type.name
            )
        ) is not None:
            measurement_type_flags = self.MeasurementType(measurement_type)
            response["measurement_type"] = ", ".join(
                measurement.name
                for measurement in self.MeasurementType
                if measurement in measurement_type_flags
                and measurement.name is not None
            )

        if (max_attr_name := self._attr_max_attribute_name) is None:
            return response

        if (max_v := self._cluster.get(max_attr_name)) is not None:
            response[max_attr_name] = self.formatter(max_v)

        return response

    @property
    def _multiplier(self) -> int | float | None:
        if not self._multiplier_attribute_name:
            return super()._multiplier

        multiplier = self._cluster.get(self._multiplier_attribute_name)
        if multiplier is not None:
            return multiplier

        for fallback_attr in self.POWER_MULTIPLIER_FALLBACK_ATTRS.get(
            self._multiplier_attribute_name, ()
        ):
            fallback = self._cluster.get(fallback_attr)
            if fallback is not None:
                return fallback
        return 1

    @_multiplier.setter
    def _multiplier(self, value: int | float | None) -> None:
        raise AttributeError("Cannot set multiplier directly")

    @property
    def _divisor(self) -> int | float | None:
        if not self._divisor_attribute_name:
            return super()._divisor

        divisor = self._cluster.get(self._divisor_attribute_name)
        if divisor is not None:
            return divisor or 1

        for fallback_attr in self.POWER_DIVISOR_FALLBACK_ATTRS.get(
            self._divisor_attribute_name, ()
        ):
            fallback = self._cluster.get(fallback_attr)
            if fallback is not None:
                return fallback or 1
        return 1

    @_divisor.setter
    def _divisor(self, value: int | float | None) -> None:
        raise AttributeError("Cannot set divisor directly")


# this entity will be created by ReportingEM or PolledEM class below
class ElectricalMeasurementActivePower(BaseElectricalMeasurement):
    """Active power phase measurement."""

    _attribute_name = "active_power"
    # no unique id suffix for backwards compatibility
    # no translation key due to device class
    _attr_max_attribute_name = "active_power_max"
    _divisor_attribute_name = BaseElectricalMeasurement.AC_POWER_DIVISOR_ATTR
    _multiplier_attribute_name = BaseElectricalMeasurement.AC_POWER_MULTIPLIER_ATTR
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement: str = UnitOfPower.WATT


@register_entity(ElectricalMeasurement.cluster_id)
class ReportingElectricalMeasurement(ElectricalMeasurementActivePower):
    """Unpolled active power measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
        models=frozenset({"VZM31-SN", "SP 234", "outletv4", "INSPELNING Smart plug"}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class PolledElectricalMeasurement(ElectricalMeasurementActivePower):
    """Polled active power measurement that polls all relevant EM attributes."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _use_custom_polling: bool = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementActivePowerPhB(ElectricalMeasurementActivePower):
    """Active power phase B measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "active_power_ph_b"
    _unique_id_suffix = "active_power_ph_b"
    _attr_translation_key: str = "active_power_ph_b"
    _attr_max_attribute_name = "active_power_max_ph_b"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementActivePowerPhC(ElectricalMeasurementActivePower):
    """Active power phase C measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "active_power_ph_c"
    _unique_id_suffix = "active_power_ph_c"
    _attr_translation_key: str = "active_power_ph_c"
    _attr_max_attribute_name = "active_power_max_ph_c"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementTotalActivePower(ElectricalMeasurementActivePower):
    """Total active power measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "total_active_power"
    _unique_id_suffix = "total_active_power"
    _attr_translation_key: str = "total_active_power"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementApparentPower(BaseElectricalMeasurement):
    """Apparent power measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "apparent_power"
    _unique_id_suffix = "apparent_power"
    _divisor_attribute_name = BaseElectricalMeasurement.AC_POWER_DIVISOR_ATTR
    _multiplier_attribute_name = BaseElectricalMeasurement.AC_POWER_MULTIPLIER_ATTR
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.APPARENT_POWER
    _attr_native_unit_of_measurement = UnitOfApparentPower.VOLT_AMPERE

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSCurrent(BaseElectricalMeasurement):
    """RMS current measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "rms_current"
    _unique_id_suffix = "rms_current"
    _attr_max_attribute_name = "rms_current_max"
    _divisor_attribute_name = "ac_current_divisor"
    _multiplier_attribute_name = "ac_current_multiplier"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSCurrentPhB(ElectricalMeasurementRMSCurrent):
    """RMS current phase B measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "rms_current_ph_b"
    _unique_id_suffix = "rms_current_ph_b"
    _attr_translation_key: str = "rms_current_ph_b"
    _attr_max_attribute_name: str = "rms_current_max_ph_b"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSCurrentPhC(ElectricalMeasurementRMSCurrent):
    """RMS current phase C measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name: str = "rms_current_ph_c"
    _unique_id_suffix: str = "rms_current_ph_c"
    _attr_translation_key: str = "rms_current_ph_c"
    _attr_max_attribute_name: str = "rms_current_max_ph_c"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSVoltage(BaseElectricalMeasurement):
    """RMS Voltage measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "rms_voltage"
    _unique_id_suffix = "rms_voltage"
    _attr_max_attribute_name = "rms_voltage_max"
    _divisor_attribute_name = "ac_voltage_divisor"
    _multiplier_attribute_name = "ac_voltage_multiplier"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSVoltagePhB(ElectricalMeasurementRMSVoltage):
    """RMS voltage phase B measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "rms_voltage_ph_b"
    _unique_id_suffix = "rms_voltage_ph_b"
    _attr_translation_key: str = "rms_voltage_ph_b"
    _attr_max_attribute_name = "rms_voltage_max_ph_b"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementRMSVoltagePhC(ElectricalMeasurementRMSVoltage):
    """RMS voltage phase C measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "rms_voltage_ph_c"
    _unique_id_suffix = "rms_voltage_ph_c"
    _attr_translation_key: str = "rms_voltage_ph_c"
    _attr_max_attribute_name = "rms_voltage_max_ph_c"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementFrequency(BaseElectricalMeasurement):
    """Frequency measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "ac_frequency"
    _unique_id_suffix = "ac_frequency"
    _attr_translation_key: str = "ac_frequency"
    _attr_max_attribute_name = "ac_frequency_max"
    _divisor_attribute_name = "ac_frequency_divisor"
    _multiplier_attribute_name = "ac_frequency_multiplier"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.FREQUENCY
    _attr_native_unit_of_measurement = UnitOfFrequency.HERTZ

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementPowerFactor(BaseElectricalMeasurement):
    """Power Factor measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "power_factor"
    _unique_id_suffix = "power_factor"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.POWER_FACTOR
    _attr_native_unit_of_measurement = PERCENTAGE

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementPowerFactorPhB(ElectricalMeasurementPowerFactor):
    """Power factor phase B measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "power_factor_ph_b"
    _unique_id_suffix = "power_factor_ph_b"
    _attr_translation_key: str = "power_factor_ph_b"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementPowerFactorPhC(ElectricalMeasurementPowerFactor):
    """Power factor phase C measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "power_factor_ph_c"
    _unique_id_suffix = "power_factor_ph_c"
    _attr_translation_key: str = "power_factor_ph_c"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementDCVoltage(BaseElectricalMeasurement):
    """DC Voltage measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "dc_voltage"
    _unique_id_suffix = "dc_voltage"
    _attr_translation_key: str = "dc_voltage"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _divisor_attribute_name = "dc_voltage_divisor"
    _multiplier_attribute_name = "dc_voltage_multiplier"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementDCCurrent(BaseElectricalMeasurement):
    """DC Current measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "dc_current"
    _unique_id_suffix = "dc_current"
    _attr_translation_key: str = "dc_current"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.CURRENT
    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _divisor_attribute_name = "dc_current_divisor"
    _multiplier_attribute_name = "dc_current_multiplier"
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(ElectricalMeasurement.cluster_id)
class ElectricalMeasurementDCPower(BaseElectricalMeasurement):
    """DC Power measurement."""

    REPORT_CONFIG = {
        ElectricalMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_frequency.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.ac_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.active_power_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.apparent_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_divisor.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.power_multiplier.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_current_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_b.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.rms_voltage_ph_c.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: ElectricalMeasurement.AttributeDefs.total_active_power.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        ElectricalMeasurement.ep_attribute: {
            ElectricalMeasurement.AttributeDefs.ac_frequency_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_max.name: True,
            ElectricalMeasurement.AttributeDefs.ac_frequency_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.active_power_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_current_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_power_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_divisor.name: True,
            ElectricalMeasurement.AttributeDefs.dc_voltage_multiplier.name: True,
            ElectricalMeasurement.AttributeDefs.measurement_type.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.power_factor_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_current_max_ph_c.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_b.name: True,
            ElectricalMeasurement.AttributeDefs.rms_voltage_max_ph_c.name: True,
        },
    }
    _attribute_name = "dc_power"
    _unique_id_suffix = "dc_power"
    _attr_translation_key: str = "dc_power"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _divisor_attribute_name = BaseElectricalMeasurement.DC_POWER_DIVISOR_ATTR
    _multiplier_attribute_name = BaseElectricalMeasurement.DC_POWER_MULTIPLIER_ATTR
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_MEASUREMENT}),
    )


@register_entity(RelativeHumidity.cluster_id)
class Humidity(Sensor):
    """Humidity sensor."""

    REPORT_CONFIG = {
        RelativeHumidity.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: RelativeHumidity.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 100),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.HUMIDITY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _divisor = 100
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_HUMIDITY}),
    )


@register_entity(SMARTTHINGS_HUMIDITY_CLUSTER)
class SmartThingsHumidity(Sensor):
    """Humidity sensor."""

    REPORT_CONFIG = {
        "cluster_0xfc45": (
            {REPORT_CONFIG_ATTR: "measured_value", REPORT_CONFIG_CONFIG: (30, 900, 50)},
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.HUMIDITY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _divisor = 100
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({f"cluster_0x{SMARTTHINGS_HUMIDITY_CLUSTER:04x}"}),
    )


@register_entity(SoilMoistureCluster.cluster_id)
class SoilMoisture(Sensor):
    """Soil Moisture sensor."""

    REPORT_CONFIG = {
        SoilMoistureCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: SoilMoistureCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 100),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.HUMIDITY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_translation_key: str = "soil_moisture"
    _divisor = 100
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SOIL_MOISTURE}),
    )


@register_entity(LeafWetnessCluster.cluster_id)
class LeafWetness(Sensor):
    """Leaf Wetness sensor."""

    REPORT_CONFIG = {
        LeafWetnessCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: LeafWetnessCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 100),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.HUMIDITY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_translation_key: str = "leaf_wetness"
    _divisor = 100
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_LEAF_WETNESS}),
    )


@register_entity(IlluminanceMeasurement.cluster_id)
class Illuminance(Sensor):
    """Illuminance Sensor."""

    REPORT_CONFIG = {
        IlluminanceMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: IlluminanceMeasurement.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.ILLUMINANCE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = LIGHT_LUX
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ILLUMINANCE}),
    )

    def formatter(self, value: int) -> int | None:
        """Convert illumination data."""
        if self._is_non_value(value):
            return None
        if value == 0:
            return 0
        return round(pow(10, ((value - 1) / 10000)))


@dataclass(frozen=True, kw_only=True)
class SmartEnergyMeteringEntityDescription:
    """Dataclass that describes a Zigbee smart energy metering entity."""

    key: str = "instantaneous_demand"
    state_class: SensorStateClass | None = SensorStateClass.MEASUREMENT
    scale: int = 1
    native_unit_of_measurement: str | None = None
    device_class: SensorDeviceClass | None = None


@register_entity(Metering.cluster_id)
class SmartEnergyMetering(PollableSensor):
    """Metering sensor."""

    entity_description: SmartEnergyMeteringEntityDescription
    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling: bool = False
    _attribute_name = "instantaneous_demand"
    _attr_translation_key: str = "instantaneous_demand"
    _attr_extra_state_attribute_names: set[str] = {
        "device_type",
        "status",
        "zcl_unit_of_measurement",
    }
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
    )

    _ENTITY_DESCRIPTION_MAP = {
        0x00: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfPower.WATT,
            device_class=SensorDeviceClass.POWER,
        ),
        0x01: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
            device_class=None,  # volume flow rate is not supported yet
        ),
        0x02: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_FEET_PER_MINUTE,
            device_class=None,  # volume flow rate is not supported yet
        ),
        0x03: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
            device_class=None,  # volume flow rate is not supported yet
            scale=100,
        ),
        0x04: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=f"{UnitOfVolume.GALLONS}/{UnitOfTime.HOURS}",  # US gallons per hour
            device_class=None,  # volume flow rate is not supported yet
        ),
        0x05: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=f"IMP {UnitOfVolume.GALLONS}/{UnitOfTime.HOURS}",  # IMP gallons per hour
            device_class=None,  # needs to be None as imperial gallons are not supported
        ),
        0x06: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfPower.BTU_PER_HOUR,
            device_class=None,
            state_class=None,
        ),
        0x07: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=f"l/{UnitOfTime.HOURS}",
            device_class=None,  # volume flow rate is not supported yet
        ),
        0x08: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfPressure.KPA,
            device_class=SensorDeviceClass.PRESSURE,
        ),  # gauge
        0x09: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=UnitOfPressure.KPA,
            device_class=SensorDeviceClass.PRESSURE,
        ),  # absolute
        0x0A: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=f"{UnitOfVolume.CUBIC_FEET}/{UnitOfTime.HOURS}",  # cubic feet per hour
            device_class=None,  # volume flow rate is not supported yet
            scale=1000,
        ),
        0x0B: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement="unitless", device_class=None, state_class=None
        ),
        0x0C: SmartEnergyMeteringEntityDescription(
            native_unit_of_measurement=f"{UnitOfEnergy.MEGA_JOULE}/{UnitOfTime.SECONDS}",
            device_class=None,  # needs to be None as MJ/s is not supported
        ),
    }

    METERING_DEVICE_TYPES_ELECTRIC = {
        0,
        7,
        8,
        9,
        10,
        11,
        13,
        14,
        15,
        127,
        134,
        135,
        136,
        137,
        138,
        140,
        141,
        142,
    }
    METERING_DEVICE_TYPES_GAS = {1, 128}
    METERING_DEVICE_TYPES_WATER = {2, 129}
    METERING_DEVICE_TYPES_HEATING_COOLING = {3, 5, 6, 130, 132, 133}
    METERING_DEVICE_TYPE_MAP = {
        0: "Electric Metering",
        1: "Gas Metering",
        2: "Water Metering",
        3: "Thermal Metering",
        4: "Pressure Metering",
        5: "Heat Metering",
        6: "Cooling Metering",
        7: "End Use Measurement Device (EUMD) for metering electric vehicle charging",
        8: "PV Generation Metering",
        9: "Wind Turbine Generation Metering",
        10: "Water Turbine Generation Metering",
        11: "Micro Generation Metering",
        12: "Solar Hot Water Generation Metering",
        13: "Electric Metering Element/Phase 1",
        14: "Electric Metering Element/Phase 2",
        15: "Electric Metering Element/Phase 3",
        127: "Mirrored Electric Metering",
        128: "Mirrored Gas Metering",
        129: "Mirrored Water Metering",
        130: "Mirrored Thermal Metering",
        131: "Mirrored Pressure Metering",
        132: "Mirrored Heat Metering",
        133: "Mirrored Cooling Metering",
        134: "Mirrored End Use Measurement Device (EUMD) for metering electric vehicle charging",
        135: "Mirrored PV Generation Metering",
        136: "Mirrored Wind Turbine Generation Metering",
        137: "Mirrored Water Turbine Generation Metering",
        138: "Mirrored Micro Generation Metering",
        139: "Mirrored Solar Hot Water Generation Metering",
        140: "Mirrored Electric Metering Element/Phase 1",
        141: "Mirrored Electric Metering Element/Phase 2",
        142: "Mirrored Electric Metering Element/Phase 3",
    }

    class DeviceStatusElectric(enum.IntFlag):
        """Device status bitfield for electric meters."""

        NO_ALARMS = 0
        CHECK_METER = 1
        LOW_BATTERY = 2
        TAMPER_DETECT = 4
        POWER_FAILURE = 8
        POWER_QUALITY = 16
        LEAK_DETECT = 32
        SERVICE_DISCONNECT = 64
        RESERVED = 128

    class DeviceStatusGas(enum.IntFlag):
        """Device status bitfield for gas meters."""

        NO_ALARMS = 0
        CHECK_METER = 1
        LOW_BATTERY = 2
        TAMPER_DETECT = 4
        NOT_DEFINED = 8
        LOW_PRESSURE = 16
        LEAK_DETECT = 32
        SERVICE_DISCONNECT = 64
        REVERSE_FLOW = 128

    class DeviceStatusWater(enum.IntFlag):
        """Device status bitfield for water meters."""

        NO_ALARMS = 0
        CHECK_METER = 1
        LOW_BATTERY = 2
        TAMPER_DETECT = 4
        PIPE_EMPTY = 8
        LOW_PRESSURE = 16
        LEAK_DETECT = 32
        SERVICE_DISCONNECT = 64
        REVERSE_FLOW = 128

    class DeviceStatusHeatingCooling(enum.IntFlag):
        """Device status bitfield for heating/cooling meters."""

        NO_ALARMS = 0
        CHECK_METER = 1
        LOW_BATTERY = 2
        TAMPER_DETECT = 4
        TEMPERATURE_SENSOR = 8
        BURST_DETECT = 16
        LEAK_DETECT = 32
        SERVICE_DISCONNECT = 64
        REVERSE_FLOW = 128

    class DeviceStatusDefault(enum.IntFlag):
        """Fallback device status bitfield."""

        NO_ALARMS = 0

    def _unit_of_measurement(self) -> int | None:
        """Return metering cluster unit of measurement."""
        return self._cluster.get(Metering.AttributeDefs.unit_of_measure.name)

    def _metering_device_type(self) -> str | int | None:
        """Return metering device type."""
        if (
            metering_device_type := self._cluster.get(
                Metering.AttributeDefs.metering_device_type.name
            )
        ) is None:
            return None
        return self.METERING_DEVICE_TYPE_MAP.get(
            metering_device_type, metering_device_type
        )

    def _metering_status(self) -> enum.IntFlag | int | None:
        """Return metering status typed by metering device type."""
        if (status := self._cluster.get(Metering.AttributeDefs.status.name)) is None:
            return None
        metering_device_type = self._cluster.get(
            Metering.AttributeDefs.metering_device_type.name
        )
        if metering_device_type in self.METERING_DEVICE_TYPES_ELECTRIC:
            return self.DeviceStatusElectric(status)
        if metering_device_type in self.METERING_DEVICE_TYPES_GAS:
            return self.DeviceStatusGas(status)
        if metering_device_type in self.METERING_DEVICE_TYPES_WATER:
            return self.DeviceStatusWater(status)
        if metering_device_type in self.METERING_DEVICE_TYPES_HEATING_COOLING:
            return self.DeviceStatusHeatingCooling(status)
        return self.DeviceStatusDefault(status)

    def _demand_formatting(self) -> int | None:
        """Return demand formatting."""
        return self._cluster.get(Metering.AttributeDefs.demand_formatting.name)

    def _summation_formatting(self) -> int | None:
        """Return summation formatting."""
        return self._cluster.get(Metering.AttributeDefs.summation_formatting.name)

    def _is_supported(self) -> bool:
        unit = self._unit_of_measurement()
        if self._is_non_value(unit, attr_def=Metering.AttributeDefs.unit_of_measure):
            return False

        return super()._is_supported()

    def recompute_capabilities(self) -> None:
        """Recompute capabilities and feature flags."""
        super().recompute_capabilities()
        entity_description = self._ENTITY_DESCRIPTION_MAP.get(
            self._unit_of_measurement()
        )
        if entity_description is not None:
            self.entity_description = entity_description
            self._attr_device_class = entity_description.device_class
            self._attr_state_class = entity_description.state_class

    @property
    def state(self) -> dict[str, Any]:
        """Return state for this sensor."""
        response = super().state
        if (device_type := self._metering_device_type()) is not None:
            response["device_type"] = device_type
        if (status := self._metering_status()) is not None:
            if isinstance(status, enum.IntFlag):
                response["status"] = str(
                    status.name if status.name is not None else status.value
                )
            else:
                response["status"] = str(status)[len(status.__class__.__name__) + 1 :]
        response["zcl_unit_of_measurement"] = self._unit_of_measurement()
        return response

    @property
    def _multiplier(self) -> int | float | None:
        return self._cluster.get(Metering.AttributeDefs.multiplier.name) or 1

    @_multiplier.setter
    def _multiplier(self, value: int | float | None) -> None:
        raise AttributeError("Cannot set multiplier directly")

    @property
    def _divisor(self) -> int | float | None:
        return self._cluster.get(Metering.AttributeDefs.divisor.name) or 1

    @_divisor.setter
    def _divisor(self, value: int | float | None) -> None:
        raise AttributeError("Cannot set divisor directly")

    def formatter(self, value: int) -> int | float:
        """Metering formatter."""
        # TODO: improve typing for base class
        scaled_value = cast(float, super().formatter(value))

        if self._unit_of_measurement() == MeteringUnitofMeasure.Kwh_and_Kwh_binary:
            # Zigbee spec power unit is kW, but we show the value in W
            value_watt = scaled_value * 1000
            if value_watt < 100:
                return round(value_watt, 1)
            return round(value_watt)

        demand_formater = create_number_formatter(
            self._demand_formatting()
            if self._demand_formatting() is not None
            else DEFAULT_FORMATTING
        )
        return float(demand_formater.format(scaled_value))


@dataclass(frozen=True, kw_only=True)
class SmartEnergySummationEntityDescription(SmartEnergyMeteringEntityDescription):
    """Dataclass that describes a Zigbee smart energy summation entity."""

    key: str = "summation_delivered"
    state_class: SensorStateClass | None = SensorStateClass.TOTAL_INCREASING


@register_entity(Metering.cluster_id)
class SmartEnergySummation(SmartEnergyMetering):
    """Smart Energy Metering summation sensor."""

    entity_description: SmartEnergySummationEntityDescription
    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _attribute_name = "current_summ_delivered"
    _unique_id_suffix = "summation_delivered"
    _attr_translation_key: str = "summation_delivered"
    _attr_suggested_display_precision: int = 3

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 0),
    )

    _ENTITY_DESCRIPTION_MAP = {
        0x00: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
            device_class=SensorDeviceClass.ENERGY,
        ),
        0x01: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
            device_class=SensorDeviceClass.VOLUME,
        ),
        0x02: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
            device_class=SensorDeviceClass.VOLUME,
        ),
        0x03: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
            device_class=SensorDeviceClass.VOLUME,
            scale=100,
        ),
        0x04: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.GALLONS,  # US gallons
            device_class=SensorDeviceClass.VOLUME,
        ),
        0x05: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=f"IMP {UnitOfVolume.GALLONS}",
            device_class=None,  # needs to be None as imperial gallons are not supported
        ),
        0x06: SmartEnergySummationEntityDescription(
            native_unit_of_measurement="BTU", device_class=None, state_class=None
        ),
        0x07: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.LITERS,
            device_class=SensorDeviceClass.VOLUME,
        ),
        0x08: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfPressure.KPA,
            device_class=SensorDeviceClass.PRESSURE,
            state_class=SensorStateClass.MEASUREMENT,
        ),  # gauge
        0x09: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfPressure.KPA,
            device_class=SensorDeviceClass.PRESSURE,
            state_class=SensorStateClass.MEASUREMENT,
        ),  # absolute
        0x0A: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfVolume.CUBIC_FEET,
            device_class=SensorDeviceClass.VOLUME,
            scale=1000,
        ),
        0x0B: SmartEnergySummationEntityDescription(
            native_unit_of_measurement="unitless", device_class=None, state_class=None
        ),
        0x0C: SmartEnergySummationEntityDescription(
            native_unit_of_measurement=UnitOfEnergy.MEGA_JOULE,
            device_class=SensorDeviceClass.ENERGY,
        ),
    }

    def formatter(self, value: int) -> int | float:
        """Metering summation formatter."""
        # TODO: improve typing for base class
        scaled_value = cast(float, Sensor.formatter(self, value))

        if self._unit_of_measurement() == MeteringUnitofMeasure.Kwh_and_Kwh_binary:
            return scaled_value

        summation_formater = create_number_formatter(
            self._summation_formatting()
            if self._summation_formatting() is not None
            else DEFAULT_FORMATTING
        )
        return float(summation_formater.format(scaled_value))


@register_entity(Metering.cluster_id)
class PolledSmartEnergySummation(SmartEnergySummation):
    """Polled Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling: bool = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"TS011F", "ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier1SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 1 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier1_summ_delivered"
    _unique_id_suffix = "tier1_summation_delivered"
    _attr_translation_key: str = "tier1_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier2SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 2 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier2_summ_delivered"
    _unique_id_suffix = "tier2_summation_delivered"
    _attr_translation_key: str = "tier2_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier3SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 3 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier3_summ_delivered"
    _unique_id_suffix = "tier3_summation_delivered"
    _attr_translation_key: str = "tier3_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier4SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 4 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier4_summ_delivered"
    _unique_id_suffix = "tier4_summation_delivered"
    _attr_translation_key: str = "tier4_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier5SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 5 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier5_summ_delivered"
    _unique_id_suffix = "tier5_summation_delivered"
    _attr_translation_key: str = "tier5_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class Tier6SmartEnergySummation(PolledSmartEnergySummation):
    """Tier 6 Smart Energy Metering summation sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_tier6_summ_delivered"
    _unique_id_suffix = "tier6_summation_delivered"
    _attr_translation_key: str = "tier6_summation_delivered"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
        models=frozenset({"ZLinky_TIC", "TICMeter"}),
        feature_priority=(PlatformFeatureGroup.SMART_ENERGY_SUMMATION, 1),
    )


@register_entity(Metering.cluster_id)
class SmartEnergySummationReceived(PolledSmartEnergySummation):
    """Smart Energy Metering summation received sensor."""

    REPORT_CONFIG = {
        Metering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_summ_received.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier1_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier2_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier3_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier4_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier5_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.current_tier6_summ_delivered.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.instantaneous_demand.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_OP,
            },
            {
                REPORT_CONFIG_ATTR: Metering.AttributeDefs.status.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_ASAP,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        Metering.ep_attribute: {
            Metering.AttributeDefs.demand_formatting.name: True,
            Metering.AttributeDefs.divisor.name: True,
            Metering.AttributeDefs.metering_device_type.name: True,
            Metering.AttributeDefs.multiplier.name: True,
            Metering.AttributeDefs.summation_formatting.name: True,
            Metering.AttributeDefs.unit_of_measure.name: True,
        },
    }
    _use_custom_polling = False  # Poll indirectly by PolledSmartEnergySummation
    _attribute_name = "current_summ_received"
    _unique_id_suffix = "summation_received"
    _attr_translation_key: str = "summation_received"

    """
    This attribute only started to be initialized in HA 2024.2.0,
    so the entity would be created on the first HA start after the
    upgrade for existing devices, as the initialization to see if
    an attribute is unsupported happens later in the background.
    To avoid creating unnecessary entities for existing devices,
    wait until the attribute was properly initialized once for now.
    """
    _skip_creation_if_no_attr_cache = True

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_SMARTENERGY_METERING}),
    )


@register_entity(PressureMeasurement.cluster_id)
class Pressure(Sensor):
    """Pressure sensor."""

    REPORT_CONFIG = {
        PressureMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: PressureMeasurement.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.PRESSURE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision: int = 0
    _attr_native_unit_of_measurement = UnitOfPressure.HPA
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_PRESSURE}),
    )


@register_entity(FlowMeasurement.cluster_id)
class Flow(Sensor):
    """Flow Measurement sensor."""

    REPORT_CONFIG = {
        FlowMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: FlowMeasurement.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.VOLUME_FLOW_RATE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _divisor = 10
    _attr_native_unit_of_measurement = UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_FLOW}),
    )


@register_entity(TemperatureMeasurement.cluster_id)
class Temperature(Sensor):
    """Temperature Sensor."""

    REPORT_CONFIG = {
        TemperatureMeasurement.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: TemperatureMeasurement.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 50),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.TEMPERATURE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _divisor = 100
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_TEMPERATURE}),
    )


@register_entity(DeviceTemperatureCluster.cluster_id)
class DeviceTemperature(Sensor):
    """Device Temperature Sensor."""

    REPORT_CONFIG = {
        DeviceTemperatureCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: DeviceTemperatureCluster.AttributeDefs.current_temperature.name,
                REPORT_CONFIG_CONFIG: (30, 900, 50),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "current_temperature"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.TEMPERATURE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_translation_key: str = "device_temperature"
    _divisor = 100
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_DEVICE_TEMPERATURE}),
    )


@register_entity(INOVELLI_CLUSTER)
class InovelliInternalTemperature(Sensor):
    """Switch Internal Temperature Sensor."""

    _attribute_name = "internal_temp_monitor"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.TEMPERATURE
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_translation_key: str = "internal_temp_monitor"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_INOVELLI}),
    )


class InovelliOverheatedState(types.enum8):
    """Inovelli overheat protection state."""

    Normal = 0x00
    Overheated = 0x01


@register_entity(INOVELLI_CLUSTER)
class InovelliOverheated(EnumSensor):
    """Sensor that displays the overheat protection state."""

    _attribute_name = "overheated"
    _unique_id_suffix = "overheated"
    _attr_translation_key: str = "overheated"
    _enum = InovelliOverheatedState
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_INOVELLI}),
    )


@register_entity(CarbonDioxideConcentrationCluster.cluster_id)
class CarbonDioxideConcentration(Sensor):
    """Carbon Dioxide Concentration sensor."""

    REPORT_CONFIG = {
        CarbonDioxideConcentrationCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: CarbonDioxideConcentrationCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 1e-06),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.CO2
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _multiplier = 1e6
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"carbon_dioxide_concentration"}),
    )


@register_entity(CarbonMonoxideConcentrationCluster.cluster_id)
class CarbonMonoxideConcentration(Sensor):
    """Carbon Monoxide Concentration sensor."""

    REPORT_CONFIG = {
        CarbonMonoxideConcentrationCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: CarbonMonoxideConcentrationCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 1e-06),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.CO
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _multiplier = 1e6
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"carbon_monoxide_concentration"}),
    )


@register_entity(0x042E)
class VOCLevel(Sensor):
    """VOC Level sensor."""

    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _multiplier = 1e6
    _attr_native_unit_of_measurement = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"voc_level"}),
        feature_priority=(PlatformFeatureGroup.VOC_LEVEL, 0),
    )


@register_entity(0x042E)
class GenericVOCLevel(Sensor):
    """VOC Level sensor."""

    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _multiplier = 1e6
    _attr_native_unit_of_measurement = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"cluster_0x042e"}),
    )


@register_entity(0x042E)
class PPBVOCLevel(Sensor):
    """VOC Level sensor."""

    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = (
        SensorDeviceClass.VOLATILE_ORGANIC_COMPOUNDS_PARTS
    )
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _multiplier = 1
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_BILLION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"voc_level"}),
        models=frozenset({"lumi.airmonitor.acn01"}),
        feature_priority=(PlatformFeatureGroup.VOC_LEVEL, 1),
    )


@register_entity(PM25Cluster.cluster_id)
class PM25(Sensor):
    """Particulate Matter 2.5 microns or less sensor."""

    REPORT_CONFIG = {
        PM25Cluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: PM25Cluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 0.1),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.PM25
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _multiplier = 1
    _attr_native_unit_of_measurement = CONCENTRATION_MICROGRAMS_PER_CUBIC_METER
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"pm25"}),
    )


@register_entity(ElectricalConductivityCluster.cluster_id)
class ElectricalConductivity(Sensor):
    """Electrical Conductivity sensor."""

    REPORT_CONFIG = {
        ElectricalConductivityCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: ElectricalConductivityCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.CONDUCTIVITY
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = UnitOfConductivity.MICROSIEMENS_PER_CM

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_ELECTRICAL_CONDUCTIVITY}),
    )


@register_entity(FormaldehydeConcentrationCluster.cluster_id)
class FormaldehydeConcentration(Sensor):
    """Formaldehyde Concentration sensor."""

    REPORT_CONFIG = {
        FormaldehydeConcentrationCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: FormaldehydeConcentrationCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 1e-06),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_translation_key: str = "formaldehyde"
    _attr_suggested_display_precision = 0
    _multiplier = 1e6
    _attr_native_unit_of_measurement = CONCENTRATION_PARTS_PER_MILLION
    _attr_primary_weight = 1

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"formaldehyde_concentration"}),
    )


@register_entity(Thermostat.cluster_id)
class ThermostatHVACAction(Sensor):
    """Thermostat HVAC action sensor."""

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
    _unique_id_suffix = "hvac_action"
    _attr_translation_key: str = "hvac_action"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        feature_priority=(PlatformFeatureGroup.HVAC_ACTION, 0),
    )

    def _is_supported(self) -> bool:
        return PlatformEntity._is_supported(self)

    @property
    def state(self) -> dict:
        """Return the current HVAC action."""
        response = super().state
        if (
            self._cluster.get(Thermostat.AttributeDefs.pi_heating_demand.name) is None
            and self._cluster.get(Thermostat.AttributeDefs.pi_cooling_demand.name)
            is None
        ):
            response["state"] = self._rm_rs_action
        else:
            response["state"] = self._pi_demand_action
        return response

    @property
    def native_value(self) -> str | None:
        """Return the current HVAC action."""
        if (
            self._cluster.get(Thermostat.AttributeDefs.pi_heating_demand.name) is None
            and self._cluster.get(Thermostat.AttributeDefs.pi_cooling_demand.name)
            is None
        ):
            return self._rm_rs_action
        return self._pi_demand_action

    @property
    def _rm_rs_action(self) -> HVACAction | None:
        """Return the current HVAC action based on running mode and running state."""

        if (
            running_state := self._cluster.get(
                Thermostat.AttributeDefs.running_state.name
            )
        ) is None:
            return None

        rs_heat = (
            Thermostat.RunningState.Heat_State_On
            | Thermostat.RunningState.Heat_2nd_Stage_On
        )
        if running_state & rs_heat:
            return HVACAction.HEATING

        rs_cool = (
            Thermostat.RunningState.Cool_State_On
            | Thermostat.RunningState.Cool_2nd_Stage_On
        )
        if running_state & rs_cool:
            return HVACAction.COOLING

        running_state = self._cluster.get(Thermostat.AttributeDefs.running_state.name)
        if running_state and running_state & (
            Thermostat.RunningState.Fan_State_On
            | Thermostat.RunningState.Fan_2nd_Stage_On
            | Thermostat.RunningState.Fan_3rd_Stage_On
        ):
            return HVACAction.FAN

        running_state = self._cluster.get(Thermostat.AttributeDefs.running_state.name)
        if running_state and running_state & Thermostat.RunningState.Idle:
            return HVACAction.IDLE

        if (
            self._cluster.get(Thermostat.AttributeDefs.system_mode.name)
            != Thermostat.SystemMode.Off
        ):
            return HVACAction.IDLE
        return HVACAction.OFF

    @property
    def _pi_demand_action(self) -> HVACAction:
        """Return the current HVAC action based on pi_demands."""

        heating_demand = self._cluster.get(
            Thermostat.AttributeDefs.pi_heating_demand.name
        )
        if heating_demand is not None and heating_demand > 0:
            return HVACAction.HEATING
        cooling_demand = self._cluster.get(
            Thermostat.AttributeDefs.pi_cooling_demand.name
        )
        if cooling_demand is not None and cooling_demand > 0:
            return HVACAction.COOLING

        if (
            self._cluster.get(Thermostat.AttributeDefs.system_mode.name)
            != Thermostat.SystemMode.Off
        ):
            return HVACAction.IDLE
        return HVACAction.OFF


@register_entity(Thermostat.cluster_id)
class SinopeHVACAction(ThermostatHVACAction):
    """Sinope Thermostat HVAC action sensor."""

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
    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        manufacturers=frozenset({"Sinope Technologies"}),
        feature_priority=(PlatformFeatureGroup.HVAC_ACTION, 1),
    )

    @property
    def _rm_rs_action(self) -> HVACAction:
        """Return the current HVAC action based on running mode and running state."""

        running_mode = self._cluster.get(Thermostat.AttributeDefs.running_mode.name)
        if running_mode == Thermostat.RunningMode.Heat:
            return HVACAction.HEATING
        if running_mode == Thermostat.RunningMode.Cool:
            return HVACAction.COOLING

        running_state = self._cluster.get(Thermostat.AttributeDefs.running_state.name)
        if running_state and running_state & (
            Thermostat.RunningState.Fan_State_On
            | Thermostat.RunningState.Fan_2nd_Stage_On
            | Thermostat.RunningState.Fan_3rd_Stage_On
        ):
            return HVACAction.FAN
        if (
            self._cluster.get(Thermostat.AttributeDefs.system_mode.name)
            != Thermostat.SystemMode.Off
            and running_mode == Thermostat.SystemMode.Off
        ):
            return HVACAction.IDLE
        return HVACAction.OFF


@register_entity(Basic.cluster_id)
class RSSISensor(Sensor):
    """RSSI sensor for a device."""

    # TODO: migrate this away from `PlatformEntity`
    _unique_id_suffix: str = "rssi"
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_device_class: SensorDeviceClass | None = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement: str | None = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _attr_translation_key: str = "rssi"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_BASIC}),
    )

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init."""
        super().__init__(clusters, endpoint, device, **kwargs)

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.device.gateway.global_updater.register_update_listener(self.update)
        self._on_remove_callbacks.append(
            lambda: self.device.gateway.global_updater.remove_update_listener(
                self.update
            )
        )

    def _is_supported(self) -> bool:
        # This entity is not actually tied to an endpoint or cluster and will always be
        # supported
        return True

    def is_supported_in_list(self, entities: list[BaseEntity]) -> bool:
        """Check if the sensor is supported given the list of entities."""
        cls = type(self)
        return not any(type(entity) is cls for entity in entities)

    @property
    def state(self) -> dict:
        """Return the state of the sensor."""
        response = super().state
        response["state"] = self.device.device.rssi
        return response

    @property
    def native_value(self) -> str | int | float | None:
        """Return the state of the entity."""
        return self._device.device.rssi

    def enable(self) -> None:
        """Enable the entity."""
        super().enable()
        self._device.gateway.global_updater.register_update_listener(self.update)

    def disable(self) -> None:
        """Disable the entity."""
        super().disable()
        self._device.gateway.global_updater.remove_update_listener(self.update)

    def update(self):
        """Call async_update at a constrained random interval."""
        if self._device.available and self._device.gateway.config.allow_polling:
            self.debug("polling for updated state")
            self.maybe_emit_state_changed_event()
        else:
            self.debug(
                "skipping polling for updated state, available: %s, allow polled requests: %s",
                self._device.available,
                self._device.gateway.config.allow_polling,
            )


@register_entity(Basic.cluster_id)
class LQISensor(RSSISensor):
    """LQI sensor for a device."""

    # TODO: migrate this away from `PlatformEntity`
    _unique_id_suffix: str = "lqi"
    _attr_device_class = None
    _attr_native_unit_of_measurement = None
    _attr_translation_key = "lqi"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_BASIC}),
    )

    @property
    def state(self) -> dict:
        """Return the state of the sensor."""
        response = super().state
        response["state"] = self.device.device.lqi
        return response

    @property
    def native_value(self) -> str | int | float | None:
        """Return the state of the entity."""
        return self._device.device.lqi


@register_entity(TUYA_MANUFACTURER_CLUSTER)
class TimeLeft(Sensor):
    """Sensor that displays time left value."""

    _attribute_name = "timer_time_left"
    _unique_id_suffix = "time_left"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.DURATION
    _attr_translation_key: str = "timer_time_left"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"tuya_manufacturer"}),
        manufacturers=frozenset({"_TZE200_htnnfasr"}),
    )


@register_entity(IKEA_AIR_PURIFIER_CLUSTER)
class IkeaDeviceRunTime(Sensor):
    """Sensor that displays device run time (in minutes)."""

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
    _attribute_name = "device_run_time"
    _unique_id_suffix = "device_run_time"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.DURATION
    _attr_translation_key: str = "device_run_time"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"ikea_airpurifier"}),
    )


@register_entity(IKEA_AIR_PURIFIER_CLUSTER)
class IkeaFilterRunTime(Sensor):
    """Sensor that displays run time of the current filter (in minutes)."""

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
    _attribute_name = "filter_run_time"
    _unique_id_suffix = "filter_run_time"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.DURATION
    _attr_translation_key: str = "filter_run_time"
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category: EntityCategory = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"ikea_airpurifier"}),
    )


class AqaraFeedingSource(types.enum8):
    """Aqara pet feeder feeding source."""

    Feeder = 0x01
    HomeAssistant = 0x02


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederLastFeedingSource(EnumSensor):
    """Sensor that displays the last feeding source of pet feeder."""

    _attribute_name = "last_feeding_source"
    _unique_id_suffix = "last_feeding_source"
    _attr_translation_key: str = "last_feeding_source"
    _enum = AqaraFeedingSource

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"aqara.feeder.acn001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederLastFeedingSize(Sensor):
    """Sensor that displays the last feeding size of the pet feeder."""

    _attribute_name = "last_feeding_size"
    _unique_id_suffix = "last_feeding_size"
    _attr_translation_key: str = "last_feeding_size"

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"aqara.feeder.acn001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederPortionsDispensed(Sensor):
    """Sensor that displays the number of portions dispensed by the pet feeder."""

    _attribute_name = "portions_dispensed"
    _unique_id_suffix = "portions_dispensed"
    _attr_translation_key: str = "portions_dispensed_today"
    _attr_state_class: SensorStateClass = SensorStateClass.TOTAL_INCREASING

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"aqara.feeder.acn001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraPetFeederWeightDispensed(Sensor):
    """Sensor that displays the weight dispensed by the pet feeder."""

    _attribute_name = "weight_dispensed"
    _unique_id_suffix = "weight_dispensed"
    _attr_translation_key: str = "weight_dispensed_today"
    _attr_native_unit_of_measurement = UnitOfMass.GRAMS
    _attr_state_class: SensorStateClass = SensorStateClass.TOTAL_INCREASING

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"aqara.feeder.acn001"}),
    )


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraSmokeDensityDbm(Sensor):
    """Sensor that displays the smoke density of an Aqara smoke sensor in dB/m."""

    _attribute_name = "smoke_density_dbm"
    _unique_id_suffix = "smoke_density_dbm"
    _attr_translation_key: str = "smoke_density"
    _attr_native_unit_of_measurement = "dB/m"
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.sensor_smoke.acn03"}),
    )


class SonoffIlluminationStates(types.enum8):
    """Enum for displaying last Illumination state."""

    Dark = 0x00
    Light = 0x01


@register_entity(SONOFF_CLUSTER)
class SonoffPresenceSenorIlluminationStatus(EnumSensor):
    """Sensor that displays the illumination status the last time peresence was detected."""

    _attribute_name = "last_illumination_state"
    _unique_id_suffix = "last_illumination"
    _attr_translation_key: str = "last_illumination_state"
    _enum = SonoffIlluminationStates

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"sonoff_manufacturer"}),
        models=frozenset({"SNZB-06P"}),
    )


@register_entity(Thermostat.cluster_id)
class PiHeatingDemand(Sensor):
    """Sensor that displays the percentage of heating power demanded.

    Optional thermostat attribute.
    """

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
    _unique_id_suffix = "pi_heating_demand"
    _attribute_name = "pi_heating_demand"
    _attr_translation_key: str = "pi_heating_demand"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _attr_suggested_display_precision = 0
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
    )


class SetpointChangeSourceEnum(types.enum8):
    """The source of the setpoint change."""

    Manual = 0x00
    Schedule = 0x01
    External = 0x02


@register_entity(Thermostat.cluster_id)
class SetpointChangeSource(EnumSensor):
    """Sensor that displays the source of the setpoint change.

    Optional thermostat attribute.
    """

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
    _unique_id_suffix = "setpoint_change_source"
    _attribute_name = "setpoint_change_source"
    _attr_translation_key: str = "setpoint_change_source"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _enum = SetpointChangeSourceEnum

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class SetpointChangeSourceTimestamp(TimestampSensor):
    """Sensor that displays the timestamp the setpoint change.

    Optional thermostat attribute.
    """

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
    _unique_id_suffix = "setpoint_change_source_timestamp"
    _attribute_name = "setpoint_change_source_timestamp"
    _attr_translation_key: str = "setpoint_change_source_timestamp"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
    )

    def formatter(self, value: types.UTCTime) -> datetime:
        """Pass-through formatter."""
        return ZCL_EPOCH + timedelta(seconds=value)


@register_entity(WindowCovering.cluster_id)
class WindowCoveringTypeSensor(EnumSensor):
    """Sensor that displays the type of a cover device."""

    REPORT_CONFIG = {
        WindowCovering.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: WindowCovering.AttributeDefs.current_position_lift_percentage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
            {
                REPORT_CONFIG_ATTR: WindowCovering.AttributeDefs.current_position_tilt_percentage.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {
        WindowCovering.ep_attribute: {
            WindowCovering.AttributeDefs.config_status.name: True,
            WindowCovering.AttributeDefs.installed_closed_limit_lift.name: True,
            WindowCovering.AttributeDefs.installed_closed_limit_tilt.name: True,
            WindowCovering.AttributeDefs.installed_open_limit_lift.name: True,
            WindowCovering.AttributeDefs.installed_open_limit_tilt.name: True,
            WindowCovering.AttributeDefs.window_covering_mode.name: True,
            WindowCovering.AttributeDefs.window_covering_type.name: True,
        },
    }
    _attribute_name: str = WindowCovering.AttributeDefs.window_covering_type.name
    _enum = WindowCovering.WindowCoveringType
    _unique_id_suffix: str = "window_covering_type"
    _attr_translation_key: str = "window_covering_type"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_COVER}),
    )


@register_entity(Basic.cluster_id)
class AqaraCurtainMotorPowerSourceSensor(EnumSensor):
    """Sensor that displays the power source of the Aqara E1 curtain motor device."""

    _attribute_name: str = Basic.AttributeDefs.power_source.name
    _enum = Basic.PowerSource
    _unique_id_suffix: str = "power_source"
    _attr_translation_key: str = "power_source"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_BASIC}),
        models=frozenset({"lumi.curtain.agl001"}),
    )


class AqaraE1HookState(types.enum8):
    """Aqara hook state."""

    Unlocked = 0x00
    Locked = 0x01
    Locking = 0x02
    Unlocking = 0x03


@register_entity(AQARA_OPPLE_CLUSTER)
class AqaraCurtainHookStateSensor(EnumSensor):
    """Representation of a ZHA curtain mode configuration entity."""

    _attribute_name = "hooks_state"
    _enum = AqaraE1HookState
    _unique_id_suffix = "hooks_state"
    _attr_translation_key: str = "hooks_state"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({"opple_cluster"}),
        models=frozenset({"lumi.curtain.agl001"}),
    )


class BitMapSensor(Sensor):
    """A sensor with only state attributes.

    The sensor value will be an aggregate of the state attributes.
    """

    _bitmap: types.bitmap8 | types.bitmap16

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this sensor."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._attr_extra_state_attribute_names: set[str] = {
            bit.name for bit in list(self._bitmap)
        }

    @property
    def state(self) -> dict[str, Any]:
        """Return the state for this sensor."""
        response = super().state
        response["state"] = self.native_value
        value = self._cluster.get(self._attribute_name)
        for bit in list(self._bitmap):
            if value is None:
                response[bit.name] = False
            else:
                response[bit.name] = bit in self._bitmap(value)
        return response

    def formatter(self, _value: int) -> str:
        """Summary of all attributes."""

        value = self._cluster.get(self._attribute_name)
        state_attr = {}

        for bit in list(self._bitmap):
            if value is None:
                state_attr[bit.name] = False
            else:
                state_attr[bit.name] = bit in self._bitmap(value)

        binary_state_attributes = [key for (key, elem) in state_attr.items() if elem]

        return "something" if binary_state_attributes else "nothing"


@register_entity(Thermostat.cluster_id)
class DanfossOpenWindowDetection(EnumSensor):
    """Danfoss proprietary attribute.

    Sensor that displays whether the TRV detects an open window using the temperature sensor.
    """

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
    _unique_id_suffix = "open_window_detection"
    _attribute_name = "open_window_detection"
    _attr_translation_key: str = "open_window_detected"
    _enum = danfoss_thermostat.DanfossOpenWindowDetectionEnum

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossLoadEstimate(Sensor):
    """Danfoss proprietary attribute for communicating its estimate of the radiator load."""

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
    _unique_id_suffix = "load_estimate"
    _attribute_name = "load_estimate"
    _attr_translation_key: str = "load_estimate"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossAdaptationRunStatus(BitMapSensor):
    """Danfoss proprietary attribute for showing the status of the adaptation run."""

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
    _unique_id_suffix = "adaptation_run_status"
    _attribute_name = "adaptation_run_status"
    _attr_translation_key: str = "adaptation_run_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _bitmap = danfoss_thermostat.DanfossAdaptationRunStatusBitmap

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Thermostat.cluster_id)
class DanfossPreheatTime(Sensor):
    """Danfoss proprietary attribute for communicating the time when it starts pre-heating."""

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
    _unique_id_suffix = "preheat_time"
    _attribute_name = "preheat_time"
    _attr_translation_key: str = "preheat_time"
    _attr_entity_registry_enabled_default = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_THERMOSTAT}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Diagnostic.cluster_id)
class DanfossSoftwareErrorCode(BitMapSensor):
    """Danfoss proprietary attribute for communicating the error code."""

    REPORT_CONFIG = {
        Diagnostic.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: "motor_step_counter",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "sw_error_code",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _unique_id_suffix = "sw_error_code"
    _attribute_name = "sw_error_code"
    _attr_translation_key: str = "software_error"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _bitmap = danfoss_thermostat.DanfossSoftwareErrorCodeBitmap

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_DIAGNOSTIC}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(Diagnostic.cluster_id)
class DanfossMotorStepCounter(Sensor):
    """Danfoss proprietary attribute for communicating the motor step counter."""

    REPORT_CONFIG = {
        Diagnostic.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: "motor_step_counter",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
            {
                REPORT_CONFIG_ATTR: "sw_error_code",
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_DEFAULT,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _unique_id_suffix = "motor_step_counter"
    _attribute_name = "motor_step_counter"
    _attr_translation_key: str = "motor_stepcount"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_DIAGNOSTIC}),
        exposed_features=frozenset({DANFOSS_ALLY_THERMOSTAT}),
    )


@register_entity(WindSpeedCluster.cluster_id)
class WindSpeed(Sensor):
    """Wind Speed sensor."""

    REPORT_CONFIG = {
        WindSpeedCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: WindSpeedCluster.AttributeDefs.measured_value.name,
                REPORT_CONFIG_CONFIG: (30, 900, 0.01),
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attribute_name = "measured_value"
    _attr_device_class: SensorDeviceClass = SensorDeviceClass.WIND_SPEED
    _attr_state_class: SensorStateClass = SensorStateClass.MEASUREMENT
    _divisor = 100
    _attr_native_unit_of_measurement = UnitOfSpeed.METERS_PER_SECOND
    _attr_primary_weight = 2

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_HANDLER_WIND_SPEED}),
    )
