"""Representation of a Zigbee endpoint for zha."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable, Iterator
import contextlib
import functools
import logging
from typing import TYPE_CHECKING, Any, Final, TypeVar

import zigpy.exceptions
from zigpy.profiles.zha import PROFILE_ID as ZHA_PROFILE_ID
from zigpy.profiles.zll import PROFILE_ID as ZLL_PROFILE_ID
from zigpy.typing import UNDEFINED
import zigpy.util
from zigpy.zcl.clusters.closures import WindowCovering
from zigpy.zcl.clusters.general import LevelControl, PowerConfiguration, Scenes
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.clusters.lightlink import LightLink
from zigpy.zcl.clusters.security import IasZone
from zigpy.zcl.foundation import (
    GENERAL_COMMANDS,
    CommandSchema,
    ConfigureReportingResponseRecord,
    GeneralCommand,
    Status,
)
from zigpy.zcl.helpers import ReportingConfig as ZigpyReportingConfig

from zha.application import const
from zha.application.const import ZHA_CLUSTER_MSG_BIND, ZHA_CLUSTER_MSG_CFG_RPT
from zha.application.platforms.cluster_config import (
    ClusterConfigContribution,
    ClusterConfigMerger,
    ClusterTarget,
    EntityClusterConfig,
    MergedClusterConfig,
    ReportingConfig,
    cluster_target_from_cluster,
)
from zha.application.platforms.cluster_names import (
    AQARA_OPPLE_CLUSTER,
    CLUSTER_READS_PER_REQ,
    REPORT_CONFIG_ATTR_PER_REQ,
)
from zha.async_ import gather_with_limited_concurrency
from zha.exceptions import ZHAException
from zha.zigbee import cluster_metadata
from zha.zigbee.cluster_events import ClusterBindEvent, ClusterConfigureReportingEvent
from zha.zigbee.cluster_io import write_attributes_safe as cluster_write_attributes_safe

if TYPE_CHECKING:
    from zigpy import Endpoint as ZigpyEndpoint

    from zha.zigbee.device import Device

ATTR_DEVICE_TYPE: Final[str] = "device_type"
ATTR_PROFILE_ID: Final[str] = "profile_id"
ATTR_IN_CLUSTERS: Final[str] = "input_clusters"
ATTR_OUT_CLUSTERS: Final[str] = "output_clusters"

_LOGGER = logging.getLogger(__name__)
CALLABLE_T = TypeVar("CALLABLE_T", bound=Callable)
RETRYABLE_REQUEST_DECORATOR = zigpy.util.retryable_request(tries=3)


class Endpoint:
    """Endpoint for a zha device."""

    _CLIENT_CLUSTER_EVENT_RELAY_IDS: Final[frozenset[int]] = frozenset(
        {
            Scenes.cluster_id,
            LevelControl.cluster_id,
            WindowCovering.cluster_id,
            Color.cluster_id,
        }
    )

    def __init__(self, zigpy_endpoint: ZigpyEndpoint, device: Device) -> None:
        """Initialize instance."""
        assert zigpy_endpoint is not None
        assert device is not None
        self._zigpy_endpoint: ZigpyEndpoint = zigpy_endpoint
        self._device: Device = device
        self._claimed_cluster_refs: set[tuple[str, bool]] = set()
        self._client_cluster_event_relays: dict[int, Any] = {}
        self._server_cluster_event_relays: dict[int, Any] = {}
        self._cluster_config_merger = ClusterConfigMerger()
        self._cluster_config_order: int = 0
        self._unique_id: str = f"{device.unique_id}-{zigpy_endpoint.endpoint_id}"

    def on_remove(self) -> None:
        """Run when endpoint is removed."""
        for cluster, relay in self._client_cluster_event_relays.values():
            with contextlib.suppress(ValueError):
                cluster.remove_listener(relay)
        self._client_cluster_event_relays.clear()
        for cluster, relay in self._server_cluster_event_relays.values():
            with contextlib.suppress(ValueError):
                cluster.remove_listener(relay)
        self._server_cluster_event_relays.clear()
        self._claimed_cluster_refs.clear()
        self._cluster_config_merger.reset()

    @functools.cached_property
    def device(self) -> Device:
        """Return the device this endpoint belongs to."""
        return self._device

    @property
    def claimed_cluster_refs(self) -> frozenset[tuple[str, bool]]:
        """Claimed clusters in ``(cluster_name, is_client)`` form."""
        return frozenset(self._claimed_cluster_refs)

    @functools.cached_property
    def zigpy_endpoint(self) -> ZigpyEndpoint:
        """Return endpoint of zigpy device."""
        return self._zigpy_endpoint

    @functools.cached_property
    def id(self) -> int:
        """Return endpoint id."""
        return self._zigpy_endpoint.endpoint_id

    def claim_cluster_refs(
        self, cluster_refs: Iterable[tuple[str, bool]]
    ) -> tuple[tuple[str, bool], ...]:
        """Claim clusters by ``(cluster_name, is_client)`` refs.

        Returns canonical claimed refs in first-seen order.
        """
        claimed_refs: list[tuple[str, bool]] = []
        seen_cluster_refs: set[tuple[str, bool]] = set()

        for cluster_name, is_client in cluster_refs:
            cluster = self._find_zigpy_cluster_by_name(
                cluster_name,
                is_client=is_client,
            )
            if cluster is None:
                continue

            canonical_ref = (
                cluster.ep_attribute or f"cluster_0x{cluster.cluster_id:04x}",
                is_client,
            )
            if canonical_ref in seen_cluster_refs:
                continue
            seen_cluster_refs.add(canonical_ref)

            claimed_refs.append(canonical_ref)
            self._claimed_cluster_refs.add(canonical_ref)

        return tuple(claimed_refs)

    def _legacy_unique_id_for_cluster(self, cluster: Any) -> str:
        """Build the cluster unique_id payload value for a zigpy cluster."""
        unique_id = self.unique_id.replace("-", ":")
        suffix = "_CLIENT" if cluster.is_client else ""
        return f"{unique_id}:0x{cluster.cluster_id:04x}{suffix}"

    def get_legacy_cluster_unique_id(self, cluster: Any) -> str:
        """Return the legacy cluster unique id payload value."""
        return self._legacy_unique_id_for_cluster(cluster)

    def _find_zigpy_cluster_by_name(
        self,
        cluster_name: str,
        *,
        is_client: bool | None = None,
    ) -> Any | None:
        """Find a zigpy cluster by ep_attribute or generic cluster name."""
        generic_cluster_id: int | None = None
        normalized_name = (
            cluster_name.removesuffix("_client")
            if cluster_name.endswith("_client")
            else cluster_name
        )
        if normalized_name.startswith("cluster_0x"):
            with contextlib.suppress(ValueError):
                generic_cluster_id = int(normalized_name.removeprefix("cluster_0x"), 16)

        search_server = is_client is not True
        search_client = is_client is not False

        if search_server:
            for cluster in self.zigpy_endpoint.in_clusters.values():
                if cluster.ep_attribute == cluster_name:
                    return cluster
                if (
                    generic_cluster_id is not None
                    and cluster.cluster_id == generic_cluster_id
                ):
                    return cluster

        if search_client:
            for cluster in self.zigpy_endpoint.out_clusters.values():
                if cluster.ep_attribute == cluster_name:
                    return cluster
                if (
                    generic_cluster_id is not None
                    and cluster.cluster_id == generic_cluster_id
                ):
                    return cluster

        return None

    def _has_exact_zigpy_cluster_name(
        self, cluster_name: str, *, is_client: bool
    ) -> bool:
        """Return True if a cluster's effective name exactly matches ``cluster_name``."""
        clusters = (
            self.zigpy_endpoint.out_clusters.values()
            if is_client
            else self.zigpy_endpoint.in_clusters.values()
        )
        for cluster in clusters:
            resolved_name = cluster.ep_attribute or (
                f"cluster_0x{cluster.cluster_id:04x}"
            )
            if resolved_name == cluster_name:
                return True
        return False

    def has_server_cluster_name(self, cluster_name: str) -> bool:
        """Return True if a server cluster with this name exists."""
        profile_id = self.zigpy_endpoint.profile_id
        if profile_id not in (ZHA_PROFILE_ID, ZLL_PROFILE_ID):
            return False
        return self._has_exact_zigpy_cluster_name(cluster_name, is_client=False)

    def has_client_cluster_name(self, cluster_name: str) -> bool:
        """Return True if a client cluster with this name exists."""
        profile_id = self.zigpy_endpoint.profile_id
        if profile_id not in (ZHA_PROFILE_ID, ZLL_PROFILE_ID):
            return False
        return self._has_exact_zigpy_cluster_name(cluster_name, is_client=True)

    def get_cluster_name_by_cluster_id(
        self, cluster_id: int, *, is_client: bool = False
    ) -> str | None:
        """Return the endpoint cluster name for a cluster id."""
        cluster = (
            self.zigpy_endpoint.out_clusters.get(cluster_id)
            if is_client
            else self.zigpy_endpoint.in_clusters.get(cluster_id)
        )
        if cluster is None:
            return None
        return getattr(cluster, "ep_attribute", None) or (f"cluster_0x{cluster_id:04x}")

    def get_cluster_ref_by_cluster_id(
        self, cluster_id: int, *, is_client: bool = False
    ) -> tuple[str, bool] | None:
        """Return ``(cluster_name, is_client)`` for a cluster id."""
        cluster_name = self.get_cluster_name_by_cluster_id(
            cluster_id,
            is_client=is_client,
        )
        if cluster_name is None:
            return None
        return cluster_name, is_client

    def is_cluster_claimed(self, cluster_id: int, *, is_client: bool = False) -> bool:
        """Return True if a cluster id is currently claimed."""
        cluster_ref = self.get_cluster_ref_by_cluster_id(
            cluster_id,
            is_client=is_client,
        )
        return cluster_ref is not None and cluster_ref in self._claimed_cluster_refs

    def claim_cluster_by_cluster_id(
        self, cluster_id: int, *, is_client: bool = False
    ) -> Any | None:
        """Claim a cluster by id if available and return its cluster object."""
        cluster_ref = self.get_cluster_ref_by_cluster_id(
            cluster_id,
            is_client=is_client,
        )
        if cluster_ref is None:
            return None
        self.claim_cluster_refs((cluster_ref,))
        return self._get_cluster_by_cluster_id(cluster_id, is_client=is_client)

    def iter_unclaimed_entityless_cluster_ids(
        self, cluster_ids: frozenset[int]
    ) -> Iterator[int]:
        """Yield unclaimed server cluster ids for entityless clusters."""
        for cluster in self.zigpy_endpoint.in_clusters.values():
            cluster_name = cluster.ep_attribute or (
                f"cluster_0x{cluster.cluster_id:04x}"
            )
            if (cluster_name, False) in self._claimed_cluster_refs:
                continue
            cluster_id = int(cluster.cluster_id)
            if cluster_id in cluster_ids:
                yield cluster_id

    def resolve_cluster_and_unique_id(self, cluster_name: str) -> tuple[Any, str]:
        """Resolve a cluster name to a zigpy cluster and legacy unique id."""
        cluster = self._find_zigpy_cluster_by_name(cluster_name)
        if cluster is None:
            raise KeyError(cluster_name)
        return cluster, self._legacy_unique_id_for_cluster(cluster)

    def resolve_cluster_and_unique_id_for_ref(
        self,
        cluster_name: str,
        *,
        is_client: bool,
    ) -> tuple[Any, str]:
        """Resolve a directional cluster ref to zigpy cluster and legacy unique id."""
        cluster = self._find_zigpy_cluster_by_name(cluster_name, is_client=is_client)
        if cluster is None:
            raise KeyError(cluster_name)
        return cluster, self._legacy_unique_id_for_cluster(cluster)

    @functools.cached_property
    def unique_id(self) -> str:
        """Return the unique id for this endpoint."""
        return self._unique_id

    @property
    def zigbee_signature(self) -> tuple[int, dict[str, Any]]:
        """Get the zigbee signature for the endpoint this pool represents."""
        return (
            self.id,
            {
                ATTR_PROFILE_ID: f"0x{self._zigpy_endpoint.profile_id:04x}"
                if self._zigpy_endpoint.profile_id is not None
                else "",
                ATTR_DEVICE_TYPE: f"0x{self._zigpy_endpoint.device_type:04x}"
                if self._zigpy_endpoint.device_type is not None
                else "",
                ATTR_IN_CLUSTERS: [
                    f"0x{cluster_id:04x}"
                    for cluster_id in sorted(self._zigpy_endpoint.in_clusters)
                ],
                ATTR_OUT_CLUSTERS: [
                    f"0x{cluster_id:04x}"
                    for cluster_id in sorted(self._zigpy_endpoint.out_clusters)
                ],
            },
        )

    @classmethod
    def new(cls, zigpy_endpoint: ZigpyEndpoint, device: Device) -> Endpoint:
        """Create new endpoint and initialize direct-cluster state."""
        endpoint = cls(zigpy_endpoint, device)
        endpoint._log_profile_setup_gating()
        endpoint.add_client_cluster_event_relays()
        endpoint.add_server_cluster_event_relays()

        return endpoint

    def _log_profile_setup_gating(self) -> None:
        """Retain legacy profile-gating debug logs during endpoint setup."""
        profile_id = self._zigpy_endpoint.profile_id
        if profile_id is None:
            _LOGGER.debug("Skipping endpoint, profile is None")
            return
        if profile_id not in (ZLL_PROFILE_ID, ZHA_PROFILE_ID):
            _LOGGER.debug(
                "Skipping endpoint, profile is not ZLL or ZHA: 0x%04X",
                profile_id,
            )
            return

    def add_client_cluster_event_relays(self) -> None:
        """Create command relays for client clusters with zha_event parity needs."""
        for cluster in self.zigpy_endpoint.out_clusters.values():
            self._ensure_client_cluster_event_relay(cluster)

    def add_server_cluster_event_relays(self) -> None:
        """Create server relays for quirk-driven zha event forwarding."""
        for cluster in self.zigpy_endpoint.in_clusters.values():
            self._ensure_server_cluster_event_relay(cluster)

    def _ensure_server_cluster_event_relay(self, cluster: Any) -> None:
        """Ensure a server cluster relay exists for quirk zha event forwarding."""
        existing = self._server_cluster_event_relays.get(int(cluster.cluster_id))
        if existing is not None:
            return

        relay = _ServerClusterEventRelay(self, cluster)
        cluster.add_listener(relay)
        self._server_cluster_event_relays[int(cluster.cluster_id)] = (cluster, relay)

    def _ensure_client_cluster_event_relay(self, cluster: Any) -> None:
        """Ensure a client cluster command relay exists for a zigpy cluster."""
        existing = self._client_cluster_event_relays.get(int(cluster.cluster_id))
        if existing is not None:
            return

        relay = _ClientClusterEventRelay(self, cluster)
        cluster.add_listener(relay)
        self._client_cluster_event_relays[int(cluster.cluster_id)] = (cluster, relay)

    async def async_initialize(self, from_cache: bool = False) -> None:
        """Initialize claimed clusters."""
        merged_cluster_configs = self._apply_merged_cluster_configs()
        await self._execute_cluster_tasks(
            "async_initialize",
            from_cache,
            max_concurrency=1,
            merged_cluster_configs=merged_cluster_configs,
        )

    async def async_configure(self) -> None:
        """Configure claimed clusters."""
        merged_cluster_configs = self._apply_merged_cluster_configs()
        await self._execute_cluster_tasks(
            "async_configure",
            merged_cluster_configs=merged_cluster_configs,
        )

    def reset_cluster_config_contributions(self) -> None:
        """Clear any entity/quirk cluster configuration contributions."""
        self._cluster_config_merger.reset()
        self._cluster_config_order = 0

    def add_cluster_config_contribution(
        self, contribution: ClusterConfigContribution
    ) -> None:
        """Record a cluster configuration contribution for this endpoint."""
        self._cluster_config_merger.add(contribution)

    def next_cluster_config_order(self) -> int:
        """Return a monotonically increasing contribution order value."""
        order = self._cluster_config_order
        self._cluster_config_order += 1
        return order

    @staticmethod
    def _normalize_cluster_defaults(
        *,
        bind: bool | None,
        reporting: tuple[dict[str, Any], ...] | list[dict[str, Any]],
        init_attrs: dict[str, bool] | None,
    ) -> tuple[bool | None, tuple[ReportingConfig, ...], dict[str, bool]]:
        """Normalize raw default values into endpoint config primitives."""
        normalized_reporting = tuple(
            ReportingConfig(attribute=cfg["attr"], config=cfg["config"])
            for cfg in reporting
        )
        return bind, normalized_reporting, dict(init_attrs or {})

    def get_cluster_defaults_by_cluster_id(
        self, cluster_id: int, *, is_client: bool
    ) -> tuple[bool | None, tuple[ReportingConfig, ...], dict[str, bool]]:
        """Return default bind/report/init values for a cluster id."""
        cluster = (
            self.zigpy_endpoint.out_clusters.get(cluster_id)
            if is_client
            else self.zigpy_endpoint.in_clusters.get(cluster_id)
        )
        if cluster is None:
            return None, (), {}

        bind, reporting, init_attrs = (
            cluster_metadata.get_cluster_default_configuration(
                cluster,
                self,
                is_client=is_client,
            )
        )
        return self._normalize_cluster_defaults(
            bind=bind,
            reporting=reporting,
            init_attrs=init_attrs,
        )

    def _add_cluster_config_from_defaults(
        self,
        *,
        target: ClusterTarget,
        default_bind: bool | None,
        default_reporting: tuple[ReportingConfig, ...],
        default_init_attrs: dict[str, bool],
        source: str,
        feature_priority: int,
        explicit_quirk: bool = False,
        bind_override: bool | None = None,
        reporting_override: tuple[ReportingConfig, ...] = (),
        init_attr_override: dict[str, bool] | None = None,
        entity_cluster_config: EntityClusterConfig | None = None,
    ) -> None:
        """Add a cluster configuration contribution using provided defaults."""
        bind = default_bind
        reporting = default_reporting
        init_attrs = dict(default_init_attrs)

        if bind_override is not None:
            bind = bind_override

        if explicit_quirk:
            reporting = reporting_override
        elif entity_cluster_config and entity_cluster_config.reporting is not None:
            reporting = entity_cluster_config.reporting
        elif reporting_override:
            reporting = reporting_override

        if explicit_quirk:
            init_attrs = dict(init_attr_override or {})
        else:
            if entity_cluster_config and entity_cluster_config.init_attrs is not None:
                init_attrs = dict(entity_cluster_config.init_attrs)
            if init_attr_override:
                init_attrs.update(init_attr_override)

        if entity_cluster_config and entity_cluster_config.bind is not None:
            bind = entity_cluster_config.bind

        self.add_cluster_config_contribution(
            ClusterConfigContribution(
                target=target,
                source=source,
                order=self.next_cluster_config_order(),
                feature_priority=feature_priority,
                explicit_quirk=explicit_quirk,
                bind=bind,
                reporting=reporting,
                init_attrs=init_attrs,
            )
        )

    def _get_cluster_by_cluster_id(
        self,
        cluster_id: int,
        *,
        is_client: bool,
    ) -> Any | None:
        """Return zigpy cluster by id and direction."""
        return (
            self.zigpy_endpoint.out_clusters.get(cluster_id)
            if is_client
            else self.zigpy_endpoint.in_clusters.get(cluster_id)
        )

    def _get_cluster_by_ref(
        self,
        cluster_name: str,
        *,
        is_client: bool,
    ) -> Any | None:
        """Return zigpy cluster by cluster ref."""
        with contextlib.suppress(KeyError):
            cluster, _unique_id = self.resolve_cluster_and_unique_id_for_ref(
                cluster_name,
                is_client=is_client,
            )
            return cluster
        return None

    def add_cluster_config_for_cluster_name(
        self,
        *,
        cluster_name: str,
        is_client: bool,
        source: str,
        feature_priority: int,
        explicit_quirk: bool = False,
        bind_override: bool | None = None,
        reporting_override: tuple[ReportingConfig, ...] = (),
        init_attr_override: dict[str, bool] | None = None,
        entity_cluster_config: EntityClusterConfig | None = None,
    ) -> None:
        """Add a cluster configuration contribution by cluster name."""
        cluster = self._get_cluster_by_ref(cluster_name, is_client=is_client)
        if cluster is None:
            return

        bind, reporting, init_attrs = self.get_cluster_defaults_by_cluster_id(
            cluster.cluster_id,
            is_client=is_client,
        )
        self._add_cluster_config_from_defaults(
            target=cluster_target_from_cluster(cluster),
            default_bind=bind,
            default_reporting=reporting,
            default_init_attrs=init_attrs,
            source=source,
            feature_priority=feature_priority,
            explicit_quirk=explicit_quirk,
            bind_override=bind_override,
            reporting_override=reporting_override,
            init_attr_override=init_attr_override,
            entity_cluster_config=entity_cluster_config,
        )

    def add_cluster_config_for_cluster_id(
        self,
        *,
        cluster_id: int,
        is_client: bool,
        source: str,
        feature_priority: int,
        explicit_quirk: bool = False,
        bind_override: bool | None = None,
        reporting_override: tuple[ReportingConfig, ...] = (),
        init_attr_override: dict[str, bool] | None = None,
        entity_cluster_config: EntityClusterConfig | None = None,
    ) -> None:
        """Add a cluster configuration contribution by cluster id."""
        cluster = self._get_cluster_by_cluster_id(cluster_id, is_client=is_client)
        if cluster is None:
            return

        bind, reporting, init_attrs = self.get_cluster_defaults_by_cluster_id(
            cluster_id,
            is_client=is_client,
        )
        self._add_cluster_config_from_defaults(
            target=cluster_target_from_cluster(cluster),
            default_bind=bind,
            default_reporting=reporting,
            default_init_attrs=init_attrs,
            source=source,
            feature_priority=feature_priority,
            explicit_quirk=explicit_quirk,
            bind_override=bind_override,
            reporting_override=reporting_override,
            init_attr_override=init_attr_override,
            entity_cluster_config=entity_cluster_config,
        )

    def _apply_merged_cluster_configs(
        self,
    ) -> dict[ClusterTarget, MergedClusterConfig]:
        """Merge cluster config contributions for this endpoint."""
        merged = self._cluster_config_merger.merge()
        if not merged:
            return {}

        for target in merged:
            if target.endpoint_id != self.id:
                continue

            cluster_ref = self.get_cluster_ref_by_cluster_id(
                target.cluster_id,
                is_client=target.is_client,
            )
            if cluster_ref is not None:
                self._claimed_cluster_refs.add(cluster_ref)

        return merged

    async def _execute_cluster_tasks(
        self,
        func_name: str,
        *args: Any,
        max_concurrency: int | None = None,
        merged_cluster_configs: dict[ClusterTarget, MergedClusterConfig] | None = None,
    ) -> None:
        """Execute a throttled cluster-stage task for each claimed cluster ref."""
        execution_plan = tuple(sorted(self._claimed_cluster_refs))

        async def _execute_stage(cluster_ref: tuple[str, bool]) -> Any:
            cluster_name, is_client = cluster_ref
            cluster = self._get_cluster_by_ref(cluster_name, is_client=is_client)
            if cluster is None:
                return None
            return await self._execute_direct_cluster_task(
                cluster=cluster,
                func_name=func_name,
                args=args,
                merged_cluster_configs=merged_cluster_configs,
            )

        tasks = [_execute_stage(cluster_ref) for cluster_ref in execution_plan]

        gather: Callable[..., Awaitable]

        if max_concurrency is None:
            gather = asyncio.gather
        else:
            gather = functools.partial(gather_with_limited_concurrency, max_concurrency)

        results = await gather(*tasks, return_exceptions=True)
        for cluster_ref, outcome in zip(execution_plan, results):
            cluster_name, is_client = cluster_ref
            cluster = self._get_cluster_by_ref(cluster_name, is_client=is_client)
            if cluster is None:
                continue

            if isinstance(outcome, Exception):
                self._log_cluster_stage(
                    cluster,
                    "'%s' stage failed: %s",
                    func_name,
                    str(outcome),
                    exc_info=outcome,
                )
            else:
                self._log_cluster_stage(cluster, "'%s' stage succeeded", func_name)

    def _log_cluster_stage(
        self, cluster: Any, msg: str, *args: Any, **kwargs: Any
    ) -> None:
        """Log cluster-stage messages with endpoint/cluster context."""
        formatted = msg % args if args else msg
        _LOGGER.debug(
            "[%s:%s]: %s",
            self.device.nwk,
            f"{self.id}:0x{cluster.cluster_id:04x}",
            formatted,
            **kwargs,
        )

    def _log_cluster_warning(self, cluster: Any, msg: str, *args: Any) -> None:
        """Log cluster warnings with endpoint/cluster context."""
        formatted = msg % args if args else msg
        _LOGGER.warning(
            "[%s:%s]: %s",
            self.device.nwk,
            f"{self.id}:0x{cluster.cluster_id:04x}",
            formatted,
        )

    @staticmethod
    def _reporting_configs_to_dicts(
        reporting: tuple[ReportingConfig, ...],
    ) -> tuple[dict[str, Any], ...]:
        """Convert entity merge reporting configs to cluster request dicts."""
        return tuple(
            {"attr": conf.attribute, "config": conf.config} for conf in reporting
        )

    def _default_merged_config_for_cluster(self, cluster: Any) -> MergedClusterConfig:
        """Return merged-config representation of current defaults for a cluster."""
        bind, reporting, init_attrs = self.get_cluster_defaults_by_cluster_id(
            cluster.cluster_id,
            is_client=cluster.is_client,
        )
        return MergedClusterConfig(
            bind=bind,
            reporting=reporting,
            init_attrs=init_attrs,
        )

    @staticmethod
    def _build_reporting_event_data(
        cluster: Any,
        report_config: tuple[dict[str, Any], ...],
    ) -> dict[str, dict[str, Any]]:
        """Build reporting event payload from report configuration."""
        event_data: dict[str, dict[str, Any]] = {}
        for attr_report in report_config:
            attr, config = attr_report["attr"], attr_report["config"]

            try:
                attr_name = cluster.find_attribute(attr).name
            except KeyError:
                attr_name = attr

            event_data[attr_name] = {
                "min": config[0],
                "max": config[1],
                "id": attr,
                "name": attr_name,
                "change": config[2],
                "status": None,
            }

        return event_data

    @staticmethod
    def _configure_reporting_status(
        *,
        cluster: Any,
        attrs: dict[Any, ZigpyReportingConfig],
        result: list[ConfigureReportingResponseRecord],
        event_data: dict[str, dict[str, Any]],
        debug: Callable[..., None],
    ) -> None:
        """Parse configure reporting result."""
        attr_names = {attr_def.name for attr_def in attrs}

        if not result:
            debug(
                "attr reporting for '%s' on '%s': %s",
                attr_names,
                cluster.ep_attribute,
                result,
            )
            for attr_name in attr_names:
                event_data[attr_name]["status"] = Status.FAILURE.name
            return

        if len(result) == 1 and result[0].status == Status.SUCCESS:
            debug(
                "Successfully configured reporting for '%s' on '%s' cluster: %s",
                attr_names,
                cluster.ep_attribute,
                result,
            )
            for attr_name in attr_names:
                event_data[attr_name]["status"] = Status.SUCCESS.name
            return

        for record in result:
            event_data[cluster.find_attribute(record.attrid).name]["status"] = (
                record.status.name
            )

        failed = [
            cluster.find_attribute(record.attrid).name
            for record in result
            if record.status != Status.SUCCESS
        ]
        debug(
            "Failed to configure reporting for '%s' on '%s' cluster: %s",
            failed,
            cluster.ep_attribute,
            result,
        )
        success = attr_names - set(failed)
        debug(
            "Successfully configured reporting for '%s' on '%s' cluster",
            success,
            cluster.ep_attribute,
        )
        for attr_name in success:
            event_data[attr_name]["status"] = Status.SUCCESS.name

    async def _bind_cluster(
        self,
        *,
        cluster: Any,
        unique_id: str,
        debug: Callable[..., None],
    ) -> None:
        """Bind a zigbee cluster and emit bind event."""
        try:
            result = await RETRYABLE_REQUEST_DECORATOR(cluster.bind)()
            debug("bound '%s' cluster: %s", cluster.ep_attribute, result[0])
            success = result[0] == 0
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
            debug(
                "Failed to bind '%s' cluster: %s",
                cluster.ep_attribute,
                str(ex),
                exc_info=ex,
            )
            success = False

        self.device.emit(
            ZHA_CLUSTER_MSG_BIND,
            ClusterBindEvent(
                cluster_name=cluster.name,
                cluster_id=cluster.cluster_id,
                cluster_unique_id=unique_id,
                success=success,
            ),
        )

    async def _configure_cluster_reporting(
        self,
        *,
        cluster: Any,
        unique_id: str,
        report_config: tuple[dict[str, Any], ...],
        debug: Callable[..., None],
    ) -> None:
        """Configure attribute reporting for a cluster and emit result event."""
        event_data = self._build_reporting_event_data(cluster, report_config)
        to_configure = [*report_config]
        chunk = to_configure[:REPORT_CONFIG_ATTR_PER_REQ]
        rest = to_configure[REPORT_CONFIG_ATTR_PER_REQ:]

        while chunk:
            reports = {
                cluster.find_attribute(rec["attr"]): ZigpyReportingConfig(
                    *rec["config"]
                )
                for rec in chunk
            }
            try:
                result = await RETRYABLE_REQUEST_DECORATOR(
                    cluster.configure_reporting_multiple
                )(reports)
                self._configure_reporting_status(
                    cluster=cluster,
                    attrs=reports,
                    result=result,
                    event_data=event_data,
                    debug=debug,
                )
            except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
                debug(
                    "failed to set reporting on '%s' cluster for: %s",
                    cluster.ep_attribute,
                    str(ex),
                )
                break

            chunk = rest[:REPORT_CONFIG_ATTR_PER_REQ]
            rest = rest[REPORT_CONFIG_ATTR_PER_REQ:]

        self.device.emit(
            ZHA_CLUSTER_MSG_CFG_RPT,
            ClusterConfigureReportingEvent(
                cluster_name=cluster.name,
                cluster_id=cluster.cluster_id,
                cluster_unique_id=unique_id,
                attributes=event_data,
            ),
        )

    async def async_write_cluster_attributes_safe(
        self,
        *,
        cluster: Any,
        attributes: dict[str, Any],
    ) -> None:
        """Write attributes and raise `ZHAException` for failures."""
        await cluster_write_attributes_safe(cluster, attributes)

    async def async_get_cluster_attributes(
        self,
        *,
        cluster: Any,
        attributes: list[int | str],
        raise_exceptions: bool,
        debug: Callable[..., None] | None = None,
        from_cache: bool = True,
        only_cache: bool = True,
    ) -> dict[int | str, Any]:
        """Get attribute values in chunks, with retries."""
        logger = debug or (lambda *args, **kwargs: None)
        chunk = attributes[:CLUSTER_READS_PER_REQ]
        rest = attributes[CLUSTER_READS_PER_REQ:]
        result: dict[int | str, Any] = {}

        while chunk:
            try:
                logger("Reading attributes in chunks: %s", chunk)
                read, _ = await RETRYABLE_REQUEST_DECORATOR(cluster.read_attributes)(
                    chunk,
                    allow_cache=from_cache,
                    only_cache=only_cache,
                    manufacturer=UNDEFINED,
                )
                logger("Got attributes: %s", read)
                result.update(read)
            except (TimeoutError, zigpy.exceptions.ZigbeeException) as ex:
                logger(
                    "failed to get attributes '%s' on '%s' cluster: %s",
                    chunk,
                    cluster.ep_attribute,
                    str(ex),
                )
                if raise_exceptions:
                    raise

            chunk = rest[:CLUSTER_READS_PER_REQ]
            rest = rest[CLUSTER_READS_PER_REQ:]

        return result

    async def _initialize_cluster(
        self,
        *,
        cluster: Any,
        init_attrs: dict[str, bool],
        report_config: tuple[dict[str, Any], ...],
        from_cache: bool,
        skip_configuration: bool,
        debug: Callable[..., None],
        specific_init: Callable[[bool], Awaitable[Any]] | None = None,
    ) -> None:
        """Initialize cluster cache by reading configured/init attributes."""
        if not from_cache and skip_configuration:
            debug("Skipping cluster initialization")
            return

        debug("initializing cluster: from_cache: %s", from_cache)
        cached = [
            attribute for attribute, cached_attr in init_attrs.items() if cached_attr
        ]
        uncached = [
            attribute
            for attribute, cached_attr in init_attrs.items()
            if not cached_attr
        ]
        uncached.extend([cfg["attr"] for cfg in report_config])

        if cached:
            debug("initializing cached cluster attributes: %s", cached)
            await self.async_get_cluster_attributes(
                cluster=cluster,
                attributes=cached,
                raise_exceptions=True,
                debug=debug,
                from_cache=True,
                only_cache=from_cache,
            )

        if uncached:
            debug(
                "initializing uncached cluster attributes: %s - from cache[%s]",
                uncached,
                from_cache,
            )
            await self.async_get_cluster_attributes(
                cluster=cluster,
                attributes=uncached,
                raise_exceptions=True,
                debug=debug,
                from_cache=from_cache,
                only_cache=from_cache,
            )

        if specific_init is not None:
            debug("Performing cluster-specific initialization: %s", uncached)
            await specific_init(from_cache)

        debug("finished cluster initialization")

    async def _execute_direct_cluster_task(
        self,
        *,
        cluster: Any,
        func_name: str,
        args: tuple[Any, ...],
        merged_cluster_configs: dict[ClusterTarget, MergedClusterConfig] | None,
    ) -> None:
        """Execute configure/initialize stage directly on a zigpy cluster."""
        merged_config = self._default_merged_config_for_cluster(cluster)
        if merged_cluster_configs is not None:
            merged_config = merged_cluster_configs.get(
                cluster_target_from_cluster(cluster), merged_config
            )

        report_config = self._reporting_configs_to_dicts(merged_config.reporting)
        unique_id = self._legacy_unique_id_for_cluster(cluster)
        debug = functools.partial(self._log_cluster_stage, cluster)

        if func_name == "async_configure":
            if cluster.cluster_id == IasZone.cluster_id:
                await self._direct_configure_ias_zone(
                    cluster=cluster, unique_id=unique_id
                )
                return

            if cluster.cluster_id == LightLink.cluster_id:
                await self._direct_configure_lightlink(cluster=cluster)
                return

            if self.device.skip_configuration:
                debug("skipping cluster configuration")
                return

            if merged_config.bind:
                debug("Performing cluster binding")
                await self._bind_cluster(
                    cluster=cluster,
                    unique_id=unique_id,
                    debug=debug,
                )

            if cluster.is_server:
                debug("Configuring cluster attribute reporting")
                await self._configure_cluster_reporting(
                    cluster=cluster,
                    unique_id=unique_id,
                    report_config=report_config,
                    debug=debug,
                )
            return

        if func_name == "async_initialize":
            from_cache = bool(args[0]) if args else False
            await self._initialize_cluster(
                cluster=cluster,
                init_attrs=merged_config.init_attrs,
                report_config=report_config,
                from_cache=from_cache,
                skip_configuration=self.device.skip_configuration,
                debug=debug,
            )
            if cluster.cluster_id == PowerConfiguration.cluster_id:
                await self._direct_initialize_power_configuration(
                    cluster=cluster,
                    from_cache=from_cache,
                )
            if cluster.cluster_id == AQARA_OPPLE_CLUSTER:
                self._direct_initialize_opple(cluster=cluster)
            return

        raise AttributeError(f"Unsupported endpoint cluster task: {func_name}")

    async def _direct_configure_ias_zone(self, *, cluster: Any, unique_id: str) -> None:
        """Direct configure path for IAS Zone clusters."""
        debug = functools.partial(self._log_cluster_stage, cluster)
        await self.async_get_cluster_attributes(
            cluster=cluster,
            attributes=[IasZone.AttributeDefs.zone_type.name],
            raise_exceptions=False,
            debug=debug,
            from_cache=False,
            only_cache=False,
        )
        if self.device.skip_configuration:
            debug("skipping IASZoneCluster configuration")
            return

        debug("started IASZoneCluster configuration")
        await self._bind_cluster(
            cluster=cluster,
            unique_id=unique_id,
            debug=debug,
        )
        ieee = cluster.endpoint.device.application.state.node_info.ieee
        try:
            await self.async_write_cluster_attributes_safe(
                cluster=cluster,
                attributes={IasZone.AttributeDefs.cie_addr.name: ieee},
            )
            debug(
                "wrote cie_addr: %s to '%s' cluster",
                str(ieee),
                cluster.ep_attribute,
            )
        except ZHAException as ex:
            debug(
                "Failed to write cie_addr: %s to '%s' cluster: %s",
                str(ieee),
                cluster.ep_attribute,
                str(ex),
            )

        debug("Sending pro-active IAS enroll response")
        cluster.create_catching_task(
            cluster.enroll_response(
                enroll_response_code=IasZone.EnrollResponse.Success,
                zone_id=0,
            )
        )
        debug("finished IASZoneCluster configuration")

    async def _direct_configure_lightlink(self, *, cluster: Any) -> None:
        """Direct configure path for LightLink clusters."""
        debug = functools.partial(self._log_cluster_stage, cluster)
        if self.device.skip_configuration:
            return

        application = self.zigpy_endpoint.device.application
        try:
            coordinator = application.get_device(application.state.node_info.ieee)
        except KeyError:
            self._log_cluster_warning(
                cluster, "Aborting - unable to locate required coordinator device."
            )
            return

        try:
            response = await cluster.get_group_identifiers(0)
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as exc:
            self._log_cluster_warning(
                cluster, "Couldn't get list of groups: %s", str(exc)
            )
            return

        if isinstance(
            response, GENERAL_COMMANDS[GeneralCommand.Default_Response].schema
        ):
            groups = []
        else:
            groups = response.group_info_records

        if groups:
            for group in groups:
                debug("Adding coordinator to 0x%04x group id", group.group_id)
                await coordinator.add_to_group(group.group_id)
            return

        await coordinator.add_to_group(0x0000, name="Lightlink Group")

    async def _direct_initialize_power_configuration(
        self, *, cluster: Any, from_cache: bool
    ) -> None:
        """Direct init path for PowerConfiguration specific reads."""
        debug = functools.partial(self._log_cluster_stage, cluster)
        await self.async_get_cluster_attributes(
            cluster=cluster,
            attributes=[
                PowerConfiguration.AttributeDefs.battery_size.name,
                PowerConfiguration.AttributeDefs.battery_quantity.name,
            ],
            raise_exceptions=False,
            debug=debug,
            from_cache=from_cache,
            only_cache=from_cache,
        )

    def _direct_initialize_opple(self, *, cluster: Any) -> None:
        """Direct init path for Opple model-specific side effects."""
        if cluster.endpoint.model not in ("lumi.motion.ac02", "lumi.motion.agl04"):
            return

        interval = cluster.get("detection_interval", cluster.get(0x0102))
        if interval is None:
            return

        self._log_cluster_stage(
            cluster, "Loaded detection interval at startup: %s", interval
        )
        cluster.endpoint.ias_zone.reset_s = int(interval)

    def emit_zha_event(self, event_data: dict[str, Any]) -> None:
        """Broadcast an event from this endpoint."""
        self.device.emit_zha_event(
            {
                const.ATTR_UNIQUE_ID: self.unique_id,
                const.ATTR_ENDPOINT_ID: self.id,
                **event_data,
            }
        )


class _ClientClusterEventRelay:
    """Relay client cluster commands to endpoint zha_event payloads."""

    def __init__(self, endpoint: Endpoint, cluster: Any) -> None:
        self._endpoint = endpoint
        self._cluster = cluster

    def cluster_command(self, tsn: int, command_id: int, args: Any) -> None:
        """Handle client cluster commands and emit a zha_event payload."""
        del tsn
        server_commands = getattr(self._cluster, "server_commands", None)
        if server_commands is None or command_id not in server_commands:
            return

        command_name = server_commands[command_id].name
        serialized_args, params = self._serialize_command_args(args)
        self._endpoint.emit_zha_event(
            {
                const.ATTR_UNIQUE_ID: self._endpoint._legacy_unique_id_for_cluster(
                    self._cluster
                ),
                const.ATTR_CLUSTER_ID: self._cluster.cluster_id,
                const.ATTR_COMMAND: command_name,
                const.ATTR_ARGS: serialized_args,
                const.ATTR_PARAMS: params,
            }
        )

    @staticmethod
    def _serialize_command_args(args: Any) -> tuple[list[Any] | dict[str, Any], dict]:
        """Serialize inbound cluster command args with legacy payload parity."""
        if isinstance(args, CommandSchema):
            return [arg for arg in args if arg is not None], args.as_dict()
        if isinstance(args, (list, dict)):
            return args, {}
        return [args], {}


class _ServerClusterEventRelay:
    """Relay quirk-triggered server cluster events to endpoint zha_event payloads."""

    def __init__(self, endpoint: Endpoint, cluster: Any) -> None:
        self._endpoint = endpoint
        self._cluster = cluster

    def emit_zha_event(self, command: str, arg: Any) -> None:
        """Compatibility method used by quirks to emit zha events."""
        self.zha_send_event(command, arg)

    def zha_send_event(self, command: str, arg: Any) -> None:
        """Handle quirk zha_send_event relays and emit endpoint payload."""
        args, params = _ClientClusterEventRelay._serialize_command_args(arg)
        self._endpoint.emit_zha_event(
            {
                const.ATTR_UNIQUE_ID: self._endpoint._legacy_unique_id_for_cluster(
                    self._cluster
                ),
                const.ATTR_CLUSTER_ID: self._cluster.cluster_id,
                const.ATTR_COMMAND: command,
                const.ATTR_ARGS: args,
                const.ATTR_PARAMS: params,
            }
        )
