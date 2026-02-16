"""Virtual gateway for Zigbee Home Automation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from functools import partial
import logging
import time
from typing import Any, Final, Self, TypeVar, cast

from zhaquirks import setup as setup_quirks
from zigpy.application import ControllerApplication
from zigpy.config import (
    CONF_DEVICE,
    CONF_DEVICE_BAUDRATE,
    CONF_DEVICE_FLOW_CONTROL,
    CONF_DEVICE_PATH,
    CONF_NWK,
    CONF_NWK_COUNTRY_CODE,
    CONF_NWK_VALIDATE_SETTINGS,
)
import zigpy.device
import zigpy.endpoint
import zigpy.group
from zigpy.quirks.v2 import UNBUILT_QUIRK_BUILDERS
from zigpy.state import State
import zigpy.types as t
from zigpy.types.named import EUI64

from zha.application import discovery
from zha.application.const import (
    UNKNOWN_MANUFACTURER,
    UNKNOWN_MODEL,
    ZHA_GW_MSG,
    ZHA_GW_MSG_CONNECTION_LOST,
    ZHA_GW_MSG_DEVICE_FULL_INIT,
    ZHA_GW_MSG_DEVICE_JOINED,
    ZHA_GW_MSG_DEVICE_LEFT,
    ZHA_GW_MSG_DEVICE_REMOVED,
    ZHA_GW_MSG_GROUP_ADDED,
    ZHA_GW_MSG_GROUP_MEMBER_ADDED,
    ZHA_GW_MSG_GROUP_MEMBER_REMOVED,
    ZHA_GW_MSG_GROUP_REMOVED,
    ZHA_GW_MSG_RAW_INIT,
    RadioType,
)
from zha.application.helpers import DeviceAvailabilityChecker, GlobalUpdater, ZHAData
from zha.async_ import (
    AsyncUtilMixin,
    create_eager_task,
    gather_with_limited_concurrency,
)
from zha.debounce import Debouncer
from zha.event import EventBase
from zha.zigbee.device import Device, DeviceInfo, DeviceStatus, ExtendedDeviceInfo
from zha.zigbee.group import Group, GroupInfo, GroupMemberReference

BLOCK_LOG_TIMEOUT: Final[int] = 60
SHUT_DOWN_DELAY_S: Final[float] = 0.1
GROUP_RECONCILE_DEBOUNCE_S: Final[float] = 0.1
_R = TypeVar("_R")
_LOGGER = logging.getLogger(__name__)


class DevicePairingStatus(Enum):
    """Status of a device."""

    PAIRED = 1
    INTERVIEW_COMPLETE = 2
    CONFIGURED = 3
    INITIALIZED = 4


@dataclass(kw_only=True, frozen=True)
class DeviceInfoWithPairingStatus(DeviceInfo):
    """Information about a device with pairing status."""

    pairing_status: DevicePairingStatus


@dataclass(kw_only=True, frozen=True)
class ExtendedDeviceInfoWithPairingStatus(ExtendedDeviceInfo):
    """Information about a device with pairing status."""

    pairing_status: DevicePairingStatus


@dataclass(kw_only=True, frozen=True)
class DeviceJoinedDeviceInfo:
    """Information about a device."""

    ieee: str
    nwk: int
    pairing_status: DevicePairingStatus


@dataclass(kw_only=True, frozen=True)
class ConnectionLostEvent:
    """Event to signal that the connection to the radio has been lost."""

    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_CONNECTION_LOST
    exception: Exception | None = None


@dataclass(kw_only=True, frozen=True)
class DeviceJoinedEvent:
    """Event to signal that a device has joined the network."""

    device_info: DeviceJoinedDeviceInfo
    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_DEVICE_JOINED


@dataclass(kw_only=True, frozen=True)
class DeviceLeftEvent:
    """Event to signal that a device has left the network."""

    ieee: EUI64
    nwk: int
    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_DEVICE_LEFT


@dataclass(kw_only=True, frozen=True)
class RawDeviceInitializedDeviceInfo(DeviceJoinedDeviceInfo):
    """Information about a device that has been initialized without quirks loaded."""

    model: str
    manufacturer: str
    signature: dict[str, Any]


@dataclass(kw_only=True, frozen=True)
class RawDeviceInitializedEvent:
    """Event to signal that a device has been initialized without quirks loaded."""

    device_info: RawDeviceInitializedDeviceInfo
    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_RAW_INIT


@dataclass(kw_only=True, frozen=True)
class DeviceFullInitEvent:
    """Event to signal that a device has been fully initialized."""

    device_info: ExtendedDeviceInfoWithPairingStatus
    new_join: bool = False
    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_DEVICE_FULL_INIT


@dataclass(kw_only=True, frozen=True)
class GroupEvent:
    """Event to signal a group event."""

    event: str
    group_info: GroupInfo
    event_type: Final[str] = ZHA_GW_MSG


@dataclass(kw_only=True, frozen=True)
class DeviceRemovedEvent:
    """Event to signal that a device has been removed."""

    device_info: ExtendedDeviceInfo
    event_type: Final[str] = ZHA_GW_MSG
    event: Final[str] = ZHA_GW_MSG_DEVICE_REMOVED


class Gateway(AsyncUtilMixin, EventBase):
    """Gateway that handles events that happen on the ZHA Zigbee network."""

    GROUP_RECONCILE_DEBOUNCE_S: Final[float] = GROUP_RECONCILE_DEBOUNCE_S

    def __init__(self, config: ZHAData) -> None:
        """Initialize the gateway."""
        super().__init__()
        self.config: ZHAData = config
        self._devices: dict[EUI64, Device] = {}
        self._groups: dict[int, Group] = {}
        self.application_controller: ControllerApplication = None
        self.coordinator_zha_device: Device | None = None

        self.shutting_down: bool = False
        self._reload_task: asyncio.Task | None = None
        self._startup_fetch_task: asyncio.Task | None = None
        self._device_init_generation: dict[EUI64, int] = {}
        self._device_init_task_generations: dict[asyncio.Task, int] = {}
        self._group_reconcile_debouncers: dict[int, Debouncer[None]] = {}

        self.global_updater: GlobalUpdater = GlobalUpdater(self)
        self._device_availability_checker: DeviceAvailabilityChecker = (
            DeviceAvailabilityChecker(self)
        )
        self.config.gateway = self

    @property
    def radio_type(self) -> RadioType:
        """Get the current radio type."""
        return RadioType[self.config.config.coordinator_configuration.radio_type]

    def get_application_controller_data(self) -> tuple[ControllerApplication, dict]:
        """Get an uninitialized instance of a zigpy `ControllerApplication`."""
        app_config = self.config.zigpy_config
        app_config[CONF_DEVICE] = {
            CONF_DEVICE_PATH: self.config.config.coordinator_configuration.path,
            CONF_DEVICE_BAUDRATE: self.config.config.coordinator_configuration.baudrate,
            CONF_DEVICE_FLOW_CONTROL: self.config.config.coordinator_configuration.flow_control,
        }

        if (
            self.config.country_code is not None
            and CONF_NWK_COUNTRY_CODE not in app_config.get(CONF_NWK, {})
        ):
            app_config.setdefault(CONF_NWK, {})
            app_config[CONF_NWK][CONF_NWK_COUNTRY_CODE] = self.config.country_code

        if CONF_NWK_VALIDATE_SETTINGS not in app_config:
            app_config[CONF_NWK_VALIDATE_SETTINGS] = True

        return self.radio_type.controller, app_config

    @classmethod
    async def async_from_config(cls, config: ZHAData) -> Self:
        """Create an instance of a gateway from config objects."""
        instance = cls(config)

        if config.config.quirks_configuration.enabled:
            for quirk in UNBUILT_QUIRK_BUILDERS:
                # v2 quirks with no manufacturer model metadata explicitly do not call
                # add_to_registry. They are used to share code between v2 quirks.
                if quirk.manufacturer_model_metadata:
                    _LOGGER.warning(
                        "Found a v2 quirk that was not added to the registry: %s",
                        quirk,
                    )
                    quirk.add_to_registry()

            UNBUILT_QUIRK_BUILDERS.clear()

            await instance.async_add_executor_job(
                setup_quirks,
                instance.config.config.quirks_configuration.custom_quirks_path,
            )

        return instance

    async def _async_initialize(self) -> None:
        """Initialize controller and connect radio."""
        self.shutting_down = False

        app_controller_cls, app_config = self.get_application_controller_data()
        self.application_controller = await app_controller_cls.new(
            config=app_config,
            auto_form=False,
            start_radio=False,
        )

        await self.application_controller.startup(auto_form=True)

        self.coordinator_zha_device = self.get_or_create_device(
            self._find_coordinator_device()
        )

        await self.load_devices()
        self.load_groups()

        self.application_controller.add_listener(self)
        self.application_controller.groups.add_listener(self)
        self.global_updater.start()
        self._device_availability_checker.start()

    async def async_initialize(self) -> None:
        """Initialize controller and connect radio."""
        try:
            await self._async_initialize()
        except Exception:
            await self.shutdown()
            raise

    @asynccontextmanager
    async def request_priority(
        self, priority: t.PacketPriority | None
    ) -> AsyncGenerator[None, None]:
        """Context manager to request a priority for radio access."""
        async with self.application_controller.request_priority(priority):
            yield

    def connection_lost(self, exc: Exception) -> None:
        """Handle connection lost event."""
        _LOGGER.debug("Connection to the radio was lost: %r", exc)
        self.emit(ZHA_GW_MSG_CONNECTION_LOST, ConnectionLostEvent(exception=exc))

    def _find_coordinator_device(self) -> zigpy.device.Device:
        zigpy_coordinator = self.application_controller.get_device(nwk=0x0000)

        if last_backup := self.application_controller.backups.most_recent_backup():
            with suppress(KeyError):
                zigpy_coordinator = self.application_controller.get_device(
                    ieee=last_backup.node_info.ieee
                )

        return zigpy_coordinator

    async def load_devices(self) -> None:
        """Restore ZHA devices from zigpy application state."""

        assert self.application_controller
        for zigpy_device in self.application_controller.devices.values():
            zha_device = self.get_or_create_device(zigpy_device)
            delta_msg = "not known"
            if zha_device.last_seen is not None:
                delta = round(time.time() - zha_device.last_seen)
                delta_msg = f"{str(timedelta(seconds=delta))} ago"
            _LOGGER.debug(
                (
                    "[%s](%s) restored as '%s', last seen: %s,"
                    " consider_unavailable_time: %s seconds"
                ),
                zha_device.nwk,
                zha_device.name,
                "available" if zha_device.available else "unavailable",
                delta_msg,
                zha_device.consider_unavailable_time,
            )

            await zha_device.async_initialize(from_cache=True)

    def load_groups(self) -> None:
        """Initialize ZHA groups."""

        for group_id, group in self.application_controller.groups.items():
            _LOGGER.info("Loading group with id: 0x%04x", group_id)
            zha_group = self.get_or_create_group(group)
            # we can do this here because the entities are in the
            # entity registry tied to the devices

            for entity in discovery.discover_group_entities(zha_group):
                entity.on_add()

    @property
    def radio_concurrency(self) -> int:
        """Maximum configured radio concurrency."""
        return (
            self.application_controller._concurrent_requests_semaphore.max_concurrency
        )  # pylint: disable=protected-access

    async def async_fetch_updated_state_mains(self) -> None:
        """Fetch updated state for mains powered devices."""
        _LOGGER.debug("Fetching current state for mains powered devices")

        now = time.time()

        # Only delay startup to poll mains-powered devices that are online
        online_devices = [
            dev
            for dev in self.devices.values()
            if dev.is_mains_powered
            and dev.last_seen is not None
            and (now - dev.last_seen) < dev.consider_unavailable_time
        ]

        # Prioritize devices that have recently been contacted
        online_devices.sort(key=lambda dev: cast(float, dev.last_seen), reverse=True)

        # Make sure that we always leave slots for non-startup requests
        max_poll_concurrency = max(1, self.radio_concurrency - 4)

        await gather_with_limited_concurrency(
            max_poll_concurrency,
            *(dev.async_initialize(from_cache=False) for dev in online_devices),
        )

        _LOGGER.debug("completed fetching current state for mains powered devices")

    async def async_initialize_devices_and_entities(self) -> None:
        """Initialize devices and load entities."""

        _LOGGER.debug("Initializing all devices from Zigpy cache")
        await asyncio.gather(
            *(dev.async_initialize(from_cache=True) for dev in self.devices.values())
        )

        async def fetch_updated_state() -> None:
            """Fetch updated state for mains powered devices."""
            try:
                if self.config.config.device_options.enable_mains_startup_polling:
                    async with self.request_priority(t.PacketPriority.LOW):
                        await self.async_fetch_updated_state_mains()
                else:
                    _LOGGER.debug(
                        "Polling of mains powered devices at startup is disabled"
                    )
            except Exception:  # pylint: disable=broad-exception-caught
                _LOGGER.warning(
                    "Failed to fetch startup state for mains powered devices",
                    exc_info=True,
                )
            finally:
                _LOGGER.debug("Allowing polled requests")
                self.config.allow_polling = True

        # background the fetching of state for mains powered devices
        if self._startup_fetch_task is None or self._startup_fetch_task.done():
            self._startup_fetch_task = self.async_create_background_task(
                fetch_updated_state(), "zha.gateway-fetch_updated_state"
            )
            self._startup_fetch_task.add_done_callback(self._startup_fetch_done)

    def _startup_fetch_done(self, task: asyncio.Task) -> None:
        """Track completion for startup mains polling task and consume exceptions."""
        if self._startup_fetch_task is task:
            self._startup_fetch_task = None

        with suppress(asyncio.CancelledError):
            if (exc := task.exception()) is not None:
                _LOGGER.warning(
                    "Unhandled exception in startup mains polling task",
                    exc_info=exc,
                )

    def _device_init_task_done(self, ieee: EUI64, task: asyncio.Task) -> None:
        """Remove device init task mapping only if this task is still current."""
        self._device_init_task_generations.pop(task, None)
        current = self._device_init_tasks.get(ieee)
        if current is task:
            self._device_init_tasks.pop(ieee, None)

    def _bump_device_init_generation(self, ieee: EUI64) -> int:
        """Advance and return the init generation token for a device."""
        generation = self._device_init_generation.get(ieee, 0) + 1
        self._device_init_generation[ieee] = generation
        return generation

    def _is_current_device_init_task(self, ieee: EUI64) -> bool:
        """Return True when called from the active init task for this ieee."""
        task = asyncio.current_task()
        if task is None:
            return True

        task_generation = self._device_init_task_generations.get(task)
        if task_generation is None:
            # Direct calls to async_device_initialized from tests/helpers.
            return True

        return self._device_init_tasks.get(ieee) is task and (
            self._device_init_generation.get(ieee) == task_generation
        )

    def _get_or_create_group_reconcile_debouncer(
        self, group_id: int
    ) -> Debouncer[None]:
        """Return the per-group debouncer for reconcile work."""
        debouncer = self._group_reconcile_debouncers.get(group_id)
        if debouncer is not None:
            return debouncer

        async def reconcile_group() -> None:
            zha_group = self._groups.get(group_id)
            if zha_group is None:
                return
            await zha_group.async_reconcile_discovered_entities()

        debouncer = Debouncer(
            gateway=self,
            logger=_LOGGER,
            cooldown=GROUP_RECONCILE_DEBOUNCE_S,
            immediate=False,
            function=reconcile_group,
        )
        self._group_reconcile_debouncers[group_id] = debouncer
        return debouncer

    def _schedule_group_reconciliation(self, zha_group: Group, reason: str) -> None:
        """Schedule async group-entity reconciliation for membership changes."""
        _LOGGER.debug(
            "Scheduling debounced group reconcile for 0x%04x due to %s",
            zha_group.group_id,
            reason,
        )
        self._get_or_create_group_reconcile_debouncer(
            zha_group.group_id
        ).async_schedule_call()

    def device_joined(self, device: zigpy.device.Device) -> None:
        """Handle device joined.

        At this point, no information about the device is known other than its
        address
        """

        self.emit(
            ZHA_GW_MSG_DEVICE_JOINED,
            DeviceJoinedEvent(
                device_info=DeviceJoinedDeviceInfo(
                    ieee=device.ieee,
                    nwk=device.nwk,
                    pairing_status=DevicePairingStatus.PAIRED,
                )
            ),
        )

    def raw_device_initialized(self, device: zigpy.device.Device) -> None:  # pylint: disable=unused-argument
        """Handle a device initialization without quirks loaded."""

        self.emit(
            ZHA_GW_MSG_RAW_INIT,
            RawDeviceInitializedEvent(
                device_info=RawDeviceInitializedDeviceInfo(
                    ieee=device.ieee,
                    nwk=device.nwk,
                    pairing_status=DevicePairingStatus.INTERVIEW_COMPLETE,
                    model=device.model if device.model else UNKNOWN_MODEL,
                    manufacturer=device.manufacturer
                    if device.manufacturer
                    else UNKNOWN_MANUFACTURER,
                    signature=device.get_signature(),
                )
            ),
        )

    def device_initialized(self, device: zigpy.device.Device) -> None:
        """Handle device joined and basic information discovered."""
        generation = self._bump_device_init_generation(device.ieee)
        if device.ieee in self._device_init_tasks:
            _LOGGER.warning(
                "Cancelling previous initialization task for device %s",
                str(device.ieee),
            )
            self._device_init_tasks[device.ieee].cancel()
        self._device_init_tasks[device.ieee] = init_task = self.async_create_task(
            self.async_device_initialized(device),
            name=f"device_initialized_task_{str(device.ieee)}:0x{device.nwk:04x}",
            eager_start=True,
        )
        self._device_init_task_generations[init_task] = generation
        init_task.add_done_callback(partial(self._device_init_task_done, device.ieee))

    def device_left(self, device: zigpy.device.Device) -> None:
        """Handle device leaving the network."""
        zha_device = self._devices.get(device.ieee)
        if zha_device is not None:
            zha_device.on_network = False

        self.async_update_device(device, available=False)
        self.emit(
            ZHA_GW_MSG_DEVICE_LEFT, DeviceLeftEvent(ieee=device.ieee, nwk=device.nwk)
        )

    def group_member_removed(
        self, zigpy_group: zigpy.group.Group, endpoint: zigpy.endpoint.Endpoint
    ) -> None:
        """Handle zigpy group member removed event."""
        # need to handle endpoint correctly on groups
        zha_group = self.get_or_create_group(zigpy_group)
        zha_group.clear_caches()
        if len(zigpy_group.members) < 2:
            # Preserve existing behavior for entity teardown when a group drops
            # below the discovery threshold.
            if debouncer := self._group_reconcile_debouncers.pop(
                zha_group.group_id, None
            ):
                debouncer.async_cancel()
            self.track_task(
                create_eager_task(
                    zha_group.async_reconcile_discovered_entities(),
                    name=f"Gateway.group_member_removed_reconcile_0x{zha_group.group_id:04x}",
                )
            )
        else:
            self._schedule_group_reconciliation(zha_group, "member_removed")

        zha_group.info("group_member_removed - endpoint: %s", endpoint)
        self._emit_group_gateway_message(zigpy_group, ZHA_GW_MSG_GROUP_MEMBER_REMOVED)

    def group_member_added(
        self, zigpy_group: zigpy.group.Group, endpoint: zigpy.endpoint.Endpoint
    ) -> None:
        """Handle zigpy group member added event."""
        # need to handle endpoint correctly on groups
        zha_group = self.get_or_create_group(zigpy_group)
        zha_group.clear_caches()
        self._schedule_group_reconciliation(zha_group, "member_added")

        zha_group.info("group_member_added - endpoint: %s", endpoint)
        self._emit_group_gateway_message(zigpy_group, ZHA_GW_MSG_GROUP_MEMBER_ADDED)

    def group_added(self, zigpy_group: zigpy.group.Group) -> None:
        """Handle zigpy group added event."""
        zha_group = self.get_or_create_group(zigpy_group)
        zha_group.info("group_added")
        # need to dispatch for entity creation here
        self._emit_group_gateway_message(zigpy_group, ZHA_GW_MSG_GROUP_ADDED)

    def group_removed(self, zigpy_group: zigpy.group.Group) -> None:
        """Handle zigpy group removed event."""
        if debouncer := self._group_reconcile_debouncers.pop(
            zigpy_group.group_id, None
        ):
            debouncer.async_shutdown()

        self._emit_group_gateway_message(zigpy_group, ZHA_GW_MSG_GROUP_REMOVED)
        zha_group = self._groups.pop(zigpy_group.group_id, None)
        if zha_group is None:
            return
        zha_group.info("group_removed")
        self.track_task(
            create_eager_task(
                zha_group.on_remove(),
                name=f"Gateway.group_removed_0x{zigpy_group.group_id:04x}",
            )
        )

    def _emit_group_gateway_message(  # pylint: disable=unused-argument
        self,
        zigpy_group: zigpy.group.Group,
        gateway_message_type: str,
    ) -> None:
        """Send the gateway event for a zigpy group event."""
        zha_group = self._groups.get(zigpy_group.group_id)
        if zha_group is not None:
            self.emit(
                gateway_message_type,
                GroupEvent(
                    event=gateway_message_type,
                    group_info=zha_group.info_object,
                ),
            )

    def device_removed(self, device: zigpy.device.Device) -> None:
        """Handle device being removed from the network."""
        _LOGGER.info("Removing device %s - %s", device.ieee, f"0x{device.nwk:04x}")
        self._bump_device_init_generation(device.ieee)
        if init_task := self._device_init_tasks.pop(device.ieee, None):
            self._device_init_task_generations.pop(init_task, None)
            if not init_task.done():
                init_task.cancel()
        zha_device = self._devices.pop(device.ieee, None)
        if zha_device is not None:
            device_info = zha_device.extended_device_info
            self.track_task(
                create_eager_task(
                    zha_device.on_remove(), name="Gateway._async_remove_device"
                )
            )
            if device_info is not None:
                self.emit(
                    ZHA_GW_MSG_DEVICE_REMOVED,
                    DeviceRemovedEvent(device_info=device_info),
                )

    def get_device(self, ieee: EUI64) -> Device | None:
        """Return Device for given ieee."""
        return self._devices.get(ieee)

    def get_group(self, group_id_or_name: int | str) -> Group | None:
        """Return Group for given group id or group name."""
        if isinstance(group_id_or_name, str):
            for group in self.groups.values():
                if group.name == group_id_or_name:
                    return group
            return None
        return self.groups.get(group_id_or_name)

    @property
    def state(self) -> State:
        """Return the active coordinator's network state."""
        return self.application_controller.state

    @property
    def devices(self) -> dict[EUI64, Device]:
        """Return devices."""
        return self._devices

    @property
    def groups(self) -> dict[int, Group]:
        """Return groups."""
        return self._groups

    def get_or_create_device(self, zigpy_device: zigpy.device.Device) -> Device:
        """Get or create a ZHA device."""
        if (zha_device := self._devices.get(zigpy_device.ieee)) is None:
            zha_device = Device.new(zigpy_device, self)
            self._devices[zigpy_device.ieee] = zha_device

        return zha_device

    def get_or_create_group(self, zigpy_group: zigpy.group.Group) -> Group:
        """Get or create a ZHA group."""
        zha_group = self._groups.get(zigpy_group.group_id)
        if zha_group is None:
            zha_group = Group(self, zigpy_group)
            self._groups[zigpy_group.group_id] = zha_group
        return zha_group

    def async_update_device(
        self,
        sender: zigpy.device.Device,
        available: bool = True,
    ) -> None:
        """Update device that has just become available."""
        if sender.ieee in self.devices:
            device = self.devices[sender.ieee]
            # avoid a race condition during new joins
            if device.status is DeviceStatus.INITIALIZED:
                device.update_available(available)

    async def async_device_initialized(self, device: zigpy.device.Device) -> None:
        """Handle device joined and basic information discovered (async)."""
        if not self._is_current_device_init_task(device.ieee):
            _LOGGER.debug(
                "Skipping stale async_device_initialized for device %s",
                device.ieee,
            )
            return
        zha_device = self.get_or_create_device(device)
        _LOGGER.debug(
            "device - %s:%s entering async_device_initialized - is_new_join: %s",
            device.nwk,
            device.ieee,
            zha_device.status is not DeviceStatus.INITIALIZED,
        )

        if zha_device.status is DeviceStatus.INITIALIZED:
            # ZHA already has an initialized device so either the device was assigned a
            # new nwk or device was physically reset and added again without being removed
            _LOGGER.debug(
                "device - %s:%s has been reset and re-added or its nwk address changed",
                device.nwk,
                device.ieee,
            )
            await self._async_device_rejoined(zha_device)
        else:
            _LOGGER.debug(
                "device - %s:%s has joined the ZHA zigbee network",
                device.nwk,
                device.ieee,
            )
            await self._async_device_joined(zha_device)

        if not self._is_current_device_init_task(device.ieee):
            _LOGGER.debug(
                "Skipping stale async_device_initialized completion for device %s",
                device.ieee,
            )
            return

        device_info = ExtendedDeviceInfoWithPairingStatus(
            pairing_status=DevicePairingStatus.INITIALIZED,
            **zha_device.extended_device_info.__dict__,
        )
        self.emit(
            ZHA_GW_MSG_DEVICE_FULL_INIT,
            DeviceFullInitEvent(device_info=device_info),
        )

    async def _async_device_joined(self, zha_device: Device) -> None:
        if not self._is_current_device_init_task(zha_device.ieee):
            return

        zha_device.available = True
        zha_device.on_network = True

        async with self.request_priority(t.PacketPriority.HIGH):
            await zha_device.async_configure()
            await zha_device.async_initialize()

        if not self._is_current_device_init_task(zha_device.ieee):
            return

        self.emit(
            ZHA_GW_MSG_DEVICE_FULL_INIT,
            DeviceFullInitEvent(
                device_info=ExtendedDeviceInfoWithPairingStatus(
                    pairing_status=DevicePairingStatus.CONFIGURED,
                    **zha_device.extended_device_info.__dict__,
                ),
                new_join=True,
            ),
        )

    async def _async_device_rejoined(self, zha_device: Device) -> None:
        if not self._is_current_device_init_task(zha_device.ieee):
            return

        _LOGGER.debug(
            "skipping discovery for previously discovered device - %s:%s",
            zha_device.nwk,
            zha_device.ieee,
        )
        # we don't have to do this on a nwk swap
        # but we don't have a way to tell currently
        await zha_device.async_configure()

        if not self._is_current_device_init_task(zha_device.ieee):
            return

        self.emit(
            ZHA_GW_MSG_DEVICE_FULL_INIT,
            DeviceFullInitEvent(
                device_info=ExtendedDeviceInfoWithPairingStatus(
                    pairing_status=DevicePairingStatus.CONFIGURED,
                    **zha_device.extended_device_info.__dict__,
                )
            ),
        )
        # Mark the device as unavailable, `async_initialize` will be called later
        zha_device.available = False
        zha_device.on_network = True

    async def async_create_zigpy_group(
        self,
        name: str,
        members: list[GroupMemberReference] | None,
        group_id: int | None = None,
    ) -> Group | None:
        """Create a new Zigpy Zigbee group."""

        # we start with two to fill any gaps from a user removing existing groups

        if group_id is None:
            group_id = 2
            while group_id in self.groups:
                group_id += 1

        # guard against group already existing
        if self.get_group(name) is None:
            self.application_controller.groups.add_group(group_id, name)
            if members is not None:
                tasks = []
                for member in members:
                    _LOGGER.debug(
                        (
                            "Adding member with IEEE: %s and endpoint ID: %s to group:"
                            " %s:0x%04x"
                        ),
                        member.ieee,
                        member.endpoint_id,
                        name,
                        group_id,
                    )
                    tasks.append(
                        self.devices[member.ieee].async_add_endpoint_to_group(
                            member.endpoint_id, group_id
                        )
                    )
                await asyncio.gather(*tasks)

        zha_group = self.groups.get(group_id)
        if zha_group is not None and members:
            # Preserve existing API behavior: groups created via this path should
            # have entities reconciled by the time this coroutine returns.
            await zha_group.async_reconcile_discovered_entities()
        return zha_group

    async def async_remove_device(self, ieee: EUI64) -> None:
        """Remove a device from ZHA."""
        if not (device := self.devices.get(ieee)):
            _LOGGER.debug("Device: %s could not be found", ieee)
            return
        if device.is_active_coordinator:
            _LOGGER.info("Removing the active coordinator (%s) is not allowed", ieee)
            return
        for group_id, group in self.groups.items():
            for member_ieee_endpoint_id in list(group.zigpy_group.members.keys()):
                if member_ieee_endpoint_id[0] == ieee:
                    await device.async_remove_from_group(group_id)

        await self.application_controller.remove(ieee)

    async def async_remove_zigpy_group(self, group_id: int) -> None:
        """Remove a Zigbee group from Zigpy."""
        if not (group := self.groups.get(group_id)):
            _LOGGER.debug("Group: 0x%04x could not be found", group_id)
            return
        if group.members:
            tasks = []
            for member in group.members:
                tasks.append(member.async_remove_from_group())
            if tasks:
                await asyncio.gather(*tasks)
        self.application_controller.groups.pop(group_id)

    async def shutdown(self) -> None:
        """Stop ZHA Controller Application."""
        if self.shutting_down:
            _LOGGER.debug("Ignoring duplicate shutdown event")
            return

        self.shutting_down = True

        if self._startup_fetch_task and not self._startup_fetch_task.done():
            self._startup_fetch_task.cancel()
        self._startup_fetch_task = None

        for debouncer in self._group_reconcile_debouncers.values():
            debouncer.async_shutdown()
        self._group_reconcile_debouncers.clear()

        for init_task in self._device_init_tasks.values():
            if not init_task.done():
                init_task.cancel()
        self._device_init_tasks.clear()
        self._device_init_task_generations.clear()
        self._device_init_generation.clear()

        self.global_updater.stop()
        self._device_availability_checker.stop()

        for device in self._devices.values():
            try:
                await device.on_remove()
            except Exception:
                _LOGGER.warning(
                    "Failed to remove device %s during shutdown",
                    device,
                    exc_info=True,
                )

        for group in self._groups.values():
            try:
                await group.on_remove()
            except Exception:
                _LOGGER.warning(
                    "Failed to remove group %s during shutdown",
                    group,
                    exc_info=True,
                )

        _LOGGER.debug("Shutting down ZHA ControllerApplication")
        if self.application_controller is not None:
            await self.application_controller.shutdown()
            self.application_controller = None
            # give bellows thread callback a chance to run
            await asyncio.sleep(SHUT_DOWN_DELAY_S)

        await super().shutdown()

        self._devices.clear()
        self._groups.clear()

    def handle_message(  # pylint: disable=unused-argument
        self,
        sender: zigpy.device.Device,
        profile: int,
        cluster: int,
        src_ep: int,
        dst_ep: int,
        message: bytes,
    ) -> None:
        """Handle message from a device Event handler."""
        if sender.ieee in self.devices and not self.devices[sender.ieee].available:
            self.devices[sender.ieee].on_network = True
            self.async_update_device(sender, available=True)

    async def network_scan(
        self, channels: t.Channels, duration_exp: int
    ) -> AsyncGenerator[t.NetworkBeacon, None]:
        """Scan for 802.15.4 networks, if supported."""
        async for network in self.application_controller.network_scan(
            channels=channels, duration_exp=duration_exp
        ):
            yield network

    async def energy_scan(
        self, channels: t.Channels, duration_exp: int, count: int
    ) -> dict[int, float]:
        """Run an energy detection scan."""
        return await self.application_controller.energy_scan(
            channels=channels, duration_exp=duration_exp, count=count
        )
