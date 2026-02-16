"""Device for Zigbee Home Automation."""

# pylint: disable=too-many-lines

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from collections.abc import Callable, Iterable
from contextlib import suppress
import dataclasses
from dataclasses import dataclass
from enum import Enum
from functools import cached_property
import logging
import time
from typing import TYPE_CHECKING, Any, Final, Self, cast

from zigpy.device import Device as ZigpyDevice
import zigpy.exceptions
from zigpy.profiles import PROFILES
import zigpy.quirks
from zigpy.quirks.v2 import DeviceAlertMetadata, QuirksV2RegistryEntry
from zigpy.types import uint1_t, uint8_t, uint16_t
from zigpy.types.named import EUI64, NWK, ExtendedPanId
from zigpy.typing import UNDEFINED, UndefinedType
from zigpy.zcl.clusters import Cluster
from zigpy.zcl.clusters.general import Groups, Identify
from zigpy.zcl.foundation import (
    Status as ZclStatus,
    WriteAttributesResponse,
    ZCLCommandDef,
)
import zigpy.zdo.types as zdo_types
from zigpy.zdo.types import (
    DeviceType,
    PermitJoins,
    Relationship,
    RouteStatus,
    RxOnWhenIdle,
)

from zha.application import Platform, discovery
from zha.application.const import (
    ATTR_ARGS,
    ATTR_ATTRIBUTE,
    ATTR_CLUSTER_ID,
    ATTR_CLUSTER_TYPE,
    ATTR_COMMAND,
    ATTR_COMMAND_TYPE,
    ATTR_ENDPOINT_ID,
    ATTR_ENDPOINTS,
    ATTR_MANUFACTURER,
    ATTR_MODEL,
    ATTR_NODE_DESCRIPTOR,
    ATTR_PARAMS,
    ATTR_QUIRK_ID,
    ATTR_VALUE,
    CLUSTER_COMMAND_SERVER,
    CLUSTER_COMMANDS_CLIENT,
    CLUSTER_COMMANDS_SERVER,
    CLUSTER_TYPE_IN,
    CLUSTER_TYPE_OUT,
    POWER_BATTERY_OR_UNKNOWN,
    POWER_MAINS_POWERED,
    UNKNOWN,
    UNKNOWN_MANUFACTURER,
    UNKNOWN_MODEL,
    ZHA_CLUSTER_CFG_DONE,
    ZHA_CLUSTER_MSG,
    ZHA_DEVICE_UPDATED_EVENT,
    ZHA_EVENT,
)
from zha.application.helpers import (
    convert_to_zcl_values,
    convert_zcl_value,
    safe_cluster_command,
    safe_read,
)
from zha.application.platforms import (
    BaseEntity,
    BaseEntityInfo,
    EntityStateChangedEvent,
    PlatformEntity,
)
from zha.application.platforms.update import BaseFirmwareUpdateEntity
from zha.const import STATE_CHANGED
from zha.event import EventBase
from zha.exceptions import ZHAException
from zha.mixins import LogMixin
from zha.zigbee.const import REPORT_CONFIG_ATTR, REPORT_CONFIG_CONFIG
from zha.zigbee.endpoint import Endpoint

if TYPE_CHECKING:
    from zha.application.gateway import Gateway

_LOGGER = logging.getLogger(__name__)
_CHECKIN_GRACE_PERIODS = 2
DIAGNOSTICS_JSON_VERSION = 2


def get_cluster_attr_data(cluster: Cluster) -> list[dict]:
    """Return cluster attribute data."""
    attributes_info = []

    for attr_def in cluster.attributes.values():
        info = {
            "id": f"0x{attr_def.id:04x}",
            "name": attr_def.name,
            "zcl_type": (
                attr_def.zcl_type.name if attr_def.zcl_type.name != "bool_" else "bool"
            ),
            "value": cluster.get(attr_def.name),
            "unsupported": cluster.is_attribute_unsupported(attr_def),
        }

        # Don't unnecessarily list out attributes that are just unread
        if info["value"] is None and not info["unsupported"]:
            continue

        # Delete unused keys
        if info["value"] is not None:
            del info["unsupported"]
        else:
            del info["value"]

        attributes_info.append(info)

    return attributes_info


def get_device_automation_triggers(
    device: zigpy.device.Device,
) -> dict[tuple[str, str], dict[str, str]]:
    """Get the supported device automation triggers for a zigpy device."""
    return {
        ("device_offline", "device_offline"): {"device_event_type": "device_offline"},
        **getattr(device, "device_automation_triggers", {}),
    }


@dataclass(frozen=True, kw_only=True)
class ClusterBinding:
    """Describes a cluster binding."""

    name: str
    type: str
    id: int
    endpoint_id: int


class DeviceStatus(Enum):
    """Status of a device."""

    CREATED = 1
    INITIALIZED = 2


class ZDOStatus(Enum):
    """Lifecycle status for the device-level ZDO listener."""

    CREATED = 1
    CONFIGURED = 2
    INITIALIZED = 3


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


@dataclass(kw_only=True, frozen=True)
class ZHAEvent:
    """Event generated when a device wishes to send an arbitrary event."""

    device_ieee: EUI64
    unique_id: str
    data: dict[str, Any]
    event_type: Final[str] = ZHA_EVENT
    event: Final[str] = ZHA_EVENT


@dataclass(kw_only=True, frozen=True)
class DeviceFirmwareInfoUpdatedEvent:
    """Event generated when the device firmware information has changed."""

    event_type: Final[str] = ZHA_DEVICE_UPDATED_EVENT
    event: Final[str] = ZHA_DEVICE_UPDATED_EVENT

    old_firmware_version: str | None
    new_firmware_version: str | None


@dataclass(kw_only=True, frozen=True)
class ClusterConfigurationComplete:
    """Event generated when all clusters are configured."""

    device_ieee: EUI64
    unique_id: str
    event_type: Final[str] = ZHA_CLUSTER_MSG
    event: Final[str] = ZHA_CLUSTER_CFG_DONE


@dataclass(kw_only=True, frozen=True)
class DeviceInfo:
    """Describes a device."""

    ieee: EUI64
    nwk: NWK
    manufacturer: str
    model: str
    name: str
    quirk_applied: bool
    quirk_class: str
    exposes_features: set[str]
    manufacturer_code: int | None
    power_source: str
    lqi: int
    rssi: int
    last_seen: str
    available: bool
    device_type: str
    signature: dict[str, Any]


@dataclass(kw_only=True, frozen=True)
class NeighborInfo:
    """Describes a neighbor."""

    device_type: DeviceType
    rx_on_when_idle: RxOnWhenIdle
    relationship: Relationship
    extended_pan_id: ExtendedPanId
    ieee: EUI64
    nwk: NWK
    permit_joining: PermitJoins
    depth: uint8_t
    lqi: uint8_t


@dataclass(kw_only=True, frozen=True)
class RouteInfo:
    """Describes a route."""

    dest_nwk: NWK
    route_status: RouteStatus
    memory_constrained: uint1_t
    many_to_one: uint1_t
    route_record_required: uint1_t
    next_hop: NWK


@dataclass(kw_only=True, frozen=True)
class EndpointNameInfo:
    """Describes an endpoint name."""

    name: str


@dataclass(kw_only=True, frozen=True)
class ExtendedDeviceInfo(DeviceInfo):
    """Describes a ZHA device."""

    active_coordinator: bool
    entities: dict[str, BaseEntityInfo]
    neighbors: list[NeighborInfo]
    routes: list[RouteInfo]
    endpoint_names: list[EndpointNameInfo]


class Device(LogMixin, EventBase):
    """ZHA Zigbee device object."""

    unique_id: str

    def __init__(
        self,
        zigpy_device: zigpy.device.Device,
        _gateway: Gateway,
    ) -> None:
        """Initialize the gateway."""
        super().__init__()

        self.unique_id = str(zigpy_device.ieee)

        self._gateway: Gateway = _gateway
        self._zigpy_device: ZigpyDevice = zigpy_device
        self.quirk_applied: bool = isinstance(
            self._zigpy_device, zigpy.quirks.BaseCustomDevice
        )
        self.quirk_class: str = (
            f"{self._zigpy_device.__class__.__module__}."
            f"{self._zigpy_device.__class__.__name__}"
        )

        # add v1 quirk exposed features (legacy quirk id)
        qid: set[str] | str = getattr(self._zigpy_device, ATTR_QUIRK_ID, set())
        self.exposes_features: set[str] = {qid} if isinstance(qid, str) else set(qid)

        # add v2 quirk exposed features
        if self.quirk_metadata is not None:
            self.exposes_features.update(
                f.feature for f in self.quirk_metadata.exposes_features
            )

        self._power_config_ch: Cluster | None = None
        self._identify_ch: Cluster | None = None
        self._basic_ch: Cluster | None = None
        self._firmware_version: str | None = None
        self._firmware_update_listener_remove: Callable[[], None] | None = None
        self._firmware_update_listener_entity_key: tuple[Platform, str] | None = None

        device_options = _gateway.config.config.device_options
        if self.is_mains_powered:
            self.consider_unavailable_time: int = (
                device_options.consider_unavailable_mains
            )
        else:
            self.consider_unavailable_time = device_options.consider_unavailable_battery
        self._available: bool = self.is_active_coordinator or (
            self.last_seen is not None
            and time.time() - self.last_seen < self.consider_unavailable_time
        )
        self._checkins_missed_count: int = 0
        self._on_network: bool = True

        self._platform_entities: dict[tuple[Platform, str], BaseEntity] = {}
        self._pending_entities: OrderedDict[tuple[Platform, str], BaseEntity] = (
            OrderedDict()
        )
        self.semaphore: asyncio.Semaphore = asyncio.Semaphore(3)

        self._on_remove_callbacks: list[Callable[[], None]] = []

        self._zdo_cluster = self._zigpy_device.endpoints[0]
        self._zdo_status = ZDOStatus.CREATED
        self._zdo_unique_id = f"{str(self.ieee)}:{self.name}_ZDO"
        self._zdo_cluster.add_listener(self)
        self._on_remove_callbacks.append(
            lambda: self._zdo_cluster.remove_listener(self)
        )

        self.status: DeviceStatus = DeviceStatus.CREATED

        self._endpoints: dict[int, Endpoint] = {}
        for ep_id, endpoint in zigpy_device.endpoints.items():
            if ep_id != 0:
                ep = Endpoint.new(endpoint, self)
                self._endpoints[ep_id] = ep
                self._on_remove_callbacks.append(ep.on_remove)

    def __repr__(self) -> str:
        """Return a string representation of the device."""
        return (
            f"{repr(self._zigpy_device)} - "
            f"quirk_applied: {self.quirk_applied} - "
            f"quirk_or_device_class: {self.quirk_class} - "
            f"exposes_features: {self.exposes_features}"
        )

    @property
    def device(self) -> zigpy.device.Device:
        """Return underlying Zigpy device."""
        return self._zigpy_device

    @cached_property
    def name(self) -> str:
        """Return device name."""
        # Nabu Casa devices include a brand name in the model
        if self.manufacturer == "Nabu Casa":
            return self.model
        return f"{self.manufacturer} {self.model}"

    @property
    def ieee(self) -> EUI64:
        """Return ieee address for device."""
        return self._zigpy_device.ieee

    @property
    def quirk_metadata(self) -> QuirksV2RegistryEntry | None:
        """Return the quirk metadata for this device."""
        return getattr(self._zigpy_device, "quirk_metadata", None)

    @cached_property
    def manufacturer(self) -> str:
        """Return manufacturer for device."""
        if self.is_active_coordinator:
            manufacturer = (
                self.gateway.application_controller.state.node_info.manufacturer
            )
            if manufacturer is None:
                return ""
            return manufacturer

        if (
            self.quirk_metadata is not None
            and self.quirk_metadata.friendly_name is not None
        ):
            return self.quirk_metadata.friendly_name.manufacturer

        if self._zigpy_device.manufacturer is None:
            return UNKNOWN_MANUFACTURER

        return self._zigpy_device.manufacturer

    @cached_property
    def model(self) -> str:
        """Return model for device."""
        if self.is_active_coordinator:
            model = self.gateway.application_controller.state.node_info.model
            if model is None:
                return f"Generic Zigbee Coordinator ({self.gateway.radio_type.pretty_name})"
            return model

        if (
            self.quirk_metadata is not None
            and self.quirk_metadata.friendly_name is not None
        ):
            return self.quirk_metadata.friendly_name.model

        if self._zigpy_device.model is None:
            return UNKNOWN_MODEL

        return self._zigpy_device.model

    @cached_property
    def device_alerts(self) -> Iterable[DeviceAlertMetadata]:
        """Return device alerts for this device."""
        if self.quirk_metadata is None:
            return []

        return self.quirk_metadata.device_alerts

    @cached_property
    def manufacturer_code(self) -> int | None:
        """Return the manufacturer code for the device."""
        if self._zigpy_device.node_desc is None:
            return None

        return self._zigpy_device.node_desc.manufacturer_code

    @property
    def nwk(self) -> NWK:
        """Return nwk for device."""
        return self._zigpy_device.nwk

    @property
    def lqi(self):
        """Return lqi for device."""
        return self._zigpy_device.lqi

    @property
    def rssi(self):
        """Return rssi for device."""
        return self._zigpy_device.rssi

    @property
    def last_seen(self) -> float | None:
        """Return last_seen for device."""
        return self._zigpy_device.last_seen

    @cached_property
    def is_mains_powered(self) -> bool | None:
        """Return true if device is mains powered."""
        if self._zigpy_device.node_desc is None:
            return None

        return self._zigpy_device.node_desc.is_mains_powered

    @cached_property
    def device_type(self) -> str:
        """Return the logical device type for the device."""
        if self._zigpy_device.node_desc is None:
            return UNKNOWN

        return self._zigpy_device.node_desc.logical_type.name

    @property
    def power_source(self) -> str:
        """Return the power source for the device."""
        return (
            POWER_MAINS_POWERED if self.is_mains_powered else POWER_BATTERY_OR_UNKNOWN
        )

    @cached_property
    def is_router(self) -> bool | None:
        """Return true if this is a routing capable device."""
        if self._zigpy_device.node_desc is None:
            return None

        return self._zigpy_device.node_desc.is_router

    @cached_property
    def is_coordinator(self) -> bool | None:
        """Return true if this device represents a coordinator."""
        if self._zigpy_device.node_desc is None:
            return None

        return self._zigpy_device.node_desc.is_coordinator

    @property
    def is_active_coordinator(self) -> bool:
        """Return true if this device is the active coordinator."""
        if not self.is_coordinator:
            return False

        return self.ieee == self.gateway.state.node_info.ieee

    @cached_property
    def is_end_device(self) -> bool | None:
        """Return true if this device is an end device."""
        if self._zigpy_device.node_desc is None:
            return None

        return self._zigpy_device.node_desc.is_end_device

    @property
    def is_groupable(self) -> bool:
        """Return true if this device has a group cluster."""
        return self.is_active_coordinator or (
            self.available and bool(self.async_get_groupable_endpoints())
        )

    @cached_property
    def skip_configuration(self) -> bool:
        """Return true if the device should not issue configuration related commands."""
        return self._zigpy_device.skip_configuration or bool(self.is_active_coordinator)

    @property
    def gateway(self):
        """Return the gateway for this device."""
        return self._gateway

    @cached_property
    def device_automation_commands(self) -> dict[str, list[tuple[str, str]]]:
        """Return the a lookup of commands to etype/sub_type."""
        commands: dict[str, list[tuple[str, str]]] = {}
        for etype_subtype, trigger in self.device_automation_triggers.items():
            if command := trigger.get(ATTR_COMMAND):
                commands.setdefault(command, []).append(etype_subtype)
        return commands

    @cached_property
    def device_automation_triggers(self) -> dict[tuple[str, str], dict[str, str]]:
        """Return the device automation triggers for this device."""
        return get_device_automation_triggers(self._zigpy_device)

    @property
    def available(self):
        """Return True if device is available."""
        return self.is_active_coordinator or (self._available and self.on_network)

    @available.setter
    def available(self, new_availability: bool) -> None:
        """Set device availability."""
        self._available = new_availability

    @property
    def on_network(self):
        """Return True if device is currently on the network."""
        return self.is_active_coordinator or self._on_network

    @on_network.setter
    def on_network(self, new_on_network: bool) -> None:
        """Set device on_network flag."""
        self.update_available(new_on_network)
        self._on_network = new_on_network
        if not new_on_network:
            self.debug("Device is not on the network, marking unavailable")

    @property
    def power_configuration_ch(self) -> Cluster | None:
        """Return power configuration cluster."""
        return self._power_config_ch

    @power_configuration_ch.setter
    def power_configuration_ch(self, cluster: Cluster) -> None:
        """Power configuration cluster setter."""
        if self._power_config_ch is None:
            self._power_config_ch = cluster

    @property
    def basic_ch(self) -> Cluster | None:
        """Return basic cluster."""
        return self._basic_ch

    @basic_ch.setter
    def basic_ch(self, cluster: Cluster) -> None:
        """Set the basic cluster."""
        if self._basic_ch is None:
            self._basic_ch = cluster

    @property
    def identify_ch(self) -> Cluster | None:
        """Return identify cluster."""
        return self._identify_ch

    @identify_ch.setter
    def identify_ch(self, cluster: Cluster) -> None:
        """Identify cluster setter."""
        if self._identify_ch is None:
            self._identify_ch = cluster

    @property
    def zdo_cluster(self):
        """Return the zigpy ZDO cluster."""
        return self._zdo_cluster

    @property
    def zdo_status(self) -> ZDOStatus:
        """Return ZDO listener lifecycle status."""
        return self._zdo_status

    @property
    def zdo_unique_id(self) -> str:
        """Return ZDO unique id used by legacy diagnostics/tests."""
        return self._zdo_unique_id

    @property
    def endpoints(self) -> dict[int, Endpoint]:
        """Return the endpoints for this device."""
        return self._endpoints

    @cached_property
    def zigbee_signature(self) -> dict[str, Any]:
        """Get zigbee signature for this device."""
        return {
            ATTR_NODE_DESCRIPTOR: self._zigpy_device.node_desc,
            ATTR_ENDPOINTS: {
                signature[0]: signature[1]
                for signature in [
                    endpoint.zigbee_signature for endpoint in self._endpoints.values()
                ]
            },
            ATTR_MANUFACTURER: self.manufacturer,
            ATTR_MODEL: self.model,
        }

    @property
    def firmware_version(self) -> str | None:
        """Return the software version for this device."""
        return self._firmware_version

    @property
    def platform_entities(self) -> dict[tuple[Platform, str], BaseEntity]:
        """Return the platform entities for this device."""
        return self._platform_entities

    def get_platform_entity(self, platform: Platform, unique_id: str) -> BaseEntity:
        """Get a platform entity by unique id."""
        entity = self._platform_entities.get((platform, unique_id))
        if entity is None:
            raise KeyError(f"Entity {unique_id} not found")
        return entity

    @classmethod
    def new(
        cls,
        zigpy_dev: zigpy.device.Device,
        gateway: Gateway,
    ) -> Self:
        """Create new device."""
        return cls(zigpy_dev, gateway)

    def async_update_firmware_version(self, firmware_version: str) -> None:
        """Update device firmware version."""
        if firmware_version == self._firmware_version:
            return

        old_firmware_version = self._firmware_version
        self._firmware_version = firmware_version

        self.emit(
            DeviceFirmwareInfoUpdatedEvent.event_type,
            DeviceFirmwareInfoUpdatedEvent(
                old_firmware_version=old_firmware_version,
                new_firmware_version=firmware_version,
            ),
        )

    async def _check_available(self, *_: Any) -> None:
        # don't flip the availability state of the coordinator
        if self.is_active_coordinator:
            return
        if self.last_seen is None:
            self.debug("last_seen is None, marking the device unavailable")
            self.update_available(False)
            return

        difference = time.time() - self.last_seen
        if difference < self.consider_unavailable_time:
            self.debug(
                "Device seen - marking the device available and resetting counter"
            )
            self.update_available(True)
            self._checkins_missed_count = 0
            return

        if self._gateway.config.allow_polling:
            if (
                self._checkins_missed_count >= _CHECKIN_GRACE_PERIODS
                or self.manufacturer == "LUMI"
                or not self._endpoints
            ):
                self.debug(
                    (
                        "last_seen is %s seconds ago and ping attempts have been exhausted,"
                        " marking the device unavailable"
                    ),
                    difference,
                )
                self.update_available(False)
                return

            self._checkins_missed_count += 1
            self.debug(
                "Attempting to checkin with device - missed checkins: %s",
                self._checkins_missed_count,
            )
            if not self.basic_ch:
                self.debug("does not have a mandatory basic cluster")
                self.update_available(False)
                return
            res = (
                await safe_read(
                    self.basic_ch,
                    [ATTR_MANUFACTURER],
                    allow_cache=False,
                    only_cache=False,
                )
            ).get(ATTR_MANUFACTURER)
            if res is not None:
                self._checkins_missed_count = 0

    def update_available(self, available: bool) -> None:
        """Update device availability and signal entities."""
        self.debug(
            (
                "Update device availability -  device available: %s - new availability:"
                " %s - changed: %s"
            ),
            self.available,
            available,
            self.available ^ available,
        )
        availability_changed = self.available ^ available
        self.available = available
        if availability_changed and available:
            # Reinitialize clusters then signal entities.
            self.debug(
                "Device availability changed and device became available,"
                " reinitializing clusters"
            )
            self._gateway.async_create_task(
                self._async_became_available(),
                name=f"({self.nwk},{self.model})_async_became_available",
                eager_start=True,
            )
            return
        if availability_changed and not available:
            self.debug("Device availability changed and device became unavailable")
            for entity in self.platform_entities.values():
                entity.maybe_emit_state_changed_event()
            self.emit_zha_event(
                {
                    "device_event_type": "device_offline",
                },
            )

    def emit_zha_event(self, event_data: dict[str, str | int]) -> None:  # pylint: disable=unused-argument
        """Relay events directly."""
        self.emit(
            ZHA_EVENT,
            ZHAEvent(
                device_ieee=self.ieee,
                unique_id=str(self.ieee),
                data=event_data,
            ),
        )

    async def _async_became_available(self) -> None:
        """Update device availability and signal entities."""
        await self.async_initialize(False)
        for platform_entity in self._platform_entities.values():
            platform_entity.maybe_emit_state_changed_event()

    @property
    def device_info(self) -> DeviceInfo:
        """Return a device description for device."""
        ieee = self.ieee
        time_struct = time.localtime(self.last_seen)
        update_time = time.strftime("%Y-%m-%dT%H:%M:%S", time_struct)
        return DeviceInfo(
            ieee=ieee,
            nwk=self.nwk,
            manufacturer=self.manufacturer,
            model=self.model,
            name=self.name,
            quirk_applied=self.quirk_applied,
            quirk_class=self.quirk_class,
            exposes_features=self.exposes_features,
            manufacturer_code=self.manufacturer_code,
            power_source=self.power_source,
            lqi=self.lqi,
            rssi=self.rssi,
            last_seen=update_time,
            available=self.available,
            device_type=self.device_type,
            signature=self.zigbee_signature,
        )

    @property
    def extended_device_info(self) -> ExtendedDeviceInfo:
        """Get extended device information."""
        topology = self.gateway.application_controller.topology
        names: list[EndpointNameInfo] = []
        for endpoint in (ep for epid, ep in self.device.endpoints.items() if epid):
            profile = PROFILES.get(endpoint.profile_id)
            if profile and endpoint.device_type is not None:
                # DeviceType provides undefined enums
                names.append(
                    EndpointNameInfo(name=profile.DeviceType(endpoint.device_type).name)
                )
            else:
                names.append(
                    EndpointNameInfo(
                        name=(
                            f"unknown {endpoint.device_type} device_type "
                            f"of 0x{(endpoint.profile_id or 0xFFFF):04x} profile id"
                        )
                    )
                )

        return ExtendedDeviceInfo(
            **self.device_info.__dict__,
            active_coordinator=self.is_active_coordinator,
            entities={
                platform_entity.unique_id: platform_entity.info_object
                for platform_entity in self.platform_entities.values()
            },
            neighbors=[
                NeighborInfo(
                    device_type=neighbor.device_type.name,
                    rx_on_when_idle=neighbor.rx_on_when_idle.name,
                    relationship=neighbor.relationship.name,
                    extended_pan_id=neighbor.extended_pan_id,
                    ieee=neighbor.ieee,
                    nwk=neighbor.nwk,
                    permit_joining=neighbor.permit_joining.name,
                    depth=neighbor.depth,
                    lqi=neighbor.lqi,
                )
                for neighbor in topology.neighbors[self.ieee]
            ],
            routes=[
                RouteInfo(
                    dest_nwk=route.DstNWK,
                    route_status=route.RouteStatus.name,
                    memory_constrained=route.MemoryConstrained,
                    many_to_one=route.ManyToOne,
                    route_record_required=route.RouteRecordRequired,
                    next_hop=route.NextHop,
                )
                for route in topology.routes[self.ieee]
            ],
            endpoint_names=names,
        )

    async def async_configure(self) -> None:
        """Configure the device."""
        self.debug("started configuration")
        self._zdo_status = ZDOStatus.CONFIGURED
        self.debug("'async_configure' stage succeeded for ZDO")

        if isinstance(self._zigpy_device, zigpy.quirks.BaseCustomDevice):
            self.debug("applying quirks custom device configuration")
            await self._zigpy_device.apply_custom_configuration()

        # Try to add entities to claim clusters.
        self._discover_new_entities()
        self._sync_pending_entity_cluster_requirements()

        await asyncio.gather(
            *(endpoint.async_configure() for endpoint in self._endpoints.values())
        )

        self.emit(
            ZHA_CLUSTER_CFG_DONE,
            ClusterConfigurationComplete(
                device_ieee=self.ieee,
                unique_id=self.ieee,
            ),
        )

        self.debug("completed configuration")

        if (
            self.gateway.config.config.device_options.enable_identify_on_join
            and self.identify_ch is not None
            and not self.skip_configuration
        ):
            self._gateway.async_create_task(
                safe_cluster_command(
                    self.identify_ch,
                    "trigger_effect",
                    effect_id=Identify.EffectIdentifier.Okay,
                    effect_variant=Identify.EffectVariant.Default,
                ),
                name=f"({self.nwk},{self.model}) trigger_effect identify",
                eager_start=True,
            )

    def _is_entity_removed_by_quirk(self, entity: PlatformEntity) -> bool:
        if self.quirk_metadata is None:
            return False

        for meta in self.quirk_metadata.disabled_default_entities:
            _LOGGER.debug("Checking if entity %s is removed by %s", entity, meta)

            if meta.unique_id_suffix is not None and not entity.unique_id.endswith(
                meta.unique_id_suffix
            ):
                continue

            if meta.endpoint_id is not None and entity.endpoint.id != meta.endpoint_id:
                continue

            if meta.cluster_id is not None and not any(
                cluster.cluster_id == meta.cluster_id
                for cluster in entity.clusters.values()
            ):
                continue

            if meta.function is not None and not meta.function(entity):
                continue

            return True

        return False

    def _apply_entity_metadata_changes(self, entity: PlatformEntity) -> None:
        """Apply entity metadata changes from quirks v2."""
        if self.quirk_metadata is None:
            return

        for meta in self.quirk_metadata.changed_entity_metadata:
            if meta.unique_id_suffix is not None and not entity.unique_id.endswith(
                meta.unique_id_suffix
            ):
                continue

            if meta.endpoint_id is not None and entity.endpoint.id != meta.endpoint_id:
                continue

            if meta.cluster_id is not None and not any(
                cluster.cluster_id == meta.cluster_id
                and cluster.cluster_type == meta.cluster_type
                for cluster in entity.clusters.values()
            ):
                continue

            if meta.function is not None and not meta.function(entity):
                continue

            # Apply metadata changes
            _LOGGER.debug(
                "Applying metadata changes from %s to entity %s", meta, entity
            )

            if meta.new_primary is not None:
                entity._attr_primary = meta.new_primary

            if meta.new_unique_id is not None:
                entity._unique_id = meta.new_unique_id

            if meta.new_translation_key is not None:
                entity._attr_translation_key = meta.new_translation_key

            if meta.new_translation_placeholders is not None:
                entity._attr_translation_placeholders = (
                    meta.new_translation_placeholders
                )

            if meta.new_device_class is not None:
                entity._attr_device_class = meta.new_device_class

            if meta.new_state_class is not None:
                entity._attr_state_class = meta.new_state_class

            if meta.new_entity_category is not None:
                entity._attr_entity_category = meta.new_entity_category

            if meta.new_entity_registry_enabled_default is not None:
                entity._attr_entity_registry_enabled_default = (
                    meta.new_entity_registry_enabled_default
                )

            if meta.new_fallback_name is not None:
                entity._attr_fallback_name = meta.new_fallback_name

    def _discover_new_entities(self) -> None:
        new_entities: Iterable[BaseEntity]

        if self.is_active_coordinator:
            new_entities = discovery.discover_coordinator_device_entities(self)
        elif self.is_coordinator:
            # TODO: purge old coordinator entities
            new_entities = []
        else:
            new_entities = discovery.discover_device_entities(self)

        # Discover all applicable entities
        for entity in new_entities:
            if self._is_entity_removed_by_quirk(entity):
                continue

            # Apply any metadata changes from quirks v2
            self._apply_entity_metadata_changes(entity)

            self._enqueue_pending_entity(entity)

    @staticmethod
    def _entity_queue_key(entity: BaseEntity) -> tuple[Platform, str]:
        """Return the canonical key used for queueing and registration."""
        return (entity.PLATFORM, entity.unique_id)

    @staticmethod
    def _pending_queue_key(entity: BaseEntity) -> tuple[Platform, str]:
        """Return a pending-queue key that preserves same-id class variants.

        Some discovery paths emit multiple candidate classes with the same
        `(platform, unique_id)` and rely on finalization support checks to pick
        the winner. Include class identity in the pending key so repeated
        discovery passes dedupe by candidate class while keeping these variants.
        """
        return (
            entity.PLATFORM,
            f"{entity.unique_id}::{entity.__class__.__module__}.{entity.__class__.__qualname__}",
        )

    def _enqueue_pending_entity(self, entity: BaseEntity) -> None:
        """Enqueue a newly discovered entity if not already registered/pending."""
        if self._entity_queue_key(entity) in self._platform_entities:
            return

        pending_key = self._pending_queue_key(entity)
        if pending_key in self._pending_entities:
            return

        entity.on_add()
        self._pending_entities[pending_key] = entity

    def _sync_pending_entity_cluster_requirements(self) -> None:
        """Apply pending entity report/init requirements to endpoint clusters."""
        if not self._pending_entities:
            return

        pending_entities: dict[tuple[Platform, str], PlatformEntity] = {
            entity_key: entity
            for entity_key, entity in self._pending_entities.items()
            if isinstance(entity, PlatformEntity)
        }

        attr_is_known_cache: dict[tuple[int, str], bool] = {}

        pending_report_config, pending_direct_report_attrs = (
            self._collect_pending_report_config(
                pending_entities=pending_entities,
                attr_is_known_cache=attr_is_known_cache,
            )
        )
        pending_init_attrs = self._collect_pending_init_attrs(
            pending_entities=pending_entities,
            attr_is_known_cache=attr_is_known_cache,
        )
        self._apply_pending_report_config(
            pending_report_config=pending_report_config,
            pending_direct_report_attrs=pending_direct_report_attrs,
        )
        self._apply_pending_init_attrs(pending_init_attrs=pending_init_attrs)

    @staticmethod
    def _cluster_has_attribute(
        cluster: Cluster,
        attr: str,
        attr_is_known_cache: dict[tuple[int, str], bool],
    ) -> bool:
        """Return whether this cluster can resolve an attribute name."""
        cache_key = (id(cluster), attr)
        if cache_key in attr_is_known_cache:
            return attr_is_known_cache[cache_key]

        try:
            cluster.find_attribute(attr)
            attr_is_known_cache[cache_key] = True
        except KeyError:
            attr_is_known_cache[cache_key] = False

        return attr_is_known_cache[cache_key]

    def _collect_pending_report_config(
        self,
        *,
        pending_entities: dict[tuple[Platform, str], PlatformEntity],
        attr_is_known_cache: dict[tuple[int, str], bool],
    ) -> tuple[
        dict[Cluster, dict[str, tuple[int, int, int | float]]],
        dict[Cluster, set[str]],
    ]:
        """Collect and merge report config requirements from pending entities."""
        pending_report_config: dict[
            Cluster, dict[str, tuple[int, int, int | float]]
        ] = defaultdict(dict)
        pending_direct_report_attrs: dict[Cluster, set[str]] = defaultdict(set)

        for entity in pending_entities.values():
            for cluster_name, report_entries in entity.entity_report_config.items():
                cluster = entity.clusters.get(cluster_name)
                if cluster is None:
                    continue

                direct_report_attrs = entity.quirks_v2_direct_report_attrs.get(
                    cluster_name, set()
                )
                cluster_report_map = pending_report_config[cluster]
                cluster_direct_attrs = pending_direct_report_attrs[cluster]

                for entry in report_entries:
                    attr = cast(str, entry[REPORT_CONFIG_ATTR])
                    if (
                        attr not in direct_report_attrs
                        and not self._cluster_has_attribute(
                            cluster, attr, attr_is_known_cache
                        )
                    ):
                        continue

                    config = cast(
                        tuple[int, int, int | float], entry[REPORT_CONFIG_CONFIG]
                    )

                    if attr in direct_report_attrs:
                        cluster_report_map[attr] = config
                        cluster_direct_attrs.add(attr)
                        continue

                    if attr in cluster_direct_attrs:
                        continue

                    if attr in cluster_report_map:
                        cluster_report_map[attr] = _merge_aggressive_reporting_config(
                            cluster_report_map[attr], config
                        )
                    else:
                        cluster_report_map[attr] = config

        return pending_report_config, pending_direct_report_attrs

    def _collect_pending_init_attrs(
        self,
        *,
        pending_entities: dict[tuple[Platform, str], PlatformEntity],
        attr_is_known_cache: dict[tuple[int, str], bool],
    ) -> dict[Cluster, dict[str, bool]]:
        """Collect and merge init-attr requirements from pending entities."""
        pending_init_attrs: dict[Cluster, dict[str, bool]] = defaultdict(dict)

        for entity in pending_entities.values():
            for cluster_name, init_attrs in entity.entity_init_attrs.items():
                cluster = entity.clusters.get(cluster_name)
                if cluster is None:
                    continue

                direct_init_attrs = entity.quirks_v2_direct_init_attrs.get(
                    cluster_name, set()
                )
                cluster_init_attrs = pending_init_attrs[cluster]
                for attr, use_cache in init_attrs.items():
                    if (
                        attr not in direct_init_attrs
                        and not self._cluster_has_attribute(
                            cluster, attr, attr_is_known_cache
                        )
                    ):
                        continue

                    if attr in cluster_init_attrs:
                        cluster_init_attrs[attr] = (
                            cluster_init_attrs[attr] and use_cache
                        )
                    else:
                        cluster_init_attrs[attr] = use_cache

        return pending_init_attrs

    def _apply_pending_report_config(
        self,
        *,
        pending_report_config: dict[Cluster, dict[str, tuple[int, int, int | float]]],
        pending_direct_report_attrs: dict[Cluster, set[str]],
    ) -> None:
        """Apply merged report config requirements to endpoint clusters."""
        for cluster, report_map in pending_report_config.items():
            endpoint = self._endpoints.get(cluster.endpoint.endpoint_id)
            if endpoint is None:
                continue
            endpoint.set_cluster_report_config(
                cluster=cluster,
                report_config=report_map,
                direct_attrs=pending_direct_report_attrs[cluster],
            )

    def _apply_pending_init_attrs(
        self, *, pending_init_attrs: dict[Cluster, dict[str, bool]]
    ) -> None:
        """Apply merged init-attr requirements to endpoint clusters."""
        for cluster, init_attrs in pending_init_attrs.items():
            endpoint = self._endpoints.get(cluster.endpoint.endpoint_id)
            if endpoint is None:
                continue
            endpoint.set_cluster_init_attrs(cluster=cluster, init_attrs=init_attrs)

    async def async_initialize(self, from_cache: bool = False) -> None:
        """Initialize clusters."""
        self.debug("started initialization")

        self._discover_new_entities()
        self._sync_pending_entity_cluster_requirements()

        self._zdo_status = ZDOStatus.INITIALIZED
        self.debug("'async_initialize' stage succeeded for ZDO")

        # We intentionally do not use `gather` here! This is so that if, for example,
        # three `device.async_initialize()`s are spawned, only three concurrent requests
        # will ever be in flight at once. Startup concurrency is managed at the device
        # level.
        for endpoint in self._endpoints.values():
            try:
                await endpoint.async_initialize(from_cache)
            except Exception:  # pylint: disable=broad-exception-caught
                self.debug("Failed to initialize endpoint", exc_info=True)

        # Drain the pending queue for this pass. Any newly discovered entities from
        # concurrent work will remain in `_pending_entities` for the next pass.
        pending_entities, self._pending_entities = (
            self._pending_entities,
            OrderedDict(),
        )
        pending_entity_items = list(pending_entities.items())

        # Compute the final entities for this pass
        new_entities: dict[tuple[Platform, str], BaseEntity] = {}
        supported_entities: list[BaseEntity] = list(self._platform_entities.values())
        processed_count = 0
        try:
            for _, entity in pending_entity_items:
                entity.recompute_capabilities()

                # Ignore unsupported entities
                if not entity.is_supported() or not entity.is_supported_in_list(
                    supported_entities
                ):
                    await entity.on_remove()
                else:
                    key = self._entity_queue_key(entity)

                    # Keep existing registered entities as canonical. Re-discovered
                    # candidates for the same key are cleaned up and dropped.
                    if key in self._platform_entities or key in new_entities:
                        await entity.on_remove()
                    else:
                        new_entities[key] = entity
                        supported_entities.append(entity)
                processed_count += 1
        except Exception:
            # Requeue this entity and any unprocessed entities so they are not lost.
            requeued_entities: OrderedDict[tuple[Platform, str], BaseEntity] = (
                OrderedDict(pending_entity_items[processed_count:])
            )
            for key, entity in self._pending_entities.items():
                if key in requeued_entities:
                    dropped_entity = requeued_entities[key]
                    try:
                        await dropped_entity.on_remove()
                    except Exception:
                        _LOGGER.warning(
                            "Failed to remove dropped pending entity %s for device %s",
                            dropped_entity,
                            self,
                            exc_info=True,
                        )
                requeued_entities[key] = entity
            self._pending_entities = requeued_entities
            raise

        if new_entities:
            _LOGGER.debug("Discovered new entities %r", new_entities)
            self._platform_entities.update(new_entities)

        # At this point we can compute a primary entity
        self._compute_primary_entity()

        # Sync firmware state with the update entity and ensure listener registration
        # is idempotent across repeated initialize calls.
        self._sync_firmware_update_listener()

        self.debug("power source: %s", self.power_source)
        self.status = DeviceStatus.INITIALIZED
        self.debug("completed initialization")

    def _remove_firmware_update_listener(self) -> None:
        """Remove the firmware update listener if one is currently registered."""
        callback = self._firmware_update_listener_remove
        if callback is None:
            return

        with suppress(ValueError):
            self._on_remove_callbacks.remove(callback)

        try:
            callback()
        except Exception:  # pylint: disable=broad-exception-caught
            _LOGGER.warning(
                "Failed to remove firmware listener callback for device %s",
                self,
                exc_info=True,
            )
        finally:
            self._firmware_update_listener_remove = None
            self._firmware_update_listener_entity_key = None

    def _sync_firmware_update_listener(self) -> None:
        """Sync firmware version and state listener with the update entity."""
        update_key: tuple[Platform, str] | None = None
        update_entity: BaseFirmwareUpdateEntity | None = None

        for key, entity in self.platform_entities.items():
            if key[0] != Platform.UPDATE:
                continue
            assert isinstance(entity, BaseFirmwareUpdateEntity)
            update_key = key
            update_entity = entity
            break

        if update_entity is None or update_key is None:
            self._remove_firmware_update_listener()
            return

        self._firmware_version = update_entity.installed_version

        if (
            self._firmware_update_listener_entity_key == update_key
            and self._firmware_update_listener_remove is not None
        ):
            return

        self._remove_firmware_update_listener()

        def entity_update_listener(event: EntityStateChangedEvent) -> None:
            """Listen to firmware update entity changes."""
            entity = self.get_platform_entity(event.platform, event.unique_id)
            assert isinstance(entity, BaseFirmwareUpdateEntity)
            self.async_update_firmware_version(entity.installed_version)

        callback = update_entity.on_event(STATE_CHANGED, entity_update_listener)
        self._on_remove_callbacks.append(callback)
        self._firmware_update_listener_remove = callback
        self._firmware_update_listener_entity_key = update_key

    async def on_remove(self) -> None:
        """Cancel tasks this device owns."""
        for callback in self._on_remove_callbacks:
            try:
                callback()
            except Exception:
                _LOGGER.warning(
                    "Failed to execute on_remove callback %s for device %s",
                    callback,
                    self,
                    exc_info=True,
                )

        for platform_entity in self._platform_entities.values():
            try:
                await platform_entity.on_remove()
            except Exception:
                _LOGGER.warning(
                    "Failed to remove platform entity %s for device %s",
                    platform_entity,
                    self,
                    exc_info=True,
                )

        for entity in self._pending_entities.values():
            try:
                await entity.on_remove()
            except Exception:
                _LOGGER.warning(
                    "Failed to remove pending entity %s for device %s",
                    entity,
                    self,
                    exc_info=True,
                )

    def async_get_clusters(self) -> dict[int, dict[str, dict[int, Cluster]]]:
        """Get all clusters for this device."""
        return {
            ep_id: {
                CLUSTER_TYPE_IN: endpoint.in_clusters,
                CLUSTER_TYPE_OUT: endpoint.out_clusters,
            }
            for (ep_id, endpoint) in self._zigpy_device.endpoints.items()
            if ep_id != 0
        }

    def async_get_groupable_endpoints(self):
        """Get device endpoints that have a group 'in' cluster."""
        return [
            ep_id
            for (ep_id, clusters) in self.async_get_clusters().items()
            if Groups.cluster_id in clusters[CLUSTER_TYPE_IN]
        ]

    def async_get_std_clusters(self):
        """Get ZHA and ZLL clusters for this device."""

        return {
            ep_id: {
                CLUSTER_TYPE_IN: endpoint.in_clusters,
                CLUSTER_TYPE_OUT: endpoint.out_clusters,
            }
            for (ep_id, endpoint) in self._zigpy_device.endpoints.items()
            if ep_id != 0 and endpoint.profile_id in PROFILES
        }

    def async_get_cluster(
        self, endpoint_id: int, cluster_id: int, cluster_type: str = CLUSTER_TYPE_IN
    ) -> Cluster:
        """Get zigbee cluster from this entity."""
        clusters: dict[int, dict[str, dict[int, Cluster]]] = self.async_get_clusters()
        return clusters[endpoint_id][cluster_type][cluster_id]

    def async_get_cluster_attributes(
        self, endpoint_id, cluster_id, cluster_type=CLUSTER_TYPE_IN
    ):
        """Get zigbee attributes for specified cluster."""
        return self.async_get_cluster(endpoint_id, cluster_id, cluster_type).attributes

    def async_get_cluster_commands(
        self, endpoint_id, cluster_id, cluster_type=CLUSTER_TYPE_IN
    ):
        """Get zigbee commands for specified cluster."""
        cluster = self.async_get_cluster(endpoint_id, cluster_id, cluster_type)
        return {
            CLUSTER_COMMANDS_CLIENT: cluster.client_commands,
            CLUSTER_COMMANDS_SERVER: cluster.server_commands,
        }

    async def write_zigbee_attribute(
        self,
        endpoint_id: int,
        cluster_id: int,
        attribute: int | str,
        value: Any,
        cluster_type: str = CLUSTER_TYPE_IN,
        manufacturer: int | UndefinedType | None = UNDEFINED,
    ) -> WriteAttributesResponse | None:
        """Write a value to a zigbee attribute for a cluster in this entity."""
        try:
            cluster: Cluster = self.async_get_cluster(
                endpoint_id, cluster_id, cluster_type
            )
        except KeyError as exc:
            raise ValueError(
                f"Cluster {cluster_id} not found on endpoint {endpoint_id} while"
                f" writing attribute {attribute} with value {value}"
            ) from exc

        attr_def = cluster.find_attribute(attribute)
        value = convert_zcl_value(value, attr_def.type)

        try:
            response = await cluster.write_attributes(
                {attribute: value}, manufacturer=manufacturer
            )
            self.debug(
                "set: %s for attr: %s to cluster: %s for ept: %s - res: %s",
                value,
                attribute,
                cluster_id,
                endpoint_id,
                response,
            )
            return response
        except zigpy.exceptions.ZigbeeException as exc:
            raise ZHAException(
                f"Failed to set attribute: "
                f"{ATTR_VALUE}: {value} "
                f"{ATTR_ATTRIBUTE}: {attribute} "
                f"{ATTR_CLUSTER_ID}: {cluster_id} "
                f"{ATTR_ENDPOINT_ID}: {endpoint_id}"
            ) from exc

    async def issue_cluster_command(
        self,
        endpoint_id: int,
        cluster_id: int,
        command: int,
        command_type: str,
        args: list | None,
        params: dict[str, Any] | None,
        cluster_type: str = CLUSTER_TYPE_IN,
        manufacturer: int | None = None,
    ) -> None:
        """Issue a command against specified zigbee cluster on this device."""
        try:
            cluster: Cluster = self.async_get_cluster(
                endpoint_id, cluster_id, cluster_type
            )
        except KeyError as exc:
            raise ValueError(
                f"Cluster {cluster_id} not found on endpoint {endpoint_id} while"
                f" issuing command {command} with args {args}"
            ) from exc
        commands: dict[int, ZCLCommandDef] = (
            cluster.server_commands
            if command_type == CLUSTER_COMMAND_SERVER
            else cluster.client_commands
        )
        if args is not None:
            self.warning(
                (
                    "args [%s] are deprecated and should be passed with the params key."
                    " The parameter names are: %s"
                ),
                args,
                [field.name for field in commands[command].schema.fields],
            )
            response = await getattr(cluster, commands[command].name)(*args)
        else:
            assert params is not None
            response = await getattr(cluster, commands[command].name)(
                **convert_to_zcl_values(params, commands[command].schema)
            )
        self.debug(
            "Issued cluster command: %s %s %s %s %s %s %s %s",
            f"{ATTR_CLUSTER_ID}: [{cluster_id}]",
            f"{ATTR_CLUSTER_TYPE}: [{cluster_type}]",
            f"{ATTR_ENDPOINT_ID}: [{endpoint_id}]",
            f"{ATTR_COMMAND}: [{command}]",
            f"{ATTR_COMMAND_TYPE}: [{command_type}]",
            f"{ATTR_ARGS}: [{args}]",
            f"{ATTR_PARAMS}: [{params}]",
            f"{ATTR_MANUFACTURER}: [{manufacturer}]",
        )
        if response is None:
            return  # client commands don't return a response
        if isinstance(response, Exception):
            raise ZHAException("Failed to issue cluster command") from response
        if response[1] is not ZclStatus.SUCCESS:
            raise ZHAException(
                f"Failed to issue cluster command with status: {response[1]}"
            )

    async def async_add_to_group(self, group_id: int) -> None:
        """Add this device to the provided zigbee group."""
        try:
            # A group name is required. However, the spec also explicitly states that
            # the group name can be ignored by the receiving device if a device cannot
            # store it, so we cannot rely on it existing after being written. This is
            # only done to make the ZCL command valid.
            await self._zigpy_device.add_to_group(group_id, name=f"0x{group_id:04X}")
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
            self.debug(
                "Failed to add device '%s' to group: 0x%04x ex: %s",
                self._zigpy_device.ieee,
                group_id,
                str(ex),
            )

    async def async_remove_from_group(self, group_id: int) -> None:
        """Remove this device from the provided zigbee group."""
        try:
            await self._zigpy_device.remove_from_group(group_id)
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
            self.debug(
                "Failed to remove device '%s' from group: 0x%04x ex: %s",
                self._zigpy_device.ieee,
                group_id,
                str(ex),
            )

    async def async_add_endpoint_to_group(
        self, endpoint_id: int, group_id: int
    ) -> None:
        """Add the device endpoint to the provided zigbee group."""
        try:
            await self._zigpy_device.endpoints[endpoint_id].add_to_group(
                group_id, name=f"0x{group_id:04X}"
            )
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
            self.debug(
                "Failed to add endpoint: %s for device: '%s' to group: 0x%04x ex: %s",
                endpoint_id,
                self._zigpy_device.ieee,
                group_id,
                str(ex),
            )

    async def async_remove_endpoint_from_group(
        self, endpoint_id: int, group_id: int
    ) -> None:
        """Remove the device endpoint from the provided zigbee group."""
        try:
            await self._zigpy_device.endpoints[endpoint_id].remove_from_group(group_id)
        except (zigpy.exceptions.ZigbeeException, TimeoutError) as ex:
            self.debug(
                (
                    "Failed to remove endpoint: %s for device '%s' from group: 0x%04x"
                    " ex: %s"
                ),
                endpoint_id,
                self._zigpy_device.ieee,
                group_id,
                str(ex),
            )

    async def async_bind_to_group(
        self, group_id: int, cluster_bindings: list[ClusterBinding]
    ) -> None:
        """Directly bind this device to a group for the given clusters."""
        await self._async_group_binding_operation(
            group_id, zdo_types.ZDOCmd.Bind_req, cluster_bindings
        )

    async def async_unbind_from_group(
        self, group_id: int, cluster_bindings: list[ClusterBinding]
    ) -> None:
        """Unbind this device from a group for the given clusters."""
        await self._async_group_binding_operation(
            group_id, zdo_types.ZDOCmd.Unbind_req, cluster_bindings
        )

    async def _async_group_binding_operation(
        self,
        group_id: int,
        operation: zdo_types.ZDOCmd,
        cluster_bindings: list[ClusterBinding],
    ) -> None:
        """Create or remove a direct zigbee binding between a device and a group."""

        zdo = self._zigpy_device.zdo
        op_msg = "0x%04x: %s %s, ep: %s, cluster: %s to group: 0x%04x"
        destination_address = zdo_types.MultiAddress()
        destination_address.addrmode = uint8_t(1)
        destination_address.nwk = uint16_t(group_id)

        tasks = []

        for cluster_binding in cluster_bindings:
            if cluster_binding.endpoint_id == 0:
                continue
            if (
                cluster_binding.id
                in self._zigpy_device.endpoints[
                    cluster_binding.endpoint_id
                ].out_clusters
            ):
                op_params = (
                    self.nwk,
                    operation.name,
                    str(self.ieee),
                    cluster_binding.endpoint_id,
                    cluster_binding.id,
                    group_id,
                )
                zdo.debug(f"processing {op_msg}", *op_params)
                tasks.append(
                    (
                        zdo.request(
                            operation,
                            self.ieee,
                            cluster_binding.endpoint_id,
                            cluster_binding.id,
                            destination_address,
                        ),
                        op_msg,
                        op_params,
                    )
                )
        res = await asyncio.gather(*(t[0] for t in tasks), return_exceptions=True)
        for outcome, log_msg in zip(res, tasks):
            if isinstance(outcome, Exception):
                fmt = f"{log_msg[1]} failed: %s"
            else:
                fmt = f"{log_msg[1]} completed: %s"
            zdo.debug(fmt, *(log_msg[2] + (outcome,)))

    def log(self, level: int, msg: str, *args: Any, **kwargs: Any) -> None:
        """Log a message."""
        msg = f"[%s](%s): {msg}"
        args = (self.nwk, self.model) + args
        _LOGGER.log(level, msg, *args, **kwargs)

    def _compute_primary_entity(self) -> None:
        """Compute the primary entity for this device."""

        # First, check if any entity is explicitly primary
        explicitly_primary = [
            entity for entity in self._platform_entities.values() if entity.primary
        ]

        if len(explicitly_primary) == 1:
            self.debug(
                "Device has a single explicitly primary entity,"
                " not performing weight matching"
            )
            return

        # It should not be possible for there to be more than one
        assert not explicitly_primary

        # For weight matching, only consider non-counter entities and entities which are
        # not explicitly marked as not primary
        candidates = [
            e
            for e in self._platform_entities.values()
            if e.enabled and e._attr_primary is not False
        ]
        candidates.sort(reverse=True, key=lambda e: e.primary_weight)

        if not candidates:
            return

        winner = candidates[0]
        others = candidates[1:]

        # We have a clear winner
        if not others or winner.primary_weight > others[0].primary_weight:
            if winner._attr_primary is not True:
                winner.primary = True
                winner.__dict__.pop("info_object", None)

            for entity in others:
                if entity._attr_primary is not False:
                    entity.primary = False
                    entity.__dict__.pop("info_object", None)

            return

        self.debug(
            "Primary entity tie between %s and %s, no primary entity", winner, others[0]
        )

        for entity in candidates:
            if entity._attr_primary is not False:
                entity.primary = False
                entity.__dict__.pop("info_object", None)

    def get_diagnostics_json(self):
        """Get ZHA device information."""

        info: dict[str, Any] = {}
        info["version"] = DIAGNOSTICS_JSON_VERSION
        info["ieee"] = str(self.ieee)
        info["nwk"] = str(self.nwk)
        info["manufacturer"] = self.device.manufacturer
        info["model"] = self.device.model
        info["friendly_manufacturer"] = self.manufacturer
        info["friendly_model"] = self.model
        info["name"] = self.name
        info["quirk_applied"] = self.quirk_applied
        info["quirk_class"] = self.quirk_class
        info["exposes_features"] = self.exposes_features
        info["manufacturer_code"] = self.manufacturer_code
        info["power_source"] = self.power_source
        info["lqi"] = self.lqi
        info["rssi"] = self.rssi
        info["last_seen"] = self.device._last_seen.isoformat()
        info["available"] = self.available
        info["device_type"] = self.device_type
        info["active_coordinator"] = self.is_active_coordinator

        node_desc = self.device.node_desc
        info["node_descriptor"] = {
            "logical_type": node_desc.logical_type.name,
            "complex_descriptor_available": bool(
                node_desc.complex_descriptor_available
            ),
            "user_descriptor_available": bool(node_desc.user_descriptor_available),
            "reserved": node_desc.reserved,
            "aps_flags": node_desc.aps_flags,
            "frequency_band": node_desc.frequency_band,
            "mac_capability_flags": node_desc.mac_capability_flags,
            "manufacturer_code": node_desc.manufacturer_code,
            "maximum_buffer_size": node_desc.maximum_buffer_size,
            "maximum_incoming_transfer_size": node_desc.maximum_incoming_transfer_size,
            "server_mask": node_desc.server_mask,
            "maximum_outgoing_transfer_size": node_desc.maximum_outgoing_transfer_size,
            "descriptor_capability_field": node_desc.descriptor_capability_field,
        }

        info["endpoints"] = {}

        for endpoint in sorted(
            self.device.non_zdo_endpoints, key=lambda ep: ep.endpoint_id
        ):
            info["endpoints"][endpoint.endpoint_id] = {
                "profile_id": endpoint.profile_id,
                "device_type": {
                    "name": (
                        (
                            PROFILES[endpoint.profile_id]
                            .DeviceType(endpoint.device_type)
                            .name
                        )
                        if endpoint.profile_id in PROFILES
                        and endpoint.device_type is not None
                        else UNKNOWN
                    ),
                    "id": endpoint.device_type,
                },
                "in_clusters": [
                    {
                        "cluster_id": f"0x{cluster_id:04x}",
                        "endpoint_attribute": cluster.ep_attribute,
                        "attributes": get_cluster_attr_data(cluster),
                    }
                    for cluster_id, cluster in sorted(endpoint.in_clusters.items())
                ],
                "out_clusters": [
                    {
                        "cluster_id": f"0x{cluster_id:04x}",
                        "endpoint_attribute": cluster.ep_attribute,
                        "attributes": get_cluster_attr_data(cluster),
                    }
                    for cluster_id, cluster in sorted(endpoint.out_clusters.items())
                ],
            }

        original_signature = self.device.original_signature

        # if we have a quirked device we add the original signature to the output and
        # convert the profile_id, device_type, input_clusters and output_clusters to hex
        # representation to make it consistent with the rest of the data
        if original_signature is not None:
            if "endpoints" in original_signature:
                for ep in original_signature["endpoints"].values():
                    if "profile_id" in ep:
                        ep["profile_id"] = f"0x{ep['profile_id']:04x}"

                    if "device_type" in ep:
                        ep["device_type"] = f"0x{ep['device_type']:04x}"

                    if "input_clusters" in ep:
                        ep["input_clusters"] = [
                            f"0x{c:04x}" for c in ep["input_clusters"]
                        ]

                    if "output_clusters" in ep:
                        ep["output_clusters"] = [
                            f"0x{c:04x}" for c in ep["output_clusters"]
                        ]

            info["original_signature"] = original_signature

        info["zha_lib_entities"] = defaultdict(list)

        for (platform, _unique_id), platform_entity in sorted(
            self.platform_entities.items()
        ):
            info_object = dataclasses.asdict(platform_entity.info_object)
            info_object["clusters"].sort(key=lambda i: (i["id"], i["type"]))
            info_object["migrate_unique_ids"] = list(info_object["migrate_unique_ids"])
            info_object["device_ieee"] = str(info_object["device_ieee"])

            obj: dict[str, Any] = {
                "info_object": info_object,
                "state": platform_entity.state,
            }

            if platform_entity.extra_state_attribute_names is not None:
                obj["extra_state_attributes"] = sorted(
                    platform_entity.extra_state_attribute_names
                )

            info["zha_lib_entities"][platform].append(obj)

        topology = self.gateway.application_controller.topology
        info["neighbors"] = [
            {
                "device_type": neighbor.device_type.name,
                "rx_on_when_idle": neighbor.rx_on_when_idle.name,
                "relationship": neighbor.relationship.name,
                "extended_pan_id": str(neighbor.extended_pan_id),
                "ieee": str(neighbor.ieee),
                "nwk": str(neighbor.nwk),
                "permit_joining": neighbor.permit_joining.name,
                "depth": neighbor.depth,
                "lqi": neighbor.lqi,
            }
            for neighbor in topology.neighbors[self.device.ieee]
        ]

        info["routes"] = [
            {
                "dest_nwk": str(route.DstNWK),
                "route_status": str(route.RouteStatus.name),
                "memory_constrained": bool(route.MemoryConstrained),
                "many_to_one": bool(route.ManyToOne),
                "route_record_required": bool(route.RouteRecordRequired),
                "next_hop": str(route.NextHop),
            }
            for route in topology.routes[self.device.ieee]
        ]

        return info
