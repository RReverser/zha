"""Locks on Zigbee Home Automation networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from zigpy.zcl.clusters.closures import DoorLock as DoorLockCluster
from zigpy.zcl.foundation import Status

from zha.application import Platform
from zha.application.platforms import ClusterMatch, PlatformEntity, register_entity
from zha.application.platforms.cluster_config import entity_cluster_configs_from_refs
from zha.application.platforms.cluster_names import (
    ARGS,
    CLUSTER_DOORLOCK,
    CLUSTER_ID,
    COMMAND,
    PARAMS,
    UNIQUE_ID,
)
from zha.application.platforms.lock.const import (
    STATE_LOCKED,
    STATE_UNLOCKED,
    VALUE_TO_STATE,
)
from zha.zigbee.cluster_events import ClusterAttributeUpdatedEvent, ClusterCommandEvent

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint


@register_entity(DoorLockCluster.cluster_id)
class DoorLock(PlatformEntity):
    """Representation of a ZHA lock."""

    PLATFORM = Platform.LOCK
    _attr_translation_key: str = "door_lock"
    _attr_primary_weight = 5

    _cluster_match = ClusterMatch(clusters=frozenset({CLUSTER_DOORLOCK}))
    _entity_cluster_configs = entity_cluster_configs_from_refs(
        (CLUSTER_DOORLOCK, False),
    )

    def __init__(
        self,
        clusters: list[Any],
        endpoint: Endpoint,
        device: Device,
        **kwargs,
    ) -> None:
        """Initialize the lock."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._doorlock_cluster = self.get_cluster(CLUSTER_DOORLOCK)
        self._state: str | None = VALUE_TO_STATE.get(
            self._doorlock_cluster.get("lock_state"), None
        )

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.subscribe_cluster_attribute_updates(
            CLUSTER_DOORLOCK,
            self.handle_cluster_attribute_updated,
        )
        self.subscribe_cluster_commands(
            CLUSTER_DOORLOCK,
            self.handle_cluster_command,
        )

    @property
    def state(self) -> dict[str, Any]:
        """Get the state of the lock."""
        response = super().state
        response["is_locked"] = self.is_locked
        return response

    @property
    def is_locked(self) -> bool:
        """Return true if entity is locked."""
        if self._state is None:
            return False
        return self._state == STATE_LOCKED

    async def async_lock(self) -> None:
        """Lock the lock."""
        result = await self._doorlock_cluster.lock_door()
        if result[0] is not Status.SUCCESS:
            self.error("Error with lock_door: %s", result)
            return

        self._state = STATE_LOCKED
        self.maybe_emit_state_changed_event()

    async def async_unlock(self) -> None:
        """Unlock the lock."""
        result = await self._doorlock_cluster.unlock_door()
        if result[0] is not Status.SUCCESS:
            self.error("Error with unlock_door: %s", result)
            return

        self._state = STATE_UNLOCKED
        self.maybe_emit_state_changed_event()

    async def async_set_lock_user_code(self, code_slot: int, user_code: str) -> None:
        """Set the user_code to index X on the lock."""
        await self._doorlock_cluster.set_pin_code(
            code_slot - 1,  # start code slots at 1, Zigbee internals use 0
            DoorLockCluster.UserStatus.Enabled,
            DoorLockCluster.UserType.Unrestricted,
            user_code,
        )
        self.debug("User code at slot %s set", code_slot)

    async def async_enable_lock_user_code(self, code_slot: int) -> None:
        """Enable user_code at index X on the lock."""
        await self._doorlock_cluster.set_user_status(
            code_slot - 1, DoorLockCluster.UserStatus.Enabled
        )
        self.debug("User code at slot %s enabled", code_slot)

    async def async_disable_lock_user_code(self, code_slot: int) -> None:
        """Disable user_code at index X on the lock."""
        await self._doorlock_cluster.set_user_status(
            code_slot - 1, DoorLockCluster.UserStatus.Disabled
        )
        self.debug("User code at slot %s disabled", code_slot)

    async def async_clear_lock_user_code(self, code_slot: int) -> None:
        """Clear the user_code at index X on the lock."""
        await self._doorlock_cluster.clear_pin_code(code_slot - 1)
        self.debug("User code at slot %s cleared", code_slot)

    def handle_cluster_attribute_updated(
        self, event: ClusterAttributeUpdatedEvent
    ) -> None:
        """Handle state updates from the lock cluster."""
        if event.attribute_id != DoorLockCluster.AttributeDefs.lock_state.id:
            return
        self._state = VALUE_TO_STATE.get(event.attribute_value, self._state)
        self.maybe_emit_state_changed_event()

    def handle_cluster_command(self, event: ClusterCommandEvent) -> None:
        """Handle inbound door lock commands."""
        command = self._doorlock_cluster.client_commands.get(event.command_id)
        if command is None:
            return

        command_name = command.name
        if (
            command_name
            != DoorLockCluster.ClientCommandDefs.operation_event_notification.name
        ):
            return

        if len(event.args) < 3:
            return

        source = event.args[0]
        operation = event.args[1]
        code_slot = event.args[2]

        self.endpoint.emit_zha_event(
            {
                UNIQUE_ID: self._doorlock_cluster.unique_id,
                CLUSTER_ID: self._doorlock_cluster.cluster_id,
                COMMAND: command_name,
                ARGS: {
                    "source": source.name if hasattr(source, "name") else str(source),
                    "operation": (
                        operation.name if hasattr(operation, "name") else str(operation)
                    ),
                    "code_slot": int(code_slot) + 1,
                },
                PARAMS: {},
            }
        )

    async def async_update(self) -> None:
        """Attempt to retrieve latest lock state from the device."""
        self.debug("polling current state")
        value = await self.get_cluster_attribute_value(
            self._doorlock_cluster,
            DoorLockCluster.AttributeDefs.lock_state.name,
            from_cache=False,
        )
        if value is not None:
            self._state = VALUE_TO_STATE.get(value, self._state)
        self.maybe_emit_state_changed_event()

    def restore_external_state_attributes(
        self,
        *,
        state: Literal["locked", "unlocked"] | None,
    ) -> None:
        """Restore extra state attributes that are stored outside of the ZCL cache."""
        self._state = state
