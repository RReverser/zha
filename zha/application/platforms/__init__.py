"""Platform module for Zigbee Home Automation."""

from __future__ import annotations

from abc import abstractmethod
import asyncio
from collections import defaultdict
from collections.abc import Callable, Sequence
from contextlib import suppress
import dataclasses
from enum import StrEnum
import functools
from functools import cached_property
import logging
from typing import TYPE_CHECKING, Any, Final, Literal, cast, final

from zigpy.profiles import zha, zll
from zigpy.quirks.v2 import EntityMetadata, EntityType
from zigpy.types import ClusterId
from zigpy.types.named import EUI64
from zigpy.typing import UNDEFINED, UndefinedType
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
)

from zha.application import Platform
from zha.application.const import UniqueIdMigration
from zha.application.platforms.cluster_config import (
    EntityClusterConfig,
    entity_cluster_configs_from_refs,
)
from zha.const import STATE_CHANGED
from zha.debounce import Debouncer
from zha.event import EventBase
from zha.mixins import LogMixin
from zha.zigbee.cluster_events import ClusterAttributeUpdatedEvent, ClusterCommandEvent
from zha.zigbee.cluster_io import (
    get_attribute_value as cluster_get_attribute_value,
    get_attributes as cluster_get_attributes,
    retryable_cluster_call,
    write_attributes_safe as cluster_write_attributes_safe,
)

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint
    from zha.zigbee.group import Group


_LOGGER = logging.getLogger(__name__)

DEFAULT_UPDATE_GROUP_FROM_CHILD_DELAY: float = 0.5

ENTITY_REGISTRY: dict[ClusterId, list[type[PlatformEntity]]] = defaultdict(list)
GROUP_ENTITY_REGISTRY: list[type[GroupEntity]] = []


@dataclasses.dataclass(slots=True)
class _ClusterCompatView:
    """Lightweight cluster metadata view for entity compatibility paths."""

    name: str
    cluster: Any
    unique_id: str
    id: str


class PlatformFeatureGroup(StrEnum):
    """Feature groups for platform entities."""

    # OnOff server clusters can be turned into lights, shades, or switches (fallback)
    LIGHT_OR_SWITCH_OR_SHADE = "light_or_switch_or_shade"

    # OnOff client clusters can be turned into manufacturer-specific motion sensors or
    # fall back to generic binary sensors
    BINARY_SENSOR = "binary_sensor"

    # Thermostat entities encompass the functionality of Fan entities
    THERMOSTAT_FAN = "thermostat_fan"

    # Model-specific overrides for HVAC action
    HVAC_ACTION = "hvac_action"

    # Model-specific overrides for VOC level
    VOC_LEVEL = "voc_level"

    # Model-specific overrides for Smart Energy Summation
    SMART_ENERGY_SUMMATION = "smart_energy_summation"

    # Model-specific overrides for local temperature calibration
    LOCAL_TEMPERATURE_CALIBRATION = "local_temperature_calibration"


@dataclasses.dataclass(frozen=True, init=False)
class ClusterMatch:
    """Declares cluster requirements for an entity class."""

    clusters: frozenset[str]
    client_clusters: frozenset[str]
    optional_clusters: frozenset[str]

    # Strict filters: if present, device info must match
    manufacturers: frozenset[str] | None
    models: frozenset[str] | None
    exposed_features: frozenset[str] | None

    # If present, device must match one of the given profile and device type combinations.
    # This will be ignored if `platform_override` is used.
    profile_device_types: (  # type:ignore[valid-type]
        frozenset[
            tuple[Literal[zha.PROFILE_ID], zha.DeviceType]
            | tuple[Literal[zll.PROFILE_ID], zll.DeviceType]
            | tuple[int, int]
        ]
        | None
    )
    not_profile_device_types: (  # type:ignore[valid-type]
        frozenset[
            tuple[Literal[zha.PROFILE_ID], zha.DeviceType]
            | tuple[Literal[zll.PROFILE_ID], zll.DeviceType]
            | tuple[int, int]
        ]
        | None
    )

    # For a given feature, only entities with the highest priority will be considered
    feature_priority: tuple[PlatformFeatureGroup, int] | None

    def __init__(
        self,
        *,
        clusters: frozenset[str] = frozenset(),
        client_clusters: frozenset[str] = frozenset(),
        optional_clusters: frozenset[str] = frozenset(),
        manufacturers: frozenset[str] | None = None,
        models: frozenset[str] | None = None,
        exposed_features: frozenset[str] | None = None,
        profile_device_types: (  # type:ignore[valid-type]
            frozenset[
                tuple[Literal[zha.PROFILE_ID], zha.DeviceType]
                | tuple[Literal[zll.PROFILE_ID], zll.DeviceType]
                | tuple[int, int]
            ]
            | None
        ) = None,
        not_profile_device_types: (  # type:ignore[valid-type]
            frozenset[
                tuple[Literal[zha.PROFILE_ID], zha.DeviceType]
                | tuple[Literal[zll.PROFILE_ID], zll.DeviceType]
                | tuple[int, int]
            ]
            | None
        ) = None,
        feature_priority: tuple[PlatformFeatureGroup, int] | None = None,
    ) -> None:
        """Initialize cluster match criteria."""
        object.__setattr__(self, "clusters", clusters)
        object.__setattr__(self, "client_clusters", client_clusters)
        object.__setattr__(self, "optional_clusters", optional_clusters)
        object.__setattr__(self, "manufacturers", manufacturers)
        object.__setattr__(self, "models", models)
        object.__setattr__(self, "exposed_features", exposed_features)
        object.__setattr__(self, "profile_device_types", profile_device_types)
        object.__setattr__(self, "not_profile_device_types", not_profile_device_types)
        object.__setattr__(self, "feature_priority", feature_priority)


def register_entity[T: type[PlatformEntity]](cluster_id: ClusterId) -> Callable[[T], T]:
    """Register an entity class for discovery."""

    def inner(cls: T) -> T:
        if hasattr(cls, "_ensure_entity_cluster_configs"):
            cls._ensure_entity_cluster_configs()
        ENTITY_REGISTRY[cluster_id].append(cls)
        return cls

    return inner


def register_group_entity(cls: type[GroupEntity]) -> type[GroupEntity]:
    """Register a group entity class for discovery."""
    GROUP_ENTITY_REGISTRY.append(cls)
    return cls


class EntityCategory(StrEnum):
    """Category of an entity."""

    # Config: An entity which allows changing the configuration of a device.
    CONFIG = "config"

    # Diagnostic: An entity exposing some configuration parameter,
    # or diagnostics of a device.
    DIAGNOSTIC = "diagnostic"


@dataclasses.dataclass(frozen=True, kw_only=True)
class BaseEntityInfo:
    """Information about a base entity."""

    fallback_name: str
    unique_id: str
    migrate_unique_ids: frozenset[str]
    platform: str
    class_name: str
    translation_key: str | None
    translation_placeholders: dict[str, str] | None
    device_class: str | None
    state_class: str | None
    entity_category: str | None
    entity_registry_enabled_default: bool
    enabled: bool = True
    primary: bool

    # For platform entities
    clusters: list[Any]
    device_ieee: EUI64 | None
    endpoint_id: int | None
    available: bool | None

    # For group entities
    group_id: int | None


@dataclasses.dataclass(frozen=True, kw_only=True)
class BaseIdentifiers:
    """Identifiers for the base entity."""

    unique_id: str
    platform: str


@dataclasses.dataclass(frozen=True, kw_only=True)
class PlatformEntityIdentifiers(BaseIdentifiers):
    """Identifiers for the platform entity."""

    device_ieee: EUI64
    endpoint_id: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class GroupEntityIdentifiers(BaseIdentifiers):
    """Identifiers for the group entity."""

    group_id: int


@dataclasses.dataclass(frozen=True, kw_only=True)
class EntityStateChangedEvent:
    """Event for when an entity state changes."""

    event_type: Final[str] = "entity"
    event: Final[str] = STATE_CHANGED
    platform: str
    unique_id: str
    device_ieee: EUI64 | None = None
    endpoint_id: int | None = None
    group_id: int | None = None


class BaseEntity(LogMixin, EventBase):
    """Base class for entities."""

    PLATFORM: Platform = Platform.UNKNOWN

    _attr_fallback_name: str | None = None
    _attr_icon: str | None = None
    _attr_translation_key: str | None = None
    _attr_translation_placeholders: dict[str, str] | None = None
    _attr_entity_category: EntityCategory | None = None
    _attr_entity_registry_enabled_default: bool = True
    _attr_device_class: str | None = None
    _attr_state_class: str | None = None
    _attr_enabled: bool = True
    _attr_always_supported: bool = False
    _attr_primary: bool | None = None

    # When two entities both want to be primary, the one with the higher weight will be
    # chosen. If there is a tie, both lose.
    _attr_primary_weight: int = 0

    def __init__(self, unique_id: str) -> None:
        """Initialize the platform entity."""
        super().__init__()

        self._unique_id: str = unique_id
        self._migrate_unique_ids: list[str] = []

        self.__previous_state: Any = None
        self._tracked_tasks: list[asyncio.Task] = []
        self._tracked_handles: list[asyncio.Handle] = []
        self._on_remove_callbacks: list[Callable[[], None]] = []

    def is_supported(self) -> bool:
        """Return if the entity is supported for the device."""
        if self._attr_always_supported:
            return True

        return self._is_supported()

    def _is_supported(self) -> bool:
        """Return if the entity is supported for the device, internal."""
        return True

    def is_supported_in_list(self, entities: list[BaseEntity]) -> bool:
        """Return if the entity is supported given all other entities."""
        return True

    def recompute_capabilities(self) -> None:
        """Recompute capabilities and feature flags."""
        pass

    @property
    def enabled(self) -> bool:
        """Return the entity enabled state."""
        return self._attr_enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        """Set the entity enabled state."""
        self._attr_enabled = value

    @property
    def primary(self) -> bool:
        """Return if the entity is the primary device control."""
        if self._attr_primary is None:
            return False

        return self._attr_primary

    @primary.setter
    def primary(self, value: bool | None) -> None:
        """Set the entity as the primary device control."""
        self._attr_primary = value

    @property
    def primary_weight(self) -> int:
        """Return the primary weight of the entity."""
        return self._attr_primary_weight

    @property
    def fallback_name(self) -> str | None:
        """Return the entity fallback name for when a translation key is unavailable."""
        return self._attr_fallback_name

    @property
    def icon(self) -> str | None:
        """Return the entity icon."""
        return self._attr_icon

    @property
    def translation_key(self) -> str | None:
        """Return the translation key."""
        if hasattr(self, "_attr_translation_key"):
            return self._attr_translation_key
        return None

    @property
    def translation_placeholders(self) -> dict[str, str] | None:
        """Return the translation placeholders."""
        return self._attr_translation_placeholders

    @property
    def entity_category(self) -> EntityCategory | None:
        """Return the entity category."""
        if hasattr(self, "_attr_entity_category"):
            return self._attr_entity_category
        return None

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return the entity category."""
        return self._attr_entity_registry_enabled_default

    @property
    def device_class(self) -> str | None:
        """Return the device class."""
        return self._attr_device_class

    @property
    def state_class(self) -> str | None:
        """Return the state class."""
        return self._attr_state_class

    @final
    @property
    def unique_id(self) -> str:
        """Return the unique id."""
        return self._unique_id

    @final
    @property
    def migrate_unique_ids(self) -> frozenset[str]:
        """Return the previous unique ids to migrate from, if any."""
        return frozenset(self._migrate_unique_ids)

    @cached_property
    def identifiers(self) -> BaseIdentifiers:
        """Return a dict with the information necessary to identify this entity."""
        return BaseIdentifiers(
            unique_id=self.unique_id,
            platform=self.PLATFORM,
        )

    @cached_property
    def info_object(self) -> BaseEntityInfo:
        """Return a representation of the platform entity."""

        return BaseEntityInfo(
            unique_id=self.unique_id,
            migrate_unique_ids=self.migrate_unique_ids,
            platform=self.PLATFORM,
            class_name=self.__class__.__name__,
            fallback_name=self.fallback_name,
            translation_key=self.translation_key,
            translation_placeholders=self.translation_placeholders,
            device_class=self.device_class,
            state_class=self.state_class,
            entity_category=self.entity_category,
            entity_registry_enabled_default=self.entity_registry_enabled_default,
            enabled=self.enabled,
            primary=self.primary,
            # Set by platform entities
            clusters=[],
            device_ieee=None,
            endpoint_id=None,
            available=None,
            # Set by group entities
            group_id=None,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Return the arguments to use in the command."""
        return {
            "class_name": self.__class__.__name__,
        }

    @cached_property
    def extra_state_attribute_names(self) -> set[str] | None:
        """Return entity specific state attribute names.

        Implemented by platform classes. Convention for attribute names
        is lowercase snake_case.
        """
        if hasattr(self, "_attr_extra_state_attribute_names"):
            return self._attr_extra_state_attribute_names
        return None

    def enable(self) -> None:
        """Enable the entity."""
        self.enabled = True

    def disable(self) -> None:
        """Disable the entity."""
        self.enabled = False

    def on_add(self) -> None:
        """Run when entity is added."""
        pass

    async def on_remove(self) -> None:
        """Cancel tasks and timers this entity owns."""
        while self._on_remove_callbacks:
            callback = self._on_remove_callbacks.pop()
            self.debug("Running remove callback: %s", callback)
            callback()

        for handle in self._tracked_handles:
            self.debug("Cancelling handle: %s", handle)
            handle.cancel()

        tasks = [t for t in self._tracked_tasks if not (t.done() or t.cancelled())]
        for task in tasks:
            self.debug("Cancelling task: %s", task)
            task.cancel()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(*tasks, return_exceptions=True)

    def maybe_emit_state_changed_event(self) -> None:
        """Send the state of this platform entity."""
        state = self.state
        if self.__previous_state != state:
            self.emit(
                STATE_CHANGED, EntityStateChangedEvent(**self.identifiers.__dict__)
            )
            self.__previous_state = state

    def log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a message."""
        msg = f"%s: {msg}"
        args = (self._unique_id,) + args
        _LOGGER.log(level, msg, *args, **kwargs)


class PlatformEntity(BaseEntity):
    """Class that represents an entity for a device platform."""

    # suffix to add to the unique_id of the entity. Used for multi
    # entities using the same primary cluster/cluster id for the entity.
    _unique_id_suffix: str | None = None

    _migrate_platform_unique_ids: tuple[tuple[UniqueIdMigration, str]] | None = None

    # Auto-discovery for the entity
    _cluster_match: ClusterMatch | None = None

    # Optional entity-owned cluster config overrides keyed by cluster name.
    _entity_cluster_configs: (
        dict[str | tuple[str, bool], EntityClusterConfig] | None
    ) = None

    @classmethod
    def _default_entity_cluster_refs(cls) -> tuple[tuple[str, bool], ...]:
        """Return ordered cluster refs derived from class match metadata."""
        match = cls.get_cluster_match()
        if match is None:
            return ()

        server_clusters = tuple(sorted(match.clusters | match.optional_clusters))
        client_clusters = tuple(sorted(match.client_clusters))
        return (
            *((cluster_name, False) for cluster_name in server_clusters),
            *((cluster_name, True) for cluster_name in client_clusters),
        )

    @classmethod
    def _ensure_entity_cluster_configs(
        cls,
    ) -> dict[str | tuple[str, bool], EntityClusterConfig]:
        """Ensure class-level entity config metadata exists."""
        if cls._entity_cluster_configs is not None:
            return cls._entity_cluster_configs

        auto_configs = entity_cluster_configs_from_refs(
            *cls._default_entity_cluster_refs()
        )
        cls._entity_cluster_configs = auto_configs
        return auto_configs

    @staticmethod
    def _cluster_names_from_refs(
        cluster_refs: Sequence[tuple[str, bool]],
    ) -> tuple[str, ...]:
        """Return ordered cluster names from name/direction refs."""
        return tuple(name for name, _is_client in cluster_refs)

    @classmethod
    def _default_match_cluster_names(cls) -> tuple[str, ...]:
        """Return required cluster names declared by the class match."""
        match = cls.get_cluster_match()
        if match is None:
            return ()
        return tuple(sorted(match.clusters | match.client_clusters))

    @classmethod
    def resolve_primary_cluster_name(
        cls,
        clusters: list[Any],
        cluster_refs: Sequence[tuple[str, bool]] | None = None,
        cluster_names: Sequence[str] | None = None,
    ) -> str:
        """Resolve a primary cluster name from provided clusters or class match."""
        if clusters:
            return clusters[0].name
        if cluster_refs:
            return cluster_refs[0][0]
        if cluster_names:
            return cluster_names[0]

        cluster_names = cls._default_match_cluster_names()
        if len(cluster_names) == 1:
            return cluster_names[0]

        raise ValueError(
            f"{cls.__name__} cannot infer a primary cluster name from match: "
            f"{cluster_names!r}"
        )

    @classmethod
    def resolve_primary_cluster_id(
        cls,
        endpoint: Endpoint,
        clusters: list[Any],
        cluster_refs: Sequence[tuple[str, bool]] | None = None,
        cluster_names: Sequence[str] | None = None,
    ) -> int:
        """Resolve a primary cluster id from provided clusters or endpoint lookup."""
        if clusters:
            return int(clusters[0].cluster_id)

        cluster_name = cls.resolve_primary_cluster_name(
            clusters,
            cluster_refs,
            cluster_names,
        )
        cluster, _unique_id = endpoint.resolve_cluster_and_unique_id(cluster_name)
        return int(cluster.cluster_id)

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        *,
        cluster_refs: Sequence[tuple[str, bool]] | None = None,
        cluster_names: Sequence[str] | None = None,
        entity_metadata: EntityMetadata | None = None,
        legacy_discovery_unique_id: str | None = None,
        **kwargs: Any,
    ):
        """Initialize the platform entity."""
        if entity_metadata is not None:
            self._init_from_quirks_metadata(entity_metadata)

        if legacy_discovery_unique_id is None:
            primary_cluster_id = type(self).resolve_primary_cluster_id(
                endpoint,
                clusters,
                cluster_refs,
                cluster_names,
            )
            legacy_discovery_unique_id = (
                f"{device.ieee}-{endpoint.id}-{primary_cluster_id}"
            )

        if self._unique_id_suffix is not None:
            unique_id = f"{legacy_discovery_unique_id}-{self._unique_id_suffix}"
        else:
            unique_id = legacy_discovery_unique_id

        super().__init__(unique_id=unique_id, **kwargs)

        resolved_cluster_names: tuple[str, ...]
        if clusters:
            resolved_cluster_names = tuple(cluster_obj.name for cluster_obj in clusters)
        elif cluster_refs:
            resolved_cluster_names = type(self)._cluster_names_from_refs(cluster_refs)
        elif cluster_names:
            resolved_cluster_names = tuple(cluster_names)
        else:
            resolved_cluster_names = type(self)._default_match_cluster_names()
        self._cluster_names = resolved_cluster_names
        self._cluster_refs = tuple(cluster_refs) if cluster_refs else ()
        self._device: Device = device
        self._endpoint = endpoint

        self._cluster_helpers = list(clusters)
        self._clusters_by_name: dict[str, Any] = {}
        self._resolved_clusters: dict[tuple[str, bool | None], tuple[Any, str]] = {}

        for cluster_obj in self._cluster_helpers:
            self._clusters_by_name[cluster_obj.name] = cluster_obj

        if self._cluster_refs:
            self._populate_cluster_compat_from_refs()

    def _populate_cluster_compat_from_refs(self) -> None:
        """Backfill cluster-name map from refs for compatibility consumers."""
        for cluster_name, is_client in self._cluster_refs:
            if cluster_name in self._clusters_by_name:
                continue
            with suppress(KeyError):
                cluster, unique_id = self.resolve_cluster_and_unique_id(
                    cluster_name,
                    is_client=is_client,
                )
                self._remember_cluster_resolution(cluster, unique_id)
                self._ensure_cluster_helpers(cluster, unique_id)
                runtime_id = (
                    f"{self.endpoint.id}:0x{int(cluster.cluster_id):04x}_client"
                    if cluster.is_client
                    else f"{self.endpoint.id}:0x{int(cluster.cluster_id):04x}"
                )
                self._clusters_by_name[cluster_name] = _ClusterCompatView(
                    name=cluster_name,
                    cluster=cluster,
                    unique_id=unique_id,
                    id=runtime_id,
                )

    @classmethod
    def get_cluster_match(cls) -> ClusterMatch | None:
        """Return the entity cluster match declaration."""
        return cls._cluster_match

    @classmethod
    def get_entity_cluster_config(
        cls,
        cluster_name: str,
        *,
        is_client: bool | None = None,
    ) -> EntityClusterConfig | None:
        """Return entity-owned cluster config override for a cluster name."""
        if configs := cls._ensure_entity_cluster_configs():
            if is_client is not None:
                directional = configs.get((cluster_name, is_client))
                if directional is not None:
                    return directional
            if config := configs.get(cluster_name):
                return config
        return None

    def get_primary_cluster_name(self) -> str:
        """Return the primary cluster name for this entity."""
        return type(self).resolve_primary_cluster_name(
            self._cluster_helpers,
            self._cluster_refs,
            self._cluster_names,
        )

    def has_cluster(self, cluster_id: int, cluster_type: Any | None = None) -> bool:
        """Return True if this entity references a matching cluster."""
        for cluster_compat in self._clusters_by_name.values():
            cluster = cluster_compat.cluster
            if cluster.cluster_id != cluster_id:
                continue
            if cluster_type is None or cluster.cluster_type == cluster_type:
                return True

        for cluster_name, is_client in self._iter_cluster_ref_sequence():
            with suppress(KeyError):
                cluster, _unique_id = self.resolve_cluster_and_unique_id(
                    cluster_name,
                    is_client=is_client,
                )
                if cluster.cluster_id != cluster_id:
                    continue
                if cluster_type is None or cluster.cluster_type == cluster_type:
                    return True

        return False

    def get_cluster(
        self,
        cluster_name: str,
        *,
        is_client: bool | None = None,
    ) -> Any:
        """Return a resolved zigpy cluster by cluster name."""
        selected_direction = self._resolve_cluster_direction(
            cluster_name,
            requested_direction=is_client,
        )

        cluster, unique_id = self._resolve_cluster(cluster_name, selected_direction)
        self._remember_cluster_resolution(cluster, unique_id)
        self._ensure_cluster_helpers(cluster, unique_id)
        return cluster

    def _resolve_cluster_direction(
        self,
        cluster_name: str,
        *,
        requested_direction: bool | None = None,
    ) -> bool | None:
        """Resolve cluster direction preference for a named cluster reference."""
        selected_direction = requested_direction
        if selected_direction is not None:
            return selected_direction

        matching_refs = [
            ref_is_client
            for ref_name, ref_is_client in self._cluster_refs
            if ref_name == cluster_name
        ]
        if matching_refs:
            # Preserve declaration order when a name appears in both directions.
            return matching_refs[0]

        return None

    def _resolve_cluster(
        self, cluster_name: str, selected_direction: bool | None
    ) -> tuple[Any, str]:
        """Resolve and cache a cluster by name and direction."""
        cache_key = (cluster_name, selected_direction)
        if cache_key in self._resolved_clusters:
            return self._resolved_clusters[cache_key]

        cluster, unique_id = self.resolve_cluster_and_unique_id(
            cluster_name,
            is_client=selected_direction,
        )
        self._resolved_clusters[cache_key] = (cluster, unique_id)
        return cluster, unique_id

    def _remember_cluster_resolution(self, cluster: Any, unique_id: str) -> None:
        """Track resolved cluster unique ids for event payload parity."""
        cluster_name = cluster.ep_attribute or f"cluster_0x{cluster.cluster_id:04x}"
        self._resolved_clusters.setdefault(
            (cluster_name, cluster.is_client),
            (cluster, unique_id),
        )

    def _ensure_cluster_helpers(self, cluster: Any, unique_id: str) -> None:
        """Attach helper attrs used by legacy entity code paths."""
        setattr(cluster, "unique_id", unique_id)
        setattr(cluster, "data_cache", self.get_cluster_data_cache(cluster))
        if not hasattr(cluster, "get_attribute_value"):
            setattr(
                cluster,
                "get_attribute_value",
                functools.partial(cluster_get_attribute_value, cluster),
            )
        if not hasattr(cluster, "get_attributes"):
            setattr(
                cluster,
                "get_attributes",
                functools.partial(cluster_get_attributes, cluster),
            )
        if not hasattr(cluster, "write_attributes_safe"):
            setattr(
                cluster,
                "write_attributes_safe",
                functools.partial(cluster_write_attributes_safe, cluster),
            )
        wrapped_commands: set[str] = getattr(
            cluster, "_zha_retry_wrapped_commands", set()
        )
        for command_map_name in ("client_commands", "server_commands"):
            command_map = getattr(cluster, command_map_name, None)
            if not isinstance(command_map, dict):
                continue
            for command_def in command_map.values():
                command_name = getattr(command_def, "name", None)
                if not command_name or command_name in wrapped_commands:
                    continue
                command = getattr(cluster, command_name, None)
                if not callable(command):
                    continue
                command_callable = cast(Callable[..., Any], command)

                async def _wrapped_command(
                    *args: Any,
                    __command: Callable[..., Any] = command_callable,
                    **kwargs: Any,
                ) -> Any:
                    return await retryable_cluster_call(__command, *args, **kwargs)

                setattr(cluster, command_name, _wrapped_command)
                wrapped_commands.add(command_name)
        setattr(cluster, "_zha_retry_wrapped_commands", wrapped_commands)

    def get_cluster_unique_id(
        self,
        cluster_name: str,
        *,
        is_client: bool | None = None,
    ) -> str:
        """Return the legacy cluster unique id for a resolved cluster reference."""
        selected_direction = self._resolve_cluster_direction(
            cluster_name,
            requested_direction=is_client,
        )
        _cluster, unique_id = self._resolve_cluster(cluster_name, selected_direction)
        return unique_id

    def get_cluster_unique_id_for_cluster(self, cluster: Any) -> str:
        """Return the legacy cluster unique id for a cluster object."""
        for resolved_cluster, unique_id in self._resolved_clusters.values():
            if resolved_cluster is cluster:
                return unique_id
        return self.endpoint.get_legacy_cluster_unique_id(cluster)

    @staticmethod
    def get_cluster_data_cache(cluster: Any) -> dict[str, Any]:
        """Return shared per-cluster entity data cache."""
        shared_cache = getattr(cluster, "_zha_entity_data_cache", None)
        if shared_cache is None:
            shared_cache = {}
            setattr(cluster, "_zha_entity_data_cache", shared_cache)
        return shared_cache

    async def get_cluster_attribute_value(
        self,
        cluster: Any,
        attribute: int | str,
        *,
        from_cache: bool = True,
    ) -> Any:
        """Read a single cluster attribute with safe error handling."""
        return await cluster_get_attribute_value(
            cluster, attribute, from_cache=from_cache
        )

    async def get_cluster_attributes(
        self,
        cluster: Any,
        attributes: list[int | str],
        *,
        from_cache: bool = True,
        only_cache: bool = True,
    ) -> dict[int | str, Any]:
        """Read multiple cluster attributes with safe error handling."""
        return await cluster_get_attributes(
            cluster,
            attributes,
            from_cache=from_cache,
            only_cache=only_cache,
        )

    async def write_cluster_attributes_safe(
        self,
        cluster: Any,
        attributes: dict[str, Any],
        *,
        manufacturer: int | UndefinedType | None = UNDEFINED,
    ) -> None:
        """Write attributes and raise `ZHAException` on failures."""
        await cluster_write_attributes_safe(
            cluster,
            attributes,
            manufacturer=manufacturer,
        )

    async def call_cluster_method(
        self,
        cluster: Any,
        method_name: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Call a cluster method with retry + transport error wrapping."""
        method = getattr(cluster, method_name)
        return await retryable_cluster_call(method, *args, **kwargs)

    def resolve_cluster_and_unique_id(
        self,
        cluster_name: str,
        *,
        is_client: bool | None = None,
    ) -> tuple[Any, str]:
        """Resolve a cluster object and its unique id for an entity cluster name."""
        selected_direction = self._resolve_cluster_direction(
            cluster_name,
            requested_direction=is_client,
        )
        if selected_direction is not None:
            return self.endpoint.resolve_cluster_and_unique_id_for_ref(
                cluster_name,
                is_client=selected_direction,
            )
        return self.endpoint.resolve_cluster_and_unique_id(cluster_name)

    def subscribe_cluster_attribute_updates(
        self,
        cluster_name: str,
        callback: Callable[[ClusterAttributeUpdatedEvent], None],
        *,
        is_client: bool | None = None,
    ) -> None:
        """Subscribe directly to zigpy cluster attribute updates for a cluster name."""
        cluster, unique_id = self.resolve_cluster_and_unique_id(
            cluster_name,
            is_client=is_client,
        )
        self._remember_cluster_resolution(cluster, unique_id)

        def _forward_event(
            event: AttributeReadEvent
            | AttributeReportedEvent
            | AttributeUpdatedEvent
            | AttributeWrittenEvent,
        ) -> None:
            callback(
                ClusterAttributeUpdatedEvent(
                    attribute_id=event.attribute_id,
                    attribute_name=event.attribute_name,
                    attribute_value=event.value,
                    cluster_unique_id=unique_id,
                    cluster_id=cluster.cluster_id,
                )
            )

        for event_type in (
            AttributeReadEvent,
            AttributeReportedEvent,
            AttributeUpdatedEvent,
            AttributeWrittenEvent,
        ):
            self._on_remove_callbacks.append(
                cluster.on_event(event_type.event_type, _forward_event)
            )

    def subscribe_cluster_commands(
        self,
        cluster_name: str,
        callback: Callable[[ClusterCommandEvent], None],
        *,
        is_client: bool | None = None,
    ) -> None:
        """Subscribe directly to zigpy cluster command callbacks for a cluster name."""
        cluster, unique_id = self.resolve_cluster_and_unique_id(
            cluster_name,
            is_client=is_client,
        )
        self._remember_cluster_resolution(cluster, unique_id)

        class _ClusterCommandListener:
            """Listener bridge for cluster command callbacks."""

            def cluster_command(
                self, tsn: int, command_id: int, args: list[Any] | None
            ) -> None:
                callback(
                    ClusterCommandEvent(
                        tsn=tsn,
                        command_id=command_id,
                        args=list(args or []),
                        cluster_unique_id=unique_id,
                        cluster_id=cluster.cluster_id,
                    )
                )

        listener = _ClusterCommandListener()
        cluster.add_listener(listener)

        def _remove_listener() -> None:
            with suppress(ValueError):
                cluster.remove_listener(listener)

        self._on_remove_callbacks.append(_remove_listener)

    def _init_from_quirks_metadata(self, entity_metadata: EntityMetadata) -> None:
        """Init this entity from the quirks metadata."""
        if entity_metadata.initially_disabled:
            self._attr_entity_registry_enabled_default = False

        # v2 quirks entities are assumed to always be supported
        self._attr_always_supported = True

        has_attribute_name = hasattr(entity_metadata, "attribute_name")
        has_command_name = hasattr(entity_metadata, "command_name")
        has_fallback_name = hasattr(entity_metadata, "fallback_name")

        if has_fallback_name:
            self._attr_fallback_name = entity_metadata.fallback_name

        if entity_metadata.translation_key:
            self._attr_translation_key = entity_metadata.translation_key

        if entity_metadata.translation_placeholders:
            self._attr_translation_placeholders = (
                entity_metadata.translation_placeholders
            )

        if unique_id_suffix := entity_metadata.unique_id_suffix:
            self._unique_id_suffix = unique_id_suffix
        elif has_attribute_name:
            self._unique_id_suffix = entity_metadata.attribute_name
        elif has_command_name:
            self._unique_id_suffix = entity_metadata.command_name

        if entity_metadata.entity_type is EntityType.CONFIG:
            self._attr_entity_category = EntityCategory.CONFIG
        elif entity_metadata.entity_type is EntityType.DIAGNOSTIC:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        else:
            self._attr_entity_category = None

        if entity_metadata.primary is not None:
            self._attr_primary = entity_metadata.primary

    @cached_property
    def identifiers(self) -> PlatformEntityIdentifiers:
        """Return a dict with the information necessary to identify this entity."""
        return PlatformEntityIdentifiers(
            unique_id=self.unique_id,
            platform=self.PLATFORM,
            device_ieee=self.device.ieee,
            endpoint_id=self.endpoint.id,
        )

    @cached_property
    def info_object(self) -> BaseEntityInfo:
        """Return a representation of the platform entity."""
        return dataclasses.replace(
            super().info_object,
            clusters=self._resolve_cluster_info_objects(),
            device_ieee=self._device.ieee,
            endpoint_id=self._endpoint.id,
            available=self.available,
        )

    def _iter_cluster_ref_sequence(self) -> tuple[tuple[str, bool], ...]:
        """Return ordered cluster refs used by this entity."""
        if self._cluster_refs:
            return self._cluster_refs
        return tuple((cluster_name, False) for cluster_name in self._cluster_names)

    def _resolve_poll_cluster_helpers(self) -> list[Any]:
        """Return cluster-backed helper objects associated with this entity."""
        return list(self._cluster_helpers)

    def _resolve_cluster_info_objects(self) -> list[Any]:
        """Resolve cluster helper info objects for diagnostics compatibility."""
        return [ch.info_object for ch in self._resolve_poll_cluster_helpers()]

    @property
    def device(self) -> Device:
        """Return the device."""
        return self._device

    @property
    def endpoint(self) -> Endpoint:
        """Return the endpoint."""
        return self._endpoint

    @property
    def should_poll(self) -> bool:
        """Return True if we need to poll for state changes."""
        return False

    @property
    def available(self) -> bool:
        """Return true if the device this entity belongs to is available."""
        return self.device.available

    @property
    def state(self) -> dict[str, Any]:
        """Return the arguments to use in the command."""
        state = super().state
        state["available"] = self.available
        return state

    async def async_update(self) -> None:
        """Retrieve latest state."""
        self.debug("polling current state")
        tasks = [
            cluster_helper.async_update()
            for cluster_helper in self._resolve_poll_cluster_helpers()
            if hasattr(cluster_helper, "async_update")
        ]
        if tasks:
            await asyncio.gather(*tasks)
            self.maybe_emit_state_changed_event()


class GroupEntity(BaseEntity):
    """A base class for group entities."""

    def __init__(
        self,
        group: Group,
        update_group_from_member_delay: float = DEFAULT_UPDATE_GROUP_FROM_CHILD_DELAY,
    ) -> None:
        """Initialize a group."""
        super().__init__(unique_id=f"{self.PLATFORM}_zha_group_0x{group.group_id:04x}")
        self._attr_fallback_name: str = group.name
        self._group: Group = group
        self._change_listener_debouncer = Debouncer(
            group.gateway,
            _LOGGER,
            cooldown=update_group_from_member_delay,
            immediate=False,
            function=self.update,
        )

    @cached_property
    def identifiers(self) -> GroupEntityIdentifiers:
        """Return a dict with the information necessary to identify this entity."""
        return GroupEntityIdentifiers(
            unique_id=self.unique_id,
            platform=self.PLATFORM,
            group_id=self.group_id,
        )

    @cached_property
    def info_object(self) -> BaseEntityInfo:
        """Return a representation of the group."""
        return dataclasses.replace(
            super().info_object,
            group_id=self.group_id,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Return the arguments to use in the command."""
        state = super().state
        state["available"] = self.available
        return state

    @property
    def available(self) -> bool:
        """Return true if all member entities are available."""
        return any(
            platform_entity.available
            for platform_entity in self._group.get_platform_entities(self.PLATFORM)
        )

    @property
    def group_id(self) -> int:
        """Return the group id."""
        return self._group.group_id

    @property
    def group(self) -> Group:
        """Return the group."""
        return self._group

    def debounced_update(self, _: Any | None = None) -> None:
        """Debounce updating group entity from member entity updates."""
        # Delay to ensure that we get updates from all members before updating the group entity
        assert self._change_listener_debouncer
        self.group.gateway.create_task(self._change_listener_debouncer.async_call())

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self._group.register_group_entity(self)

    async def on_remove(self) -> None:
        """Cancel tasks this entity owns."""
        await super().on_remove()
        self._group.unregister_group_entity(self)

        if self._change_listener_debouncer:
            self._change_listener_debouncer.async_cancel()

    @abstractmethod
    def update(self, _: Any | None = None) -> None:
        """Update the state of this group entity."""

    async def async_update(self, _: Any | None = None) -> None:
        """Update the state of this group entity."""
        self.update()
