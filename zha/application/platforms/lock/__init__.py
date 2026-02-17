"""Locks on Zigbee Home Automation networks."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from zigpy.zcl import (
    AttributeReadEvent,
    AttributeReportedEvent,
    AttributeUpdatedEvent,
    AttributeWrittenEvent,
    Cluster,
)
from zigpy.zcl.clusters.closures import DoorLock as DoorLockCluster
from zigpy.zcl.foundation import Status

from zha.application import Platform
from zha.application.helpers import (
    resolve_incoming_cluster_command_name,
    safe_cluster_command,
    safe_read,
)
from zha.application.platforms import ClusterMatch, PlatformEntity, register_entity
from zha.application.platforms.lock.const import (
    STATE_LOCKED,
    STATE_UNLOCKED,
    VALUE_TO_STATE,
)
from zha.zigbee.const import (
    CLUSTER_DOORLOCK,
    REPORT_CONFIG_ATTR,
    REPORT_CONFIG_CONFIG,
    REPORT_CONFIG_IMMEDIATE,
)

if TYPE_CHECKING:
    from zha.zigbee.device import Device
    from zha.zigbee.endpoint import Endpoint


@register_entity(DoorLockCluster.cluster_id)
class DoorLock(PlatformEntity):
    """Representation of a ZHA lock."""

    PLATFORM = Platform.LOCK
    REPORT_CONFIG = {
        DoorLockCluster.ep_attribute: (
            {
                REPORT_CONFIG_ATTR: DoorLockCluster.AttributeDefs.lock_state.name,
                REPORT_CONFIG_CONFIG: REPORT_CONFIG_IMMEDIATE,
            },
        ),
    }
    ZCL_INIT_ATTRS = {}
    _attr_translation_key: str = "door_lock"
    _attr_primary_weight = 5

    _cluster_match = ClusterMatch(in_clusters=frozenset({CLUSTER_DOORLOCK}))

    def __init__(
        self,
        clusters: list[Cluster],
        endpoint: Endpoint,
        device: Device,
        **kwargs,
    ) -> None:
        """Initialize the lock."""
        super().__init__(clusters, endpoint, device, **kwargs)
        self._doorlock_cluster: Cluster = clusters[0]
        self._state: str | None = VALUE_TO_STATE.get(
            self._doorlock_cluster.get(DoorLockCluster.AttributeDefs.lock_state.name),
            None,
        )

    def on_add(self) -> None:
        """Run when entity is added."""
        super().on_add()
        self.register_cluster_event_listeners(
            self._doorlock_cluster,
            (
                AttributeReadEvent.event_type,
                AttributeReportedEvent.event_type,
                AttributeUpdatedEvent.event_type,
                AttributeWrittenEvent.event_type,
            ),
            self.handle_cluster_attribute_updated,
        )
        self.register_cluster_context_listener(self._doorlock_cluster)
        self.endpoint.register_cluster_command_owner(self._doorlock_cluster)
        self._on_remove_callbacks.append(
            lambda: self.endpoint.unregister_cluster_command_owner(
                self._doorlock_cluster
            )
        )

    def cluster_command(
        self,
        cluster: Cluster,
        tsn: int,  # pylint: disable=unused-argument
        command_id: int,
        args: list[Any],
    ) -> None:
        """Handle cluster commands and preserve lock event payload semantics."""
        if cluster is not self._doorlock_cluster:
            return

        command_name = resolve_incoming_cluster_command_name(cluster, command_id)

        if (
            command_name
            == DoorLockCluster.ClientCommandDefs.operation_event_notification.name
        ):
            self.endpoint.emit_cluster_zha_event(
                cluster,
                command_name,
                {
                    "source": args[0].name,
                    "operation": args[1].name,
                    "code_slot": args[2] + 1,
                },
            )
            return

        self.endpoint.emit_cluster_zha_event(cluster, command_name, args or [])

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
        result = await safe_cluster_command(self._doorlock_cluster, "lock_door")
        if result[0] is not Status.SUCCESS:
            self.error("Error with lock_door: %s", result)
            return

        self._state = STATE_LOCKED
        self.maybe_emit_state_changed_event()

    async def async_unlock(self) -> None:
        """Unlock the lock."""
        result = await safe_cluster_command(self._doorlock_cluster, "unlock_door")
        if result[0] is not Status.SUCCESS:
            self.error("Error with unlock_door: %s", result)
            return

        self._state = STATE_UNLOCKED
        self.maybe_emit_state_changed_event()

    async def async_set_lock_user_code(self, code_slot: int, user_code: str) -> None:
        """Set the user_code to index X on the lock."""
        await safe_cluster_command(
            self._doorlock_cluster,
            "set_pin_code",
            code_slot - 1,
            DoorLockCluster.UserStatus.Enabled,
            DoorLockCluster.UserType.Unrestricted,
            user_code,
        )
        self.debug("User code at slot %s set", code_slot)

    async def async_enable_lock_user_code(self, code_slot: int) -> None:
        """Enable user_code at index X on the lock."""
        await safe_cluster_command(
            self._doorlock_cluster,
            "set_user_status",
            code_slot - 1,
            DoorLockCluster.UserStatus.Enabled,
        )
        self.debug("User code at slot %s enabled", code_slot)

    async def async_disable_lock_user_code(self, code_slot: int) -> None:
        """Disable user_code at index X on the lock."""
        await safe_cluster_command(
            self._doorlock_cluster,
            "set_user_status",
            code_slot - 1,
            DoorLockCluster.UserStatus.Disabled,
        )
        self.debug("User code at slot %s disabled", code_slot)

    async def async_clear_lock_user_code(self, code_slot: int) -> None:
        """Clear the user_code at index X on the lock."""
        await safe_cluster_command(
            self._doorlock_cluster, "clear_pin_code", code_slot - 1
        )
        self.debug("User code at slot %s cleared", code_slot)

    async def async_update(self) -> None:
        """Retrieve latest lock state."""
        result = await safe_read(
            self._doorlock_cluster,
            [DoorLockCluster.AttributeDefs.lock_state.name],
            allow_cache=True,
            only_cache=True,
        )
        self._state = VALUE_TO_STATE.get(
            result.get(DoorLockCluster.AttributeDefs.lock_state.name), self._state
        )
        self.maybe_emit_state_changed_event()

    def handle_cluster_attribute_updated(
        self,
        event: AttributeReadEvent
        | AttributeReportedEvent
        | AttributeUpdatedEvent
        | AttributeWrittenEvent,
    ) -> None:
        """Handle state update from cluster."""
        if event.attribute_id != DoorLockCluster.AttributeDefs.lock_state.id:
            return
        self._state = VALUE_TO_STATE.get(event.value, self._state)
        self.maybe_emit_state_changed_event()

    def restore_external_state_attributes(
        self,
        *,
        state: Literal["locked", "unlocked"] | None,
    ) -> None:
        """Restore extra state attributes that are stored outside of the ZCL cache."""
        self._state = state
