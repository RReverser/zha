"""Cluster naming/constants for entity-centric platform code."""

from typing import Final

REPORT_CONFIG_ATTR_PER_REQ: Final[int] = 3
REPORT_CONFIG_MAX_INT: Final[int] = 900
REPORT_CONFIG_MAX_INT_BATTERY_SAVE: Final[int] = 10800
REPORT_CONFIG_MIN_INT: Final[int] = 30
REPORT_CONFIG_MIN_INT_ASAP: Final[int] = 1
REPORT_CONFIG_MIN_INT_IMMEDIATE: Final[int] = 0
REPORT_CONFIG_MIN_INT_OP: Final[int] = 5
REPORT_CONFIG_MIN_INT_BATTERY_SAVE: Final[int] = 3600
REPORT_CONFIG_RPT_CHANGE: Final[int] = 1
REPORT_CONFIG_DEFAULT: tuple[int, int, int] = (
    REPORT_CONFIG_MIN_INT,
    REPORT_CONFIG_MAX_INT,
    REPORT_CONFIG_RPT_CHANGE,
)
REPORT_CONFIG_ASAP: tuple[int, int, int] = (
    REPORT_CONFIG_MIN_INT_ASAP,
    REPORT_CONFIG_MAX_INT,
    REPORT_CONFIG_RPT_CHANGE,
)
REPORT_CONFIG_BATTERY_SAVE: tuple[int, int, int] = (
    REPORT_CONFIG_MIN_INT_BATTERY_SAVE,
    REPORT_CONFIG_MAX_INT_BATTERY_SAVE,
    REPORT_CONFIG_RPT_CHANGE,
)
REPORT_CONFIG_IMMEDIATE: tuple[int, int, int] = (
    REPORT_CONFIG_MIN_INT_IMMEDIATE,
    REPORT_CONFIG_MAX_INT,
    REPORT_CONFIG_RPT_CHANGE,
)
REPORT_CONFIG_OP: tuple[int, int, int] = (
    REPORT_CONFIG_MIN_INT_OP,
    REPORT_CONFIG_MAX_INT,
    REPORT_CONFIG_RPT_CHANGE,
)
CLUSTER_READS_PER_REQ: Final[int] = 5

CLUSTER_ACCELEROMETER: Final[str] = "accelerometer"
CLUSTER_BINARY_INPUT: Final[str] = "binary_input"
CLUSTER_BINARY_OUTPUT: Final[str] = "binary_output"
CLUSTER_ANALOG_INPUT: Final[str] = "analog_input"
CLUSTER_ANALOG_OUTPUT: Final[str] = "analog_output"
CLUSTER_ATTRIBUTE: Final[str] = "attribute"
CLUSTER_BASIC: Final[str] = "basic"
CLUSTER_COLOR: Final[str] = "light_color"
CLUSTER_COVER: Final[str] = "window_covering"
CLUSTER_DIAGNOSTIC: Final[str] = "diagnostic"
CLUSTER_DEVICE_TEMPERATURE: Final[str] = "device_temperature"
CLUSTER_DOORLOCK: Final[str] = "door_lock"
CLUSTER_ELECTRICAL_CONDUCTIVITY: Final[str] = "electrical_conductivity"
CLUSTER_ELECTRICAL_MEASUREMENT: Final[str] = "electrical_measurement"
CLUSTER_EVENT_RELAY: Final[str] = "event_relay"
CLUSTER_FAN: Final[str] = "fan"
CLUSTER_FLOW: Final[str] = "flow"
CLUSTER_HUMIDITY: Final[str] = "humidity"
CLUSTER_HUE_OCCUPANCY: Final[str] = "philips_occupancy"
CLUSTER_SOIL_MOISTURE: Final[str] = "soil_moisture"
CLUSTER_LEAF_WETNESS: Final[str] = "leaf_wetness"
CLUSTER_IAS_ACE: Final[str] = "ias_ace"
CLUSTER_IAS_WD: Final[str] = "ias_wd"
CLUSTER_IDENTIFY: Final[str] = "identify"
CLUSTER_ILLUMINANCE: Final[str] = "illuminance"
CLUSTER_LEVEL: Final[str] = "level"
CLUSTER_MULTISTATE_INPUT: Final[str] = "multistate_input"
CLUSTER_OCCUPANCY: Final[str] = "occupancy"
CLUSTER_ON_OFF: Final[str] = "on_off"
CLUSTER_OTA: Final[str] = "ota"
CLUSTER_OTA_SERVER: Final[str] = "ota_server"
CLUSTER_POWER_CONFIGURATION: Final[str] = "power"
CLUSTER_PRESSURE: Final[str] = "pressure"
CLUSTER_SHADE: Final[str] = "shade"
CLUSTER_SMARTENERGY_METERING: Final[str] = "smartenergy_metering"
CLUSTER_TEMPERATURE: Final[str] = "temperature"
CLUSTER_THERMOSTAT: Final[str] = "thermostat"
CLUSTER_WIND_SPEED: Final[str] = "wind_speed"
CLUSTER_ZDO: Final[str] = "zdo"
CLUSTER_ZONE: Final[str] = "ias_zone"
ZONE: Final[str] = CLUSTER_ZONE
CLUSTER_INOVELLI: Final[str] = "inovelli_vzm31sn_cluster"

AQARA_OPPLE_CLUSTER: Final[int] = 0xFCC0
IKEA_AIR_PURIFIER_CLUSTER: Final[int] = 0xFC7D
IKEA_REMOTE_CLUSTER: Final[int] = 0xFC80
IKEA_SHORTCUT_V1_CLUSTER: Final[int] = 0xFC7F
INOVELLI_CLUSTER: Final[int] = 0xFC31
OSRAM_BUTTON_CLUSTER: Final[int] = 0xFD00
PHILIPS_CONTACT_CLUSTER: Final[int] = 0xFC06
PHILLIPS_REMOTE_CLUSTER: Final[int] = 0xFC00
SMARTTHINGS_ACCELERATION_CLUSTER: Final[int] = 0xFC02
SMARTTHINGS_HUMIDITY_CLUSTER: Final[int] = 0xFC45
SONOFF_CLUSTER: Final[int] = 0xFC11
TUYA_MANUFACTURER_CLUSTER: Final[int] = 0xEF00
VOC_LEVEL_CLUSTER: Final[int] = 0x042E
SINOPE_MANUFACTURER_CLUSTER: Final[int] = 0xFF01
LEGRAND_CABLE_OUTLET_CLUSTER: Final[int] = 0xFC40

CLUSTER_EVENT: Final[str] = "cluster_event"
CLUSTER_ATTRIBUTE_UPDATED: Final[str] = "cluster_attribute_updated"
CLUSTER_COMMAND_RECEIVED: Final[str] = "cluster_command_received"
CLUSTER_STATE_CHANGED: Final[str] = "cluster_state_changed"
CLUSTER_LEVEL_CHANGED: Final[str] = "cluster_level_changed"

ATTRIBUTE_ID: Final[str] = "attribute_id"
ATTRIBUTE_NAME: Final[str] = "attribute_name"
ATTRIBUTE_VALUE: Final[str] = "attribute_value"

UNIQUE_ID: Final[str] = "unique_id"
CLUSTER_ID: Final[str] = "cluster_id"
COMMAND: Final[str] = "command"
ARGS: Final[str] = "args"
PARAMS: Final[str] = "params"

SIGNAL_ATTR_UPDATED: Final[str] = "attribute_updated"
SIGNAL_MOVE_LEVEL: Final[str] = "move_level"
SIGNAL_REMOVE: Final[str] = "remove"
SIGNAL_SET_LEVEL: Final[str] = "set_level"
SIGNAL_STATE_ATTR: Final[str] = "update_state_attribute"
UNKNOWN: Final[str] = "unknown"
VALUE: Final[str] = "value"
