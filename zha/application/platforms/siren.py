"""Support for ZHA sirens."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from enum import IntFlag
import functools
from typing import TYPE_CHECKING, Any, Final

from zigpy.profiles import zha
from zigpy.zcl import Cluster
from zigpy.zcl.clusters.security import IasWd

from zha.application import Platform
from zha.application.const import (
    WARNING_DEVICE_MODE_BURGLAR,
    WARNING_DEVICE_MODE_EMERGENCY,
    WARNING_DEVICE_MODE_EMERGENCY_PANIC,
    WARNING_DEVICE_MODE_FIRE,
    WARNING_DEVICE_MODE_FIRE_PANIC,
    WARNING_DEVICE_MODE_POLICE_PANIC,
    WARNING_DEVICE_MODE_STOP,
    WARNING_DEVICE_SOUND_HIGH,
    WARNING_DEVICE_STROBE_HIGH,
    WARNING_DEVICE_STROBE_NO,
    Strobe,
)
from zha.application.helpers import safe_cluster_command
from zha.application.platforms import (
    BaseEntityInfo,
    ClusterMatch,
    PlatformEntity,
    register_entity,
)
from zha.zigbee.const import CLUSTER_IAS_WD

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint

DEFAULT_DURATION = 5  # seconds

ATTR_AVAILABLE_TONES: Final[str] = "available_tones"
ATTR_DURATION: Final[str] = "duration"
ATTR_VOLUME_LEVEL: Final[str] = "volume_level"
ATTR_TONE: Final[str] = "tone"


class SirenEntityFeature(IntFlag):
    """Supported features of the siren entity."""

    TURN_ON = 1
    TURN_OFF = 2
    TONES = 4
    VOLUME_SET = 8
    DURATION = 16


@dataclass(frozen=True, kw_only=True)
class SirenEntityInfo(BaseEntityInfo):
    """Siren entity info."""

    available_tones: dict[int, str]
    supported_features: SirenEntityFeature


@register_entity(IasWd.cluster_id)
class Siren(PlatformEntity):
    """Representation of a ZHA siren."""

    PLATFORM = Platform.SIREN
    REPORT_CONFIG = {}
    ZCL_INIT_ATTRS = {}
    _attr_fallback_name: str = "Siren"
    _attr_primary_weight = 4

    _cluster_match = ClusterMatch(
        in_clusters=frozenset({CLUSTER_IAS_WD}),
    )

    def __init__(
        self,
        clusters: list[Cluster],
        endpoint: Endpoint,
        device: Device,
        **kwargs: Any,
    ) -> None:
        """Init this siren."""
        self._cluster: Cluster = clusters[0]

        legacy_discovery_unique_id = (
            f"{endpoint.device.ieee}-{endpoint.id}"
            if (
                endpoint.zigpy_endpoint.device_type == zha.DeviceType.IAS_WARNING_DEVICE
            )
            else f"{endpoint.device.ieee}-{endpoint.id}-{int(IasWd.cluster_id)}"
        )

        super().__init__(
            clusters,
            endpoint,
            device,
            **kwargs,
            legacy_discovery_unique_id=legacy_discovery_unique_id,
        )
        self._attr_supported_features = (
            SirenEntityFeature.TURN_ON
            | SirenEntityFeature.TURN_OFF
            | SirenEntityFeature.DURATION
            | SirenEntityFeature.VOLUME_SET
            | SirenEntityFeature.TONES
        )
        self._attr_available_tones: dict[int, str] = {
            WARNING_DEVICE_MODE_BURGLAR: "Burglar",
            WARNING_DEVICE_MODE_FIRE: "Fire",
            WARNING_DEVICE_MODE_EMERGENCY: "Emergency",
            WARNING_DEVICE_MODE_POLICE_PANIC: "Police Panic",
            WARNING_DEVICE_MODE_FIRE_PANIC: "Fire Panic",
            WARNING_DEVICE_MODE_EMERGENCY_PANIC: "Emergency Panic",
        }
        self._attr_is_on: bool = False
        self._off_listener: asyncio.TimerHandle | None = None
        self._cached_tone: int = WARNING_DEVICE_MODE_EMERGENCY
        self._cached_level: int = WARNING_DEVICE_SOUND_HIGH
        self._cached_strobe: int = Strobe.No_Strobe
        self._cached_strobe_level: int = WARNING_DEVICE_STROBE_HIGH

    @staticmethod
    def _get_bit(value: int, bit: int) -> bool:
        """Get the specified bit from the value."""
        return (value & (1 << bit)) != 0

    @classmethod
    def _set_bit(
        cls,
        destination_value: int,
        destination_bit: int,
        source_value: int,
        source_bit: int,
    ) -> int:
        """Set a bit from source value into destination value."""
        if cls._get_bit(source_value, source_bit):
            return destination_value | (1 << destination_bit)
        return destination_value

    @classmethod
    def _build_start_warning_value(
        cls,
        *,
        mode: int,
        strobe: int,
        siren_level: int,
    ) -> int:
        """Create the IAS WD warning bitmask payload."""
        value = 0
        value = cls._set_bit(value, 0, siren_level, 0)
        value = cls._set_bit(value, 1, siren_level, 1)
        value = cls._set_bit(value, 2, strobe, 0)
        value = cls._set_bit(value, 4, mode, 0)
        value = cls._set_bit(value, 5, mode, 1)
        value = cls._set_bit(value, 6, mode, 2)
        value = cls._set_bit(value, 7, mode, 3)
        return value

    async def _issue_start_warning(
        self,
        *,
        mode: int,
        strobe: int,
        siren_level: int = WARNING_DEVICE_SOUND_HIGH,
        warning_duration: int = DEFAULT_DURATION,
        strobe_duty_cycle: int = 0,
        strobe_intensity: int = WARNING_DEVICE_STROBE_HIGH,
    ) -> None:
        """Issue IAS WD start warning command with legacy bit packing."""
        warning = self._build_start_warning_value(
            mode=mode,
            strobe=strobe,
            siren_level=siren_level,
        )
        await safe_cluster_command(
            self._cluster,
            "start_warning",
            warning,
            warning_duration,
            strobe_duty_cycle,
            strobe_intensity,
        )

    @functools.cached_property
    def info_object(self) -> SirenEntityInfo:
        """Return representation of the siren."""
        return SirenEntityInfo(
            **super().info_object.__dict__,
            available_tones=self._attr_available_tones,
            supported_features=self._attr_supported_features,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Get the state of the siren."""
        response = super().state
        response["state"] = self.is_on
        return response

    @property
    def supported_features(self) -> SirenEntityFeature:
        """Return supported features."""
        return self._attr_supported_features

    @property
    def is_on(self) -> bool:
        """Return true if the entity is on."""
        return self._attr_is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on siren."""
        if self._off_listener:
            self._off_listener.cancel()
            self._off_listener = None

        siren_tone = self._cached_tone
        siren_duration = DEFAULT_DURATION
        siren_level = self._cached_level
        should_strobe = self._cached_strobe
        strobe_level = self._cached_strobe_level
        if (duration := kwargs.get(ATTR_DURATION)) is not None:
            siren_duration = duration
        if (tone := kwargs.get(ATTR_TONE)) is not None:
            siren_tone = tone
        if (level := kwargs.get(ATTR_VOLUME_LEVEL)) is not None:
            siren_level = int(level)

        await self._issue_start_warning(
            mode=siren_tone,
            warning_duration=siren_duration,
            siren_level=siren_level,
            strobe=should_strobe,
            strobe_duty_cycle=50 if should_strobe else 0,
            strobe_intensity=strobe_level,
        )
        self._cached_tone = siren_tone
        self._cached_level = siren_level
        self._cached_strobe = should_strobe
        self._cached_strobe_level = strobe_level
        self._attr_is_on = True
        self._off_listener = asyncio.get_running_loop().call_later(
            siren_duration, self.async_set_off
        )
        self._tracked_handles.append(self._off_listener)
        self.maybe_emit_state_changed_event()

    async def async_turn_off(self, **kwargs: Any) -> None:  # pylint: disable=unused-argument
        """Turn off siren."""
        await self._issue_start_warning(
            mode=WARNING_DEVICE_MODE_STOP,
            strobe=WARNING_DEVICE_STROBE_NO,
        )
        self._attr_is_on = False
        self.maybe_emit_state_changed_event()

    def async_set_off(self) -> None:
        """Set is_on to False and write HA state."""
        self._attr_is_on = False
        if self._off_listener:
            self._off_listener.cancel()

            with contextlib.suppress(ValueError):
                self._tracked_handles.remove(self._off_listener)

            self._off_listener = None
        self.maybe_emit_state_changed_event()
