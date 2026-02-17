"""Representation of a Zigbee endpoint for zha."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import functools
import itertools
import logging
from typing import TYPE_CHECKING, Any, Final

import zigpy.exceptions
from zigpy.profiles.zha import PROFILE_ID as ZHA_PROFILE_ID
from zigpy.profiles.zll import PROFILE_ID as ZLL_PROFILE_ID
from zigpy.typing import UNDEFINED
from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    Cluster,
)
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.clusters.general import Identify, OnOff, Ota
from zigpy.zcl.clusters.security import IasZone
from zigpy.zcl.foundation import CommandSchema, ConfigureReportingResponseRecord, Status
from zigpy.zcl.helpers import ReportingConfig

from zha.application import const
from zha.application.const import (
    ZHA_CLUSTER_MSG,
    ZHA_CLUSTER_MSG_BIND,
    ZHA_CLUSTER_MSG_CFG_RPT,
)
from zha.application.helpers import resolve_incoming_cluster_command_name, retry_request
from zha.exceptions import ZHAException
from zha.zigbee.const import (
    ARGS,
    ATTRIBUTE_ID,
    ATTRIBUTE_NAME,
    ATTRIBUTE_VALUE,
    CLUSTER_ID,
    CLUSTER_READS_PER_REQ,
    COMMAND,
    PARAMS,
    REPORT_CONFIG_ATTR_PER_REQ,
    SIGNAL_ATTR_UPDATED,
    UNIQUE_ID,
    UNKNOWN,
    VALUE,
)

if TYPE_CHECKING:
    from zigpy import Endpoint as ZigpyEndpoint

    from zha.zigbee.device import Device

ATTR_DEVICE_TYPE: Final[str] = "device_type"
ATTR_PROFILE_ID: Final[str] = "profile_id"
ATTR_IN_CLUSTERS: Final[str] = "input_clusters"
ATTR_OUT_CLUSTERS: Final[str] = "output_clusters"

SERVER_BIND_FALSE_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        0x0000,  # Basic
        0x0003,  # Identify
        0x0004,  # Groups
        0x0019,  # OTA
        0x0021,  # GreenPowerProxy
        0x1000,  # LightLink
    }
)
CLIENT_BIND_FALSE_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        0x0019,  # OTA client
    }
)
CLIENT_COMMAND_SUPPRESSED_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        0xFC31,  # InovelliNotification client
    }
)
CLIENT_ATTRIBUTE_EVENT_SUPPRESSED_CLUSTER_IDS: Final[frozenset[int]] = frozenset(
    {
        Ota.cluster_id,
        0xFC31,  # InovelliNotification client
    }
)

_LOGGER = logging.getLogger(__name__)


@dataclass(kw_only=True, frozen=True)
class ClusterBindEvent:
    """Event generated when the cluster is bound."""

    cluster_name: str
    cluster_id: int
    success: bool
    cluster_handler_unique_id: str
    event_type: Final[str] = ZHA_CLUSTER_MSG
    event: Final[str] = ZHA_CLUSTER_MSG_BIND


@dataclass(kw_only=True, frozen=True)
class ClusterConfigureReportingEvent:
    """Event generated when a cluster configures attribute reporting."""

    cluster_name: str
    cluster_id: int
    attributes: dict[str, dict[str, Any]]
    cluster_handler_unique_id: str
    event_type: Final[str] = ZHA_CLUSTER_MSG
    event: Final[str] = ZHA_CLUSTER_MSG_CFG_RPT


def _merge_aggressive_reporting_config(
    existing: tuple[int, int, int | float],
    incoming: tuple[int, int, int | float],
) -> tuple[int, int, int | float]:
    """Merge reporting configs by choosing the fastest/lowest thresholds."""
    return (
        min(existing[0], incoming[0]),
        min(existing[1], incoming[1]),
        min(existing[2], incoming[2]),
    )


class _ClusterListener:
    """Cluster listener wrapper to preserve the source cluster context."""

    def __init__(self, endpoint: Endpoint, cluster: Cluster) -> None:
        self._endpoint = endpoint
        self._cluster = cluster

    def cluster_command(self, tsn: int, command_id: int, args: list[Any]) -> None:
        """Forward cluster commands with cluster context."""
        self._endpoint.handle_cluster_command(self._cluster, tsn, command_id, args)


class Endpoint:
    """Endpoint for a zha device."""

    def __init__(self, zigpy_endpoint: ZigpyEndpoint, device: Device) -> None:
        """Initialize instance."""
        assert zigpy_endpoint is not None
        assert device is not None
        self._zigpy_endpoint: ZigpyEndpoint = zigpy_endpoint
        self._device: Device = device
        self._unique_id: str = f"{device.unique_id}-{zigpy_endpoint.endpoint_id}"

        self._in_clusters_by_name: dict[str, Cluster] = {
            self.resolve_cluster_name(cluster): cluster
            for cluster in self._zigpy_endpoint.in_clusters.values()
        }
        self._out_clusters_by_name: dict[str, Cluster] = {
            self.resolve_cluster_name(cluster): cluster
            for cluster in self._zigpy_endpoint.out_clusters.values()
        }
        self._claimed_clusters: dict[str, Cluster] = {}
        self._cluster_bind: dict[str, bool] = {}
        self._cluster_report_config: dict[
            str, dict[str, tuple[int, int, int | float]]
        ] = {}
        self._cluster_direct_report_attrs: dict[str, set[str]] = {}
        self._cluster_init_attrs: dict[str, dict[str, bool]] = {}
        self._cluster_command_owners: dict[str, int] = {}
        self._cluster_event_unsubs: list[Callable[[], None]] = []
        self._cluster_listeners: list[tuple[Cluster, _ClusterListener]] = []
        self._on_off_client_off_listeners: dict[str, asyncio.Handle] = {}

        profile_id = self._zigpy_endpoint.profile_id
        if profile_id is None:
            _LOGGER.debug("Skipping endpoint, profile is None")
            return
        elif profile_id not in (ZLL_PROFILE_ID, ZHA_PROFILE_ID):
            _LOGGER.debug(
                "Skipping endpoint, profile is not ZLL or ZHA: 0x%04X",
                profile_id,
            )
            return

        self._attach_cluster_listeners()
        self._set_device_core_clusters()

    def _set_device_core_clusters(self) -> None:
        """Expose well-known endpoint clusters on the owning device."""
        if power_cluster := self._in_clusters_by_name.get("power"):
            self._device.power_configuration_ch = power_cluster
        if identify_cluster := self._in_clusters_by_name.get("identify"):
            self._device.identify_ch = identify_cluster
        if basic_cluster := self._in_clusters_by_name.get("basic"):
            self._device.basic_ch = basic_cluster

    def _attach_cluster_listeners(self) -> None:
        """Attach listeners to clusters for command and attribute side effects."""
        attr_events = (
            AttributeReadEvent,
            AttributeReportedEvent,
            AttributeUpdatedEvent,
            AttributeWrittenEvent,
        )

        for cluster in itertools.chain(
            self._zigpy_endpoint.in_clusters.values(),
            self._zigpy_endpoint.out_clusters.values(),
        ):
            listener = _ClusterListener(self, cluster)
            cluster.add_listener(listener)
            self._cluster_listeners.append((cluster, listener))

            if cluster.is_client:
                for event_type in attr_events:
                    self._cluster_event_unsubs.append(
                        cluster.on_event(
                            event_type.event_type,
                            functools.partial(
                                self._handle_client_attribute_event, cluster
                            ),
                        )
                    )

    def on_remove(self) -> None:
        """Run when endpoint is removed."""
        for cluster, listener in self._cluster_listeners:
            cluster.remove_listener(listener)
        self._cluster_listeners.clear()

        for unsub in self._cluster_event_unsubs:
            unsub()
        self._cluster_event_unsubs.clear()

        for handle in self._on_off_client_off_listeners.values():
            handle.cancel()
        self._on_off_client_off_listeners.clear()
        self._cluster_command_owners.clear()

    @functools.cached_property
    def device(self) -> Device:
        """Return the device this endpoint belongs to."""
        return self._device

    @functools.cached_property
    def zigpy_endpoint(self) -> ZigpyEndpoint:
        """Return endpoint of zigpy device."""
        return self._zigpy_endpoint

    @functools.cached_property
    def id(self) -> int:
        """Return endpoint id."""
        return self._zigpy_endpoint.endpoint_id

    @property
    def in_clusters_by_name(self) -> dict[str, Cluster]:
        """Return input clusters indexed by cluster match name."""
        return self._in_clusters_by_name

    @property
    def out_clusters_by_name(self) -> dict[str, Cluster]:
        """Return output clusters indexed by cluster match name."""
        return self._out_clusters_by_name

    @property
    def in_clusters(self) -> dict[int, Cluster]:
        """Return input clusters indexed by cluster id."""
        return self._zigpy_endpoint.in_clusters

    @property
    def out_clusters(self) -> dict[int, Cluster]:
        """Return output clusters indexed by cluster id."""
        return self._zigpy_endpoint.out_clusters

    @property
    def claimed_clusters(self) -> dict[str, Cluster]:
        """Return claimed clusters indexed by runtime cluster key."""
        return self._claimed_clusters

    @staticmethod
    def resolve_cluster_name(cluster: Cluster) -> str:
        """Resolve canonical cluster match name from a zigpy cluster."""
        return cluster.ep_attribute or f"cluster_0x{cluster.cluster_id:04x}"

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
        """Create a new endpoint."""
        return cls(zigpy_endpoint, device)

    def _cluster_key(self, cluster: Cluster) -> str:
        suffix = "_client" if cluster.is_client else ""
        return f"{self.id}:0x{cluster.cluster_id:04x}{suffix}"

    def _cluster_unique_id(self, cluster: Cluster) -> str:
        unique_id = f"{self.unique_id.replace('-', ':')}:0x{cluster.cluster_id:04x}"
        if cluster.is_client:
            unique_id = f"{unique_id}_CLIENT"
        return unique_id

    def _default_bind(self, cluster: Cluster) -> bool:
        if cluster.is_client:
            return cluster.cluster_id not in CLIENT_BIND_FALSE_CLUSTER_IDS
        return cluster.cluster_id not in SERVER_BIND_FALSE_CLUSTER_IDS

    def is_cluster_claimed(self, cluster: Cluster) -> bool:
        """Return True if the cluster is already claimed."""
        return self._cluster_key(cluster) in self._claimed_clusters

    def claim_clusters(self, clusters: list[Cluster]) -> None:
        """Claim clusters for endpoint lifecycle processing."""
        for cluster in clusters:
            cluster_key = self._cluster_key(cluster)
            self._claimed_clusters[cluster_key] = cluster
            self._cluster_bind.setdefault(cluster_key, self._default_bind(cluster))

    def register_cluster_command_owner(self, cluster: Cluster) -> None:
        """Register an entity command owner for the given cluster."""
        cluster_key = self._cluster_key(cluster)
        self._cluster_command_owners[cluster_key] = (
            self._cluster_command_owners.get(cluster_key, 0) + 1
        )

    def unregister_cluster_command_owner(self, cluster: Cluster) -> None:
        """Unregister one entity command owner for the given cluster."""
        cluster_key = self._cluster_key(cluster)
        owners = self._cluster_command_owners.get(cluster_key)
        if owners is None:
            return
        if owners <= 1:
            self._cluster_command_owners.pop(cluster_key, None)
            return
        self._cluster_command_owners[cluster_key] = owners - 1

    def set_cluster_bind(self, cluster: Cluster, bind: bool) -> None:
        """Set/merge bind policy for one cluster."""
        cluster_key = self._cluster_key(cluster)
        existing = self._cluster_bind.get(cluster_key)
        if existing is None:
            self._cluster_bind[cluster_key] = bind
        else:
            # Once bind is disabled by a claim path, keep it disabled.
            self._cluster_bind[cluster_key] = existing and bind

    def set_cluster_report_config(
        self,
        cluster: Cluster,
        report_config: dict[str, tuple[int, int, int | float]],
        direct_attrs: set[str],
    ) -> None:
        """Set/merge report config for one claimed cluster."""
        cluster_key = self._cluster_key(cluster)
        existing_report_map = self._cluster_report_config.setdefault(cluster_key, {})
        existing_direct_attrs = self._cluster_direct_report_attrs.setdefault(
            cluster_key, set()
        )

        for attr, config in report_config.items():
            if attr in direct_attrs:
                existing_report_map[attr] = config
                existing_direct_attrs.add(attr)
                continue

            if attr in existing_direct_attrs:
                continue

            if attr in existing_report_map:
                existing_report_map[attr] = _merge_aggressive_reporting_config(
                    existing_report_map[attr], config
                )
            else:
                existing_report_map[attr] = config

    def set_cluster_init_attrs(
        self, cluster: Cluster, init_attrs: dict[str, bool]
    ) -> None:
        """Set/merge init attrs for one claimed cluster."""
        cluster_key = self._cluster_key(cluster)
        existing_init_attrs = self._cluster_init_attrs.setdefault(cluster_key, {})

        for attr, use_cache in init_attrs.items():
            if attr in existing_init_attrs:
                # Cache conflicts resolve to uncached reads.
                existing_init_attrs[attr] = existing_init_attrs[attr] and use_cache
            else:
                existing_init_attrs[attr] = use_cache

    async def async_initialize(self, from_cache: bool = False) -> None:
        """Initialize claimed clusters."""
        if not from_cache and self.device.skip_configuration:
            _LOGGER.debug("[%s] skipping cluster initialization", self.unique_id)
            return

        for cluster_key in sorted(self._claimed_clusters):
            cluster = self._claimed_clusters[cluster_key]
            try:
                await self._initialize_cluster(cluster, from_cache=from_cache)
                _LOGGER.debug(
                    "[%s:%s] 'async_initialize' stage succeeded",
                    self.device.nwk,
                    cluster_key,
                )
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOGGER.debug(
                    "[%s:%s] 'async_initialize' stage failed: %s",
                    self.device.nwk,
                    cluster_key,
                    str(ex),
                    exc_info=ex,
                )

    async def async_configure(self) -> None:
        """Configure claimed clusters."""
        if self.device.skip_configuration:
            _LOGGER.debug("[%s] skipping cluster configuration", self.unique_id)
            return

        for cluster_key in sorted(self._claimed_clusters):
            cluster = self._claimed_clusters[cluster_key]
            try:
                await self._configure_cluster(cluster)
                _LOGGER.debug(
                    "[%s:%s] 'async_configure' stage succeeded",
                    self.device.nwk,
                    cluster_key,
                )
            except Exception as ex:  # pylint: disable=broad-exception-caught
                _LOGGER.debug(
                    "[%s:%s] 'async_configure' stage failed: %s",
                    self.device.nwk,
                    cluster_key,
                    str(ex),
                    exc_info=ex,
                )

    async def _configure_cluster(self, cluster: Cluster) -> None:
        """Configure bind/reporting for one claimed cluster."""
        cluster_key = self._cluster_key(cluster)
        if self._cluster_bind.get(cluster_key, self._default_bind(cluster)):
            await self._bind_cluster(cluster)

        if cluster.is_server:
            await self._configure_reporting(cluster)

    async def _bind_cluster(self, cluster: Cluster) -> None:
        """Bind one cluster and emit legacy bind event payload."""
        success = False
        try:
            res = await retry_request(cluster.bind)()
            success = res[0] == 0
            _LOGGER.debug(
                "[%s:%s] bound cluster %s: %s",
                self.device.nwk,
                self._cluster_key(cluster),
                self.resolve_cluster_name(cluster),
                res[0],
            )
        except (zigpy.exceptions.ZigbeeException, TimeoutError, ZHAException) as ex:
            _LOGGER.debug(
                "[%s:%s] failed to bind cluster %s: %s",
                self.device.nwk,
                self._cluster_key(cluster),
                self.resolve_cluster_name(cluster),
                str(ex),
                exc_info=ex,
            )

        self.device.emit(
            ZHA_CLUSTER_MSG_BIND,
            ClusterBindEvent(
                cluster_name=cluster.name,
                cluster_id=cluster.cluster_id,
                cluster_handler_unique_id=self._cluster_unique_id(cluster),
                success=success,
            ),
        )

    async def _configure_reporting(self, cluster: Cluster) -> None:
        """Configure reporting for one server cluster and emit legacy event payload."""
        cluster_key = self._cluster_key(cluster)
        report_map = self._cluster_report_config.get(cluster_key, {})
        event_data: dict[str, dict[str, Any]] = {}

        for attr, config in report_map.items():
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

        to_configure = list(report_map.items())
        chunk, rest = (
            to_configure[:REPORT_CONFIG_ATTR_PER_REQ],
            to_configure[REPORT_CONFIG_ATTR_PER_REQ:],
        )
        while chunk:
            reports = {
                cluster.find_attribute(attr): ReportingConfig(*config)
                for attr, config in chunk
            }
            try:
                res = await retry_request(cluster.configure_reporting_multiple)(reports)
                self._configure_reporting_status(cluster, reports, res, event_data)
            except (zigpy.exceptions.ZigbeeException, TimeoutError, ZHAException) as ex:
                _LOGGER.debug(
                    "[%s:%s] failed to set reporting on cluster %s: %s",
                    self.device.nwk,
                    cluster_key,
                    self.resolve_cluster_name(cluster),
                    str(ex),
                )
                break
            chunk, rest = (
                rest[:REPORT_CONFIG_ATTR_PER_REQ],
                rest[REPORT_CONFIG_ATTR_PER_REQ:],
            )

        self.device.emit(
            ZHA_CLUSTER_MSG_CFG_RPT,
            ClusterConfigureReportingEvent(
                cluster_name=cluster.name,
                cluster_id=cluster.cluster_id,
                cluster_handler_unique_id=self._cluster_unique_id(cluster),
                attributes=event_data,
            ),
        )

    @staticmethod
    def _configure_reporting_status(
        cluster: Cluster,
        attrs: dict[Any, ReportingConfig],
        res: list[ConfigureReportingResponseRecord],
        event_data: dict[str, dict[str, Any]],
    ) -> None:
        """Parse configure reporting result."""
        attr_names = {attr_def.name for attr_def in attrs}

        if not res:
            for attr_name in attr_names:
                event_data[attr_name]["status"] = Status.FAILURE.name
            return

        if len(res) == 1 and res[0].status == Status.SUCCESS:
            for attr_name in attr_names:
                event_data[attr_name]["status"] = Status.SUCCESS.name
            return

        for record in res:
            event_data[cluster.find_attribute(record.attrid).name]["status"] = (
                record.status.name
            )

        failed = {
            cluster.find_attribute(record.attrid).name
            for record in res
            if record.status != Status.SUCCESS
        }
        for attr_name in attr_names - failed:
            event_data[attr_name]["status"] = Status.SUCCESS.name

    async def _initialize_cluster(self, cluster: Cluster, *, from_cache: bool) -> None:
        """Initialize one claimed cluster."""
        cluster_key = self._cluster_key(cluster)
        init_attrs = self._cluster_init_attrs.get(cluster_key, {})
        report_map = self._cluster_report_config.get(cluster_key, {})

        cached = [attr for attr, use_cache in init_attrs.items() if use_cache]
        uncached = [attr for attr, use_cache in init_attrs.items() if not use_cache]
        uncached.extend(report_map.keys())

        # Keep ordering stable while removing duplicates.
        cached = list(dict.fromkeys(cached))
        uncached = list(dict.fromkeys(uncached))

        if cached:
            await self._read_cluster_attributes(
                cluster,
                cached,
                allow_cache=True,
                only_cache=from_cache,
            )
        if uncached:
            await self._read_cluster_attributes(
                cluster,
                uncached,
                allow_cache=from_cache,
                only_cache=from_cache,
            )

    async def _read_cluster_attributes(
        self,
        cluster: Cluster,
        attributes: list[str],
        *,
        allow_cache: bool,
        only_cache: bool,
    ) -> None:
        """Read attributes in chunks with retries and exception swallowing."""
        chunk = attributes[:CLUSTER_READS_PER_REQ]
        rest = attributes[CLUSTER_READS_PER_REQ:]

        while chunk:
            try:
                await retry_request(cluster.read_attributes)(
                    chunk,
                    allow_cache=allow_cache,
                    only_cache=only_cache,
                    manufacturer=UNDEFINED,  # quirk compatibility
                )
            except (zigpy.exceptions.ZigbeeException, TimeoutError, ZHAException) as ex:
                _LOGGER.debug(
                    "[%s:%s] failed to read attrs %s on cluster %s: %s",
                    self.device.nwk,
                    self._cluster_key(cluster),
                    chunk,
                    self.resolve_cluster_name(cluster),
                    str(ex),
                )
            chunk = rest[:CLUSTER_READS_PER_REQ]
            rest = rest[CLUSTER_READS_PER_REQ:]

    def emit_cluster_zha_event(
        self, cluster: Cluster, command: str, arg: list | dict | CommandSchema
    ) -> None:
        """Emit a zha_event payload with legacy key/value semantics."""
        args: list | dict
        if isinstance(arg, CommandSchema):
            args = [a for a in arg if a is not None]
            params = arg.as_dict()
        elif isinstance(arg, (list, dict)):
            args = arg
            params = {}
        else:
            raise TypeError(f"Unexpected cluster command argument: {arg!r}")

        self.emit_zha_event(
            {
                UNIQUE_ID: self._cluster_unique_id(cluster),
                CLUSTER_ID: cluster.cluster_id,
                COMMAND: command,
                ARGS: args,
                PARAMS: params,
            }
        )

    def _handle_client_attribute_event(
        self,
        cluster: Cluster,
        event: AttributeReadEvent
        | AttributeReportedEvent
        | AttributeUpdatedEvent
        | AttributeWrittenEvent,
    ) -> None:
        """Handle client cluster attribute updates with legacy zha_event payload."""
        if cluster.cluster_id in CLIENT_ATTRIBUTE_EVENT_SUPPRESSED_CLUSTER_IDS:
            return

        self.emit_cluster_zha_event(
            cluster,
            SIGNAL_ATTR_UPDATED,
            {
                ATTRIBUTE_ID: event.attribute_id,
                ATTRIBUTE_NAME: event.attribute_name or UNKNOWN,
                ATTRIBUTE_VALUE: event.value,
                VALUE: event.value,
            },
        )

    def _resolve_command_name(self, cluster: Cluster, command_id: int) -> str:
        return resolve_incoming_cluster_command_name(cluster, command_id)

    def handle_cluster_command(
        self, cluster: Cluster, tsn: int, command_id: int, args: list[Any]
    ) -> None:
        """Handle cluster commands and preserve legacy side effects and events."""
        command_name = self._resolve_command_name(cluster, command_id)
        cluster_key = self._cluster_key(cluster)

        _LOGGER.debug(
            "[%s:%s] received command '%s' args=%s tsn=%s",
            self.device.nwk,
            cluster_key,
            command_name,
            args,
            tsn,
        )

        if self._cluster_command_owners.get(cluster_key, 0) > 0:
            return

        if cluster.is_client and cluster.cluster_id == OnOff.cluster_id:
            if command_name in (
                OnOff.ServerCommandDefs.off.name,
                OnOff.ServerCommandDefs.off_with_effect.name,
            ):
                cluster.update_attribute(OnOff.AttributeDefs.on_off.id, False)
            elif command_name in (
                OnOff.ServerCommandDefs.on.name,
                OnOff.ServerCommandDefs.on_with_recall_global_scene.name,
            ):
                cluster.update_attribute(OnOff.AttributeDefs.on_off.id, True)
            elif command_name == OnOff.ServerCommandDefs.on_with_timed_off.name:
                should_accept = args[0]
                on_time = args[1]
                on_off = bool(cluster.get(OnOff.AttributeDefs.on_off.name))
                # 0 = always accept, 1 = accept only if already on.
                if should_accept == 0 or (should_accept == 1 and on_off):
                    if (
                        off_listener := self._on_off_client_off_listeners.get(
                            cluster_key
                        )
                    ) is not None:
                        off_listener.cancel()
                        self._on_off_client_off_listeners.pop(cluster_key, None)
                    cluster.update_attribute(OnOff.AttributeDefs.on_off.id, True)
                    if on_time > 0:
                        self._on_off_client_off_listeners[cluster_key] = (
                            asyncio.get_running_loop().call_later(
                                on_time / 10,
                                cluster.update_attribute,
                                OnOff.AttributeDefs.on_off.id,
                                False,
                            )
                        )
            elif command_name == OnOff.ServerCommandDefs.toggle.name:
                cluster.update_attribute(
                    OnOff.AttributeDefs.on_off.id,
                    not bool(cluster.get(OnOff.AttributeDefs.on_off.name)),
                )

        if cluster.is_client and cluster.cluster_id == Ota.cluster_id:
            if command_name == Ota.ServerCommandDefs.query_next_image.name and args:
                cluster.update_attribute(
                    Ota.AttributeDefs.current_file_version.id,
                    args[3],
                )
            return

        if cluster.cluster_id == IasZone.cluster_id and cluster.is_server:
            if command_id == IasZone.ClientCommandDefs.status_change_notification.id:
                zone_status = args[0]
                cluster.update_attribute(
                    IasZone.AttributeDefs.zone_status.id, zone_status
                )
            elif command_id == IasZone.ClientCommandDefs.enroll.id:
                cluster.create_catching_task(
                    cluster.enroll_response(
                        enroll_response_code=IasZone.EnrollResponse.Success,
                        zone_id=0,
                    )
                )

        if (
            cluster.cluster_id == Identify.cluster_id
            and cluster.is_server
            and command_name == Identify.ServerCommandDefs.trigger_effect.name
        ):
            self.emit_cluster_zha_event(
                cluster,
                f"{self._cluster_unique_id(cluster)}_{command_name}",
                args[0] if args else [],
            )
            return

        if (
            cluster.cluster_id == DoorLock.cluster_id
            and cluster.is_server
            and command_name
            == DoorLock.ClientCommandDefs.operation_event_notification.name
        ):
            self.emit_cluster_zha_event(
                cluster,
                command_name,
                {
                    "source": args[0].name,
                    "operation": args[1].name,
                    "code_slot": args[2] + 1,
                },
            )
            return

        if (
            cluster.is_client
            and cluster.cluster_id in CLIENT_COMMAND_SUPPRESSED_CLUSTER_IDS
        ):
            return

        self.emit_cluster_zha_event(cluster, command_name, args or [])

    def emit_zha_event(self, event_data: dict[str, Any]) -> None:
        """Broadcast an event from this endpoint."""
        self.device.emit_zha_event(
            {
                const.ATTR_UNIQUE_ID: self.unique_id,
                const.ATTR_ENDPOINT_ID: self.id,
                **event_data,
            }
        )
