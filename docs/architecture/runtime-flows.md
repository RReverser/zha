# Runtime Flows

This document captures control flow and data flow through the major runtime paths.

## 1) Startup and Initialization Flow

Main path:

1. `Gateway.async_from_config` builds runtime state from `ZHAData`.
2. `Gateway._async_initialize` creates/starts zigpy controller.
3. Existing devices and groups are restored.
4. Gateway listeners are attached.
5. Background helpers (`GlobalUpdater`, `DeviceAvailabilityChecker`) begin polling loops.

Primary files:

- `zha/application/gateway.py`
- `zha/application/helpers.py`

Operational notes:

- Startup uses bounded concurrency in selected operations.
- Shutdown must cancel tracked tasks through async utility primitives.

## 2) Device Join and Device Initialization Flow

Main path:

1. zigpy triggers gateway join/initialize callbacks.
2. Gateway resolves or creates the `Device` domain object.
3. `Device.async_configure` configures endpoint/cluster-handler setup.
4. `Device.async_initialize` performs initialization and discovery-driven entity enablement.
5. Pairing and lifecycle events are emitted.

Primary files:

- `zha/application/gateway.py`
- `zha/zigbee/device.py`
- `zha/zigbee/endpoint.py`

Operational notes:

- Device initialization intentionally serializes some per-device work to reduce race conditions.

## 3) Entity Discovery Flow

Main path:

1. Device initialization reaches `_discover_new_entities`.
2. Discovery inspects endpoint cluster handlers and metadata.
3. Registry-matched entity classes are ranked/arbitrated.
4. Winning entities claim needed cluster handlers.
5. Entities are instantiated and registered into runtime collections.

Primary files:

- `zha/zigbee/device.py`
- `zha/application/discovery.py`
- `zha/application/platforms/__init__.py`

Operational notes:

- Import side effects in discovery populate registries and are required behavior.

## 4) Attribute Update to Entity State Flow

Main path:

1. Cluster emits attribute update.
2. `ClusterHandler` receives and normalizes update event.
3. Subscribed entity listeners process the update.
4. Entity computes derived state.
5. Entity emits state-changed event through `EventBase` chain.

Primary files:

- `zha/zigbee/cluster_handlers/__init__.py`
- `zha/application/platforms/*`
- `zha/event.py`

Operational notes:

- Event payload stability is a compatibility contract.

## 5) Group Update Flow

Main path:

1. Group membership changes are observed by gateway listeners.
2. Group entity rebuild or updates are triggered.
3. Group-level subscriptions and aggregate behavior are updated.
4. Debounced operations coalesce rapid membership changes.

Primary files:

- `zha/application/gateway.py`
- `zha/zigbee/group.py`
- `zha/debounce.py`

## 6) Periodic Polling and Availability Flow

Main path:

1. Background loops iterate over devices.
2. Polling/availability checks run with concurrency bounds.
3. Device availability and state events are updated.

Primary files:

- `zha/application/helpers.py`
- `zha/async_.py`

Operational notes:

- Timing behavior is sensitive; regressions often appear as flaky tests.
