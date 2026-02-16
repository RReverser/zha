"""Static cluster policy tables used by ZHA runtime."""

from __future__ import annotations

from typing import Final

# Clusters that should be considered for device-to-device binding helpers.
BINDABLE_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        0x0006,  # OnOff
        0x0008,  # LevelControl
        0x000D,  # AnalogOutput
        0x0102,  # WindowCovering
        0x0300,  # ColorControl
    }
)

# Clusters that may not produce entities but still need lifecycle handling.
ENTITYLESS_CONFIGURE_REQUIRED_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        0x0000,  # Basic
        0x0502,  # IAS WD
        0x1000,  # LightLink
        0xEF00,  # Tuya manufacturer-specific
        0xFC00,  # Philips remote
        0xFC06,  # Philips contact
        0xFC11,  # Sonoff manufacturer-specific
        0xFC40,  # Legrand cable outlet
        0xFC7D,  # IKEA air purifier
        0xFC7F,  # IKEA shortcut v1
        0xFC80,  # IKEA remote
        0xFCC0,  # Aqara Opple
        0xFD00,  # Osram button
        0xFF01,  # Sinope manufacturer-specific
    }
)
