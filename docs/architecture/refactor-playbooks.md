# Refactor Playbooks

These playbooks are for high-impact, behavior-sensitive changes.

## Playbook 1: Split a Large Module Safely

Target examples:

- `zha/zigbee/device.py`
- `zha/application/platforms/sensor/__init__.py`
- `zha/application/platforms/light/__init__.py`

Execution pattern:

1. Freeze public symbol contracts and import paths.
2. Extract internal helpers first (pure behavior-preserving move).
3. Move cohesive sections to new internal modules.
4. Keep backward-compatible re-exports in original module.
5. Run contract-focused tests, then broad confidence tests.
6. Run pre-commit on all files.

Risk controls:

- Avoid changing call order in lifecycle methods.
- Avoid identity/schema changes during structural move.

## Playbook 2: Change Discovery Matching Logic

Target examples:

- `zha/application/discovery.py`
- `zha/application/platforms/__init__.py`

Execution pattern:

1. Enumerate existing matching criteria and arbitration assumptions.
2. Add guard tests for current expected outcomes.
3. Implement minimal matching/arbitration change.
4. Validate representative diagnostics devices.
5. Run contract-focused tests and diagnostics checks if needed.

Risk controls:

- Preserve deterministic tie-breaking behavior.
- Preserve cluster-handler claiming semantics.

## Playbook 3: Introduce or Modify Cluster Handlers

Target examples:

- `zha/zigbee/cluster_handlers/__init__.py`
- `zha/zigbee/cluster_handlers/registries.py`

Execution pattern:

1. Define registration intent and affected cluster IDs.
2. Confirm handler selection path from endpoint wiring.
3. Add/adjust handler behavior with focused tests.
4. Verify event payload stability for entity listeners.
5. Run contract-focused tests, then broad confidence tests.

Risk controls:

- Do not break binding/reporting flows for existing handlers.
- Keep attribute event normalization behavior consistent.

## Playbook 4: Evolve Event Internals Without Breaking Schema

Target examples:

- `zha/event.py`
- entity platform modules consuming/producing events

Execution pattern:

1. Document current emitted payload shape.
2. Refactor internal event production/dispatch.
3. Preserve public payload fields and semantics.
4. Verify event-related tests and impacted platform tests.

Risk controls:

- Avoid renaming fields unless compatibility layer is provided.
- Avoid introducing ordering assumptions in async event delivery.

## Playbook 5: Adjust Polling/Availability Concurrency

Target examples:

- `zha/application/helpers.py`
- `zha/async_.py`
- `zha/debounce.py`

Execution pattern:

1. Record existing cadence and concurrency constraints.
2. Introduce change behind minimal, well-scoped logic edits.
3. Verify no task leaks and clean cancellation on shutdown.
4. Run contract-focused tests, then broad confidence tests.

Risk controls:

- Preserve debounce and allow-polling guard semantics.
- Watch for flake-prone timing regressions.
