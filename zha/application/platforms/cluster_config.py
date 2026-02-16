"""Entity-centric cluster configuration primitives and merge logic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from zha.application.platforms.cluster_names import (
    CLUSTER_ACCELEROMETER,
    CLUSTER_ANALOG_INPUT,
    CLUSTER_ANALOG_OUTPUT,
    CLUSTER_BASIC,
    CLUSTER_BINARY_INPUT,
    CLUSTER_BINARY_OUTPUT,
    CLUSTER_COLOR,
    CLUSTER_COVER,
    CLUSTER_DEVICE_TEMPERATURE,
    CLUSTER_DIAGNOSTIC,
    CLUSTER_DOORLOCK,
    CLUSTER_ELECTRICAL_CONDUCTIVITY,
    CLUSTER_ELECTRICAL_MEASUREMENT,
    CLUSTER_FAN,
    CLUSTER_FLOW,
    CLUSTER_HUE_OCCUPANCY,
    CLUSTER_HUMIDITY,
    CLUSTER_IAS_ACE,
    CLUSTER_IAS_WD,
    CLUSTER_IDENTIFY,
    CLUSTER_ILLUMINANCE,
    CLUSTER_INOVELLI,
    CLUSTER_LEAF_WETNESS,
    CLUSTER_LEVEL,
    CLUSTER_OCCUPANCY,
    CLUSTER_ON_OFF,
    CLUSTER_OTA,
    CLUSTER_POWER_CONFIGURATION,
    CLUSTER_PRESSURE,
    CLUSTER_SHADE,
    CLUSTER_SMARTENERGY_METERING,
    CLUSTER_SOIL_MOISTURE,
    CLUSTER_TEMPERATURE,
    CLUSTER_WIND_SPEED,
    CLUSTER_ZONE,
    REPORT_CONFIG_ASAP,
    REPORT_CONFIG_BATTERY_SAVE,
    REPORT_CONFIG_DEFAULT,
    REPORT_CONFIG_IMMEDIATE,
    REPORT_CONFIG_OP,
)


@dataclass(frozen=True, slots=True)
class ClusterTarget:
    """Unique target cluster descriptor."""

    endpoint_id: int
    cluster_id: int
    is_client: bool


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    """Per-attribute reporting configuration."""

    attribute: str
    config: tuple[int, int, int | float]


@dataclass(frozen=True, slots=True)
class EntityClusterConfig:
    """Entity-owned cluster config contribution.

    Fields default to ``None`` so entity classes can override only the pieces
    they need while preserving current defaults.
    """

    bind: bool | None = None
    reporting: tuple[ReportingConfig, ...] | None = None
    init_attrs: dict[str, bool] | None = None


@dataclass(frozen=True, slots=True)
class ClusterConfigContribution:
    """Configuration contribution from one entity or quirk metadata."""

    target: ClusterTarget
    source: str
    order: int
    feature_priority: int
    explicit_quirk: bool = False
    bind: bool | None = None
    reporting: tuple[ReportingConfig, ...] = ()
    init_attrs: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MergedClusterConfig:
    """Merged cluster configuration for one target cluster."""

    bind: bool | None
    reporting: tuple[ReportingConfig, ...]
    init_attrs: dict[str, bool]


def _report(attribute: str, config: tuple[int, int, int | float]) -> ReportingConfig:
    """Build a reporting config helper object."""
    return ReportingConfig(attribute=attribute, config=config)


# Cluster names without exported constants in `cluster_names.py`.
_CLUSTER_NAME_CARBON_DIOXIDE = "carbon_dioxide_concentration"
_CLUSTER_NAME_CARBON_MONOXIDE = "carbon_monoxide_concentration"
_CLUSTER_NAME_CLUSTER_0X042E = "cluster_0x042e"
_CLUSTER_NAME_CLUSTER_0XFC45 = "cluster_0xfc45"
_CLUSTER_NAME_FORMALDEHYDE = "formaldehyde_concentration"
_CLUSTER_NAME_IKEA_AIRPURIFIER = "ikea_airpurifier"
_CLUSTER_NAME_ILLUMINANCE_LEVEL = "illuminance_level"
_CLUSTER_NAME_ALARMS = "alarms"
_CLUSTER_NAME_ANALOG_VALUE = "analog_value"
_CLUSTER_NAME_APPLIANCE_CONTROL = "appliance_control"
_CLUSTER_NAME_BINARY_VALUE = "binary_value"
_CLUSTER_NAME_COMMISSIONING = "commissioning"
_CLUSTER_NAME_MULTISTATE_INPUT = "multistate_input"
_CLUSTER_NAME_MULTISTATE_OUTPUT = "multistate_output"
_CLUSTER_NAME_MULTISTATE_VALUE = "multistate_value"
_CLUSTER_NAME_ON_OFF_CONFIG = "on_off_config"
_CLUSTER_NAME_OPPLE = "opple_cluster"
_CLUSTER_NAME_PARTITION = "partition"
_CLUSTER_NAME_PM25 = "pm25"
_CLUSTER_NAME_POWER_PROFILE = "power_profile"
_CLUSTER_NAME_SCENES = "scenes"
_CLUSTER_NAME_SINOPE = "sinope_manufacturer_specific"
_CLUSTER_NAME_SONOFF = "sonoff_manufacturer"
_CLUSTER_NAME_THERMOSTAT = "thermostat"
_CLUSTER_NAME_THERMOSTAT_UI = "thermostat_ui"
_CLUSTER_NAME_TUYA = "tuya_manufacturer"
_CLUSTER_NAME_VOC_LEVEL = "voc_level"


# Entity-centric defaults migrated from the previous cluster runtime.
# Keys are `(cluster_name, is_client)`.
_DEFAULT_ENTITY_CLUSTER_CONFIGS: dict[tuple[str, bool], EntityClusterConfig] = {
    (CLUSTER_ACCELEROMETER, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("acceleration", REPORT_CONFIG_ASAP),
            _report("x_axis", REPORT_CONFIG_ASAP),
            _report("y_axis", REPORT_CONFIG_ASAP),
            _report("z_axis", REPORT_CONFIG_ASAP),
        ),
        init_attrs={},
    ),
    (CLUSTER_ANALOG_INPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={
            "description": True,
            "max_present_value": True,
            "min_present_value": True,
            "out_of_service": True,
            "reliability": True,
            "resolution": True,
            "status_flags": True,
            "engineering_units": True,
            "application_type": True,
        },
    ),
    (CLUSTER_ANALOG_OUTPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={
            "min_present_value": True,
            "max_present_value": True,
            "resolution": True,
            "relinquish_default": True,
            "description": True,
            "engineering_units": True,
            "application_type": True,
        },
    ),
    (CLUSTER_BASIC, False): EntityClusterConfig(
        bind=False,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_SCENES, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_ON_OFF_CONFIG, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_ALARMS, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_BINARY_INPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={"description": True},
    ),
    (CLUSTER_BINARY_OUTPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={"description": True},
    ),
    (_CLUSTER_NAME_ANALOG_VALUE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_BINARY_VALUE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_MULTISTATE_INPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_MULTISTATE_OUTPUT, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_MULTISTATE_VALUE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("present_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_COMMISSIONING, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_PARTITION, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_POWER_PROFILE, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_APPLIANCE_CONTROL, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_COLOR, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("current_x", REPORT_CONFIG_DEFAULT),
            _report("current_y", REPORT_CONFIG_DEFAULT),
            _report("color_temperature", REPORT_CONFIG_DEFAULT),
        ),
        init_attrs={
            "color_mode": False,
            "color_temp_physical_min": True,
            "color_temp_physical_max": True,
            "color_capabilities": True,
            "color_loop_active": False,
            "start_up_color_temperature": True,
            "options": True,
        },
    ),
    (CLUSTER_COVER, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("current_position_lift_percentage", REPORT_CONFIG_IMMEDIATE),
            _report("current_position_tilt_percentage", REPORT_CONFIG_IMMEDIATE),
        ),
        init_attrs={
            "window_covering_type": True,
            "window_covering_mode": True,
            "config_status": True,
            "installed_closed_limit_lift": True,
            "installed_closed_limit_tilt": True,
            "installed_open_limit_lift": True,
            "installed_open_limit_tilt": True,
        },
    ),
    (CLUSTER_DEVICE_TEMPERATURE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("current_temperature", (30, 900, 50)),),
        init_attrs={},
    ),
    (CLUSTER_DIAGNOSTIC, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_DOORLOCK, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("lock_state", REPORT_CONFIG_IMMEDIATE),),
        init_attrs={},
    ),
    (CLUSTER_ELECTRICAL_CONDUCTIVITY, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (CLUSTER_ELECTRICAL_MEASUREMENT, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("ac_voltage_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("ac_voltage_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("ac_current_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("ac_current_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("ac_power_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("ac_power_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("power_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("power_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("active_power", REPORT_CONFIG_OP),
            _report("active_power_ph_b", REPORT_CONFIG_OP),
            _report("active_power_ph_c", REPORT_CONFIG_OP),
            _report("total_active_power", REPORT_CONFIG_OP),
            _report("apparent_power", REPORT_CONFIG_OP),
            _report("rms_current", REPORT_CONFIG_OP),
            _report("rms_current_ph_b", REPORT_CONFIG_OP),
            _report("rms_current_ph_c", REPORT_CONFIG_OP),
            _report("rms_voltage", REPORT_CONFIG_OP),
            _report("rms_voltage_ph_b", REPORT_CONFIG_OP),
            _report("rms_voltage_ph_c", REPORT_CONFIG_OP),
            _report("ac_frequency", REPORT_CONFIG_OP),
            _report("dc_voltage_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("dc_voltage_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("dc_current_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("dc_current_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("dc_power_multiplier", REPORT_CONFIG_IMMEDIATE),
            _report("dc_power_divisor", REPORT_CONFIG_IMMEDIATE),
            _report("dc_voltage", REPORT_CONFIG_OP),
            _report("dc_current", REPORT_CONFIG_OP),
            _report("dc_power", REPORT_CONFIG_OP),
        ),
        init_attrs={
            "ac_frequency_divisor": True,
            "ac_frequency_max": True,
            "ac_frequency_multiplier": True,
            "active_power_max": True,
            "active_power_max_ph_b": True,
            "active_power_max_ph_c": True,
            "measurement_type": True,
            "power_factor": True,
            "power_factor_ph_b": True,
            "power_factor_ph_c": True,
            "rms_current_max": True,
            "rms_current_max_ph_b": True,
            "rms_current_max_ph_c": True,
            "rms_voltage_max": True,
            "rms_voltage_max_ph_b": True,
            "rms_voltage_max_ph_c": True,
            "dc_voltage_divisor": True,
            "dc_voltage_multiplier": True,
            "dc_current_divisor": True,
            "dc_current_multiplier": True,
            "dc_power_divisor": True,
            "dc_power_multiplier": True,
        },
    ),
    (CLUSTER_FAN, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("fan_mode", REPORT_CONFIG_OP),),
        init_attrs={"fan_mode_sequence": True},
    ),
    (_CLUSTER_NAME_THERMOSTAT, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("local_temperature", (30, 900, 25)),
            _report("occupied_cooling_setpoint", (30, 900, 25)),
            _report("occupied_heating_setpoint", (30, 900, 25)),
            _report("unoccupied_cooling_setpoint", (30, 900, 25)),
            _report("unoccupied_heating_setpoint", (30, 900, 25)),
            _report("running_mode", (30, 900, 25)),
            _report("running_state", (30, 900, 5)),
            _report("system_mode", (30, 900, 25)),
            _report("occupancy", (30, 900, 1)),
            _report("pi_cooling_demand", (30, 900, 5)),
            _report("pi_heating_demand", (30, 900, 5)),
        ),
        init_attrs={
            "abs_min_heat_setpoint_limit": True,
            "abs_max_heat_setpoint_limit": True,
            "abs_min_cool_setpoint_limit": True,
            "abs_max_cool_setpoint_limit": True,
            "ctrl_sequence_of_oper": False,
            "max_cool_setpoint_limit": True,
            "max_heat_setpoint_limit": True,
            "min_cool_setpoint_limit": True,
            "min_heat_setpoint_limit": True,
            "local_temperature_calibration": True,
            "setpoint_change_source": True,
        },
    ),
    (CLUSTER_FLOW, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (CLUSTER_HUE_OCCUPANCY, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("occupancy", REPORT_CONFIG_IMMEDIATE),),
        init_attrs={},
    ),
    (CLUSTER_HUMIDITY, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 100)),),
        init_attrs={},
    ),
    (CLUSTER_IAS_ACE, True): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_IAS_WD, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_IDENTIFY, False): EntityClusterConfig(
        bind=False,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_ILLUMINANCE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_ILLUMINANCE_LEVEL, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("level_status", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (CLUSTER_INOVELLI, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_LEAF_WETNESS, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 100)),),
        init_attrs={},
    ),
    (CLUSTER_LEVEL, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("current_level", REPORT_CONFIG_ASAP),),
        init_attrs={
            "on_off_transition_time": True,
            "on_level": True,
            "on_transition_time": True,
            "off_transition_time": True,
            "default_move_rate": True,
            "start_up_current_level": True,
        },
    ),
    (CLUSTER_OCCUPANCY, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("occupancy", REPORT_CONFIG_IMMEDIATE),),
        init_attrs={},
    ),
    (CLUSTER_ON_OFF, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("on_off", REPORT_CONFIG_IMMEDIATE),),
        init_attrs={"start_up_on_off": True},
    ),
    (CLUSTER_ON_OFF, True): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_OTA, False): EntityClusterConfig(
        bind=False,
        reporting=(),
        init_attrs={"current_file_version": True},
    ),
    (CLUSTER_OTA, True): EntityClusterConfig(
        bind=False,
        reporting=(),
        init_attrs={"current_file_version": True},
    ),
    (CLUSTER_POWER_CONFIGURATION, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("battery_voltage", REPORT_CONFIG_BATTERY_SAVE),
            _report("battery_percentage_remaining", REPORT_CONFIG_BATTERY_SAVE),
        ),
        init_attrs={},
    ),
    (CLUSTER_PRESSURE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", REPORT_CONFIG_DEFAULT),),
        init_attrs={},
    ),
    (CLUSTER_SHADE, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (CLUSTER_SMARTENERGY_METERING, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("instantaneous_demand", REPORT_CONFIG_OP),
            _report("current_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier1_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier2_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier3_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier4_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier5_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_tier6_summ_delivered", REPORT_CONFIG_DEFAULT),
            _report("current_summ_received", REPORT_CONFIG_DEFAULT),
            _report("status", REPORT_CONFIG_ASAP),
        ),
        init_attrs={
            "demand_formatting": True,
            "divisor": True,
            "metering_device_type": True,
            "multiplier": True,
            "summation_formatting": True,
            "unit_of_measure": True,
        },
    ),
    (CLUSTER_SOIL_MOISTURE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 100)),),
        init_attrs={},
    ),
    (CLUSTER_TEMPERATURE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 50)),),
        init_attrs={},
    ),
    (CLUSTER_WIND_SPEED, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 0.01)),),
        init_attrs={},
    ),
    (CLUSTER_ZONE, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={"zone_status": False, "zone_state": True, "zone_type": True},
    ),
    (_CLUSTER_NAME_CARBON_DIOXIDE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 1e-06)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_CARBON_MONOXIDE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 1e-06)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_CLUSTER_0X042E, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_CLUSTER_0XFC45, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 50)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_FORMALDEHYDE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 1e-06)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_IKEA_AIRPURIFIER, False): EntityClusterConfig(
        bind=True,
        reporting=(
            _report("filter_run_time", REPORT_CONFIG_DEFAULT),
            _report("replace_filter", REPORT_CONFIG_IMMEDIATE),
            _report("filter_life_time", REPORT_CONFIG_DEFAULT),
            _report("disable_led", REPORT_CONFIG_IMMEDIATE),
            _report("air_quality_25pm", REPORT_CONFIG_IMMEDIATE),
            _report("child_lock", REPORT_CONFIG_IMMEDIATE),
            _report("fan_mode", REPORT_CONFIG_IMMEDIATE),
            _report("fan_speed", REPORT_CONFIG_IMMEDIATE),
            _report("device_run_time", REPORT_CONFIG_DEFAULT),
        ),
        init_attrs={},
    ),
    (_CLUSTER_NAME_OPPLE, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_PM25, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("measured_value", (30, 900, 0.1)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_SINOPE, False): EntityClusterConfig(
        bind=True,
        reporting=(_report("action_report", (0, 0, 1)),),
        init_attrs={},
    ),
    (_CLUSTER_NAME_SONOFF, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_THERMOSTAT_UI, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={"keypad_lockout": True},
    ),
    (_CLUSTER_NAME_TUYA, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
    (_CLUSTER_NAME_VOC_LEVEL, False): EntityClusterConfig(
        bind=True,
        reporting=(),
        init_attrs={},
    ),
}


def get_default_entity_cluster_config(
    cluster_name: str,
    *,
    is_client: bool,
) -> EntityClusterConfig | None:
    """Return copied default entity config for a cluster ref."""
    config = _DEFAULT_ENTITY_CLUSTER_CONFIGS.get((cluster_name, is_client))
    if config is None:
        return None
    return EntityClusterConfig(
        bind=config.bind,
        reporting=tuple(config.reporting) if config.reporting is not None else None,
        init_attrs=dict(config.init_attrs) if config.init_attrs is not None else None,
    )


def entity_cluster_configs_from_refs(
    *cluster_refs: tuple[str, bool],
) -> dict[str | tuple[str, bool], EntityClusterConfig]:
    """Build class-level entity config declarations from cluster refs."""
    configs: dict[str | tuple[str, bool], EntityClusterConfig] = {}
    for cluster_name, is_client in cluster_refs:
        if config := get_default_entity_cluster_config(
            cluster_name, is_client=is_client
        ):
            configs[(cluster_name, is_client)] = config
    return configs


def _is_more_demanding(current: ReportingConfig, candidate: ReportingConfig) -> bool:
    """Return True if candidate is a stricter reporting config than current."""

    cur_min, cur_max, cur_change = current.config
    cand_min, cand_max, cand_change = candidate.config

    # Smaller intervals and smaller reportable change are more demanding.
    if cand_min != cur_min:
        return cand_min < cur_min
    if cand_max != cur_max:
        return cand_max < cur_max

    try:
        if cand_change != cur_change:
            return float(cand_change) < float(cur_change)
    except (TypeError, ValueError):
        return False

    return False


class ClusterConfigMerger:
    """Merge cluster configuration contributions from entities and quirks."""

    def __init__(self) -> None:
        """Initialize the merger state."""
        self._contributions: defaultdict[
            ClusterTarget, list[ClusterConfigContribution]
        ] = defaultdict(list)

    def reset(self) -> None:
        """Clear all accumulated contributions."""
        self._contributions.clear()

    def add(self, contribution: ClusterConfigContribution) -> None:
        """Add a contribution."""
        self._contributions[contribution.target].append(contribution)

    def merge(self) -> dict[ClusterTarget, MergedClusterConfig]:
        """Merge all contributions by target cluster.

        Merge algorithm order:
        1. Primary contribution baseline by highest feature priority.
        2. Explicit quirk override.
        3. Most demanding merge for remaining conflicts.
        4. First-match tie breaker.
        """
        merged: dict[ClusterTarget, MergedClusterConfig] = {}

        for target, contributions in self._contributions.items():
            if not contributions:
                continue

            ordered = sorted(contributions, key=lambda conf: conf.order)

            non_quirk = [conf for conf in ordered if not conf.explicit_quirk]
            if non_quirk:
                highest_priority = max(conf.feature_priority for conf in non_quirk)
                primary = next(
                    conf
                    for conf in non_quirk
                    if conf.feature_priority == highest_priority
                )
            else:
                primary = ordered[0]

            bind = self._merge_bind(primary, ordered)
            reporting = self._merge_reporting(primary, ordered)
            init_attrs = self._merge_init_attrs(primary, ordered)

            merged[target] = MergedClusterConfig(
                bind=bind,
                reporting=tuple(reporting[attr] for attr in sorted(reporting)),
                init_attrs=init_attrs,
            )

        return merged

    def _merge_bind(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> bool | None:
        """Merge bind config for a cluster."""
        bind_candidates = [conf for conf in ordered if conf.bind is not None]
        if not bind_candidates:
            return None

        selected: bool | None = (
            primary.bind if primary.bind is not None else bind_candidates[0].bind
        )

        quirk_candidates = [
            conf
            for conf in bind_candidates
            if conf.explicit_quirk and conf.bind is not None
        ]
        if quirk_candidates:
            # Binding is "most-demanding": if any quirk contribution requires
            # binding, we keep it enabled even if other quirk contributions are
            # initialization-only and set `bind=False`.
            if any(conf.bind for conf in quirk_candidates):
                return True
            return quirk_candidates[-1].bind

        if any(conf.bind for conf in bind_candidates):
            return True

        return selected

    def _merge_reporting(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> dict[str, ReportingConfig]:
        """Merge reporting config for a cluster by attribute."""
        by_attr: defaultdict[str, list[tuple[int, ReportingConfig, bool]]] = (
            defaultdict(list)
        )

        for conf in ordered:
            for report in conf.reporting:
                by_attr[report.attribute].append(
                    (conf.order, report, conf.explicit_quirk)
                )

        merged: dict[str, ReportingConfig] = {}
        primary_by_attr = {rep.attribute: rep for rep in primary.reporting}

        for attr, candidates in by_attr.items():
            candidates.sort(key=lambda item: item[0])
            selected = primary_by_attr.get(attr, candidates[0][1])

            quirk_override = [rep for _o, rep, is_quirk in candidates if is_quirk]
            if quirk_override:
                merged[attr] = quirk_override[-1]
                continue

            most_demanding = selected
            for _order, candidate, _is_quirk in candidates:
                if _is_more_demanding(most_demanding, candidate):
                    most_demanding = candidate

            merged[attr] = most_demanding

        return merged

    def _merge_init_attrs(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> dict[str, bool]:
        """Merge initialization attributes by attribute name."""
        attr_values: defaultdict[str, list[tuple[int, bool, bool]]] = defaultdict(list)

        for conf in ordered:
            for attr_name, from_cache in conf.init_attrs.items():
                attr_values[attr_name].append(
                    (conf.order, from_cache, conf.explicit_quirk)
                )

        merged: dict[str, bool] = {}

        for attr, candidates in attr_values.items():
            candidates.sort(key=lambda item: item[0])

            selected = (
                primary.init_attrs[attr]
                if attr in primary.init_attrs
                else candidates[0][1]
            )

            quirk_override = [
                from_cache for _o, from_cache, is_quirk in candidates if is_quirk
            ]
            if quirk_override:
                merged[attr] = quirk_override[-1]
                continue

            # False means "uncached read required" and is more demanding.
            if any(from_cache is False for _o, from_cache, _q in candidates):
                merged[attr] = False
            else:
                merged[attr] = selected

        return merged


def cluster_target_from_cluster(cluster: Any) -> ClusterTarget:
    """Build a target descriptor for a zigpy cluster instance."""

    return ClusterTarget(
        endpoint_id=cluster.endpoint.endpoint_id,
        cluster_id=cluster.cluster_id,
        is_client=cluster.is_client,
    )
