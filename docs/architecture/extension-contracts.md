# Extension Contracts and Invariants

This document defines behavior that must remain stable across large refactors.

## Hard Compatibility Contracts

## 1) Entity `unique_id` Stability

- Refactors must not alter generated identity for existing entities.
- Changes to identity composition require explicit migration strategy and diagnostics impact assessment.

## 2) Event Payload/Schema Stability

- Event names, payload field names, and semantic meaning must remain stable.
- If event internals must change, preserve outward schema and backward-compatible behavior.

## 3) Registry/Discovery Behavior Parity

- Entity registry matching and arbitration outcomes must remain equivalent for existing devices.
- Cluster-handler claiming semantics must remain functionally unchanged.

## Registry and Discovery Contracts

Primary files:

- `zha/application/discovery.py`
- `zha/application/platforms/__init__.py`
- `zha/zigbee/cluster_handlers/registries.py`

Invariants:

- Discovery imports that exist for registration side effects must still execute.
- Registry keys and predicates must preserve matching intent for existing platforms/devices.
- Priority/arbitration behavior should remain deterministic.

## Quirk and Override Contracts

Primary files:

- `zha/application/gateway.py`
- `zha/zigbee/cluster_handlers/manufacturerspecific.py`
- `zha/decorators.py`

Invariants:

- Quirk-provided metadata and overrides continue to apply with the same precedence.
- Manufacturer-specific handler registration remains discoverable and loadable.

## Async and Task Lifecycle Contracts

Primary files:

- `zha/async_.py`
- `zha/application/helpers.py`

Invariants:

- Background tasks are tracked and cancellable.
- Shutdown cleanup remains comprehensive.
- Concurrency bounds and debounce behavior remain effective.

## Contract-Aware Change Checklist

Before merge, confirm:

1. Unique IDs are unchanged for representative diagnostics fixtures.
2. Event payload shapes are unchanged in impacted code paths.
3. Discovery produces the same entity classes for representative devices.
4. Quirk-driven behavior still wins where expected.
5. Async cancellation/shutdown leaves no orphan background tasks.
