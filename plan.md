# ClusterHandler Removal Refactor Plan (`plan.md` Content)

## Summary
This plan removes all `ClusterHandler`/`ClientClusterHandler`/`ZDOClusterHandler` usage and moves cluster lifecycle, reporting/init configuration, and event handling to `PlatformEntity`, `Endpoint`, and `Device`, while preserving the hard compatibility contracts.

This is an **atomic PR** with internal migration phases.

Current mode constraint:
`plan.md` cannot be written yet in Plan Mode. On first implementation step, write this content to `plan.md` and persist the scaffolding artifacts listed below.

## Locked Decisions
1. Delivery strategy: **Atomic PR**.
2. `_cluster_handler_match` becomes `_cluster_match` and matches by **cluster names**.
3. `_cluster_match` is **direction-aware** with separate server/input and client/output name sets.
4. Event compatibility: keep exact legacy value/content for `cluster_handler_unique_id` in `zha_channel_*`.
5. Event compatibility: keep exact legacy `unique_id` value/content in `zha_event` payloads.
6. Entityless cluster command/event logic lives in **`Device`/`Endpoint`**.
7. Diagnostics schema migration is allowed; replace `info_object.cluster_handlers` with a new `info_object.clusters`.

## Precomputed Scaffolding (Do Not Recompute)
Use these planning artifacts as frozen references and copy them into tracked repo location (suggested: `docs/refactor-data/ch_to_entity/`).

1. `/tmp/entity_inventory_runtime.json`
   Purpose: full entity catalog (258 classes), registered cluster IDs, current match predicates.
   SHA256: `08e3a0b7eee502b6a87b79946b71107320813d36bd7416fa9a6477feacdac560`
2. `/tmp/cluster_name_to_id_map.json`
   Purpose: canonical cluster-name to cluster-id map (49 names).
   SHA256: `0ebe9909ef350c5d6e94ecfe9c076676859ddfa0272783ff3066a923dacc95a0`
3. `/tmp/cluster_registry_map.json`
   Purpose: current cluster handler registry map (119 entries), direction, feature key, bindable, cluster_handler_only.
   SHA256: `0913a35fc318c8671ccf920e24f99c91b18ed7990baf3f4278ad65863e05dbbe`
4. `/tmp/cluster_bind_policy.json`
   Purpose: current per-cluster default bind policy derived from handler `BIND`.
   SHA256: `d19a68a1e803e5cbdfdb015eea9b74d1470cadf39759820180da3d705058cfb8`
5. `/tmp/cluster_handler_class_inventory.json`
   Purpose: current handler class inventory (REPORT_CONFIG, ZCL_INIT_ATTRS, dynamic rules, overrides).
   SHA256: `b004b0588c4ff99cc8c342645e6c8a4a031adf928ab815898bbf5362273e62d5`
6. `/tmp/cluster_attribute_matrix.json`
   Purpose: per-cluster aggregated reporting/init attribute matrix, including feature variants.
   SHA256: `a48a7869470c5b7b08bd9f2978fe06fd491895793175d2de1b2b3620670384f5`
7. `/tmp/entity_attribute_requirements.json`
   Purpose: per-entity resolved cluster requirements with derived report/init requirements.
   SHA256: `dab59268769f08cb133df3118e22a816a22e328a6f5c99171becd36c6a9550f8`
8. `/tmp/entity_handler_api_usage.txt`
   Purpose: exact entity-to-handler API/property usage map to port to direct cluster APIs.
   SHA256: `5c475f95f44f0015b513b9752465833132e63470b6c23b433826835f4e8c5f9d`
9. `/tmp/cluster_event_behavior_inventory.json`
   Purpose: event/cache behaviors currently emitted/mutated in handler methods.
   SHA256: `b60c5bc0d9db7e1e43c2564f3551cb46989f13049cd14220605774e58c13f308`
10. `/tmp/event_payload_contract.json`
    Purpose: frozen public event field contracts.
    SHA256: `1618319616f4a1be273307a5c689920c10cf1618b762c805482e90e150c64067`
11. `/tmp/test_refactor_impact_manifest.txt`
    Purpose: tests coupled to handler internals/contracts.
    SHA256: `68412920b7ee1ddb91a7d2d9aff54b47c804d108cb59b94698f664a7728346f1`

Also persist `/tmp/plan_scaffolding_manifest.json` as the index manifest.

## Canonical Cluster Name Map (for `_cluster_match`)
Use this exact mapping when joining entity match names to cluster IDs:

```text
accelerometer: 0xfc02
analog_input: 0x000c
analog_output: 0x000d
basic: 0x0000
binary_input: 0x000f
binary_output: 0x0010
carbon_dioxide_concentration: 0x040d
carbon_monoxide_concentration: 0x040c
cluster_handler_0x042e: 0x042e
cluster_handler_0xfc45: 0xfc45
device_temperature: 0x0002
diagnostic: 0x0b05
door_lock: 0x0101
electrical_conductivity: 0x040a
electrical_measurement: 0x0b04
fan: 0x0202
flow: 0x0404
formaldehyde_concentration: 0x042b
humidity: 0x0405
ias_ace: 0x0501
ias_wd: 0x0502
ias_zone: 0x0500
identify: 0x0003
ikea_airpurifier: 0xfc7d
illuminance: 0x0400
inovelli_vzm31sn_cluster: 0xfc31
leaf_wetness: 0x0407
level: 0x0008
light_color: 0x0300
occupancy: 0x0406
on_off: 0x0006
opple_cluster: 0xfcc0
ota: 0x0019
philips_occupancy: 0x0406
pm25: 0x042a
power: 0x0001
pressure: 0x0403
shade: 0x0100
sinope_manufacturer_specific: 0xff01
smartenergy_metering: 0x0702
soil_moisture: 0x0408
sonoff_manufacturer: 0xfc11
temperature: 0x0402
thermostat: 0x0201
thermostat_ui: 0x0204
tuya_manufacturer: 0xef00
voc_level: 0x042e
wind_speed: 0x040b
window_covering: 0x0102
```

Fallback rename required:
`cluster_handler_0x042e -> cluster_0x042e`
`cluster_handler_0xfc45 -> cluster_0xfc45`

## Implementation Plan

## Phase 0: Persist Plan + Scaffolding
1. Create `plan.md` at repo root with this full content.
2. Copy all `/tmp` scaffolding artifacts into `docs/refactor-data/ch_to_entity/`.
3. Add a short `README.md` in `docs/refactor-data/ch_to_entity/` with artifact purpose and SHA checks.
4. Add a CI/non-CI check script that verifies artifact checksums before implementation begins.

## Phase 1: Contract Guard Tests (Before Structural Change)
1. Add/extend tests in `tests/test_discover.py` to freeze discovery parity and unique IDs.
2. Add/extend tests in `tests/test_device.py` and `tests/test_cluster_handlers.py` replacement tests to freeze:
   - `zha_channel_bind` payload fields and exact values.
   - `zha_channel_configure_reporting` payload fields and exact values.
   - `zha_event` payload keys and exact `unique_id` values.
3. Add a diagnostics parity fixture comparison test for representative devices from `tests/data/devices/`.

## Phase 2: Introduce Cluster-Native Discovery Contracts
1. In `zha/zha/application/platforms/__init__.py`:
   - Replace `ClusterHandlerMatch` with `ClusterMatch`.
   - Replace `_cluster_handler_match` with `_cluster_match`.
   - Define `_cluster_match` fields as direction-aware:
     - `in_clusters`
     - `out_clusters`
     - `optional_in_clusters`
     - all existing filters and feature priority fields retained.
2. Update all 258 entity class declarations across platform files to `_cluster_match`.
3. In `zha/zha/zigbee/endpoint.py`, add cluster-name indexes:
   - `in_clusters_by_name`
   - `out_clusters_by_name`
   - name resolver: `cluster.ep_attribute` else `cluster_0xNNNN`.
4. In `zha/zha/application/discovery.py`, switch matching from handler-name sets to cluster-name sets with existing arbitration semantics unchanged.
5. Preserve deterministic tie-breaking and platform override behavior exactly.

## Phase 3: PlatformEntity Cluster-Native Construction
1. In `zha/zha/application/platforms/__init__.py`, change `PlatformEntity` construction from `cluster_handlers` to direct `clusters`.
2. Keep deterministic cluster ordering for unique ID composition to maintain legacy IDs.
3. Replace entity internals:
   - `self.cluster_handlers` -> `self.clusters`.
4. Convert `BaseEntityInfo`:
   - remove `cluster_handlers`
   - add `clusters` in the new schema.
5. Update all platform modules under `zha/zha/application/platforms/` to direct cluster access.

## Phase 4: Move REPORT_CONFIG and ZCL_INIT_ATTRS to Entity Classes
1. Define entity-owned config schema in `zha/zha/application/platforms/__init__.py`:
   - `REPORT_CONFIG` keyed by cluster name.
   - `ZCL_INIT_ATTRS` keyed by cluster name with cache flags.
2. Populate all entity definitions using `/tmp/entity_attribute_requirements.json`.
3. Include multi-cluster entities explicitly (10 classes identified).
4. Include client-cluster entities explicitly (5 classes identified).
5. Stop all mutation of cluster handler class/instance config paths.

## Phase 5: Reporting/Init Aggregation and Runtime Application
1. Implement aggregation in `zha/zha/zigbee/device.py` and `zha/zha/zigbee/endpoint.py`.
2. Aggregation rules:
   - Reporting conflict: choose most aggressive (`min_interval=min`, `max_interval=min`, `change=min`) per cluster+attribute.
   - Quirks v2 direct reporting overrides aggregated entity reporting for same cluster+attribute.
   - Init cache conflict: `False` wins (no cache).
3. Preserve chunking and request behavior equivalent to legacy constants.
4. Preserve event emission structure for bind/configure-reporting with exact legacy payload values.

## Phase 6: Quirks v2 Metadata Migration
1. In `zha/zha/application/discovery.py`, stop mutating handler instance fields.
2. Map quirks metadata directly into entity-owned report/init requirements.
3. Preserve current claiming semantics:
   - report config found -> claim + bind.
   - init-only found -> claim, no bind (legacy behavior).
4. Preserve precedence:
   - direct quirks config > entity aggregated config.

## Phase 7: Port Special Handler Behaviors to Device/Endpoint/Entities
Port all custom logic currently in handler overrides.

Required ports:
1. `LevelControlClusterHandler` synthesized level-change behavior for light/cover.
2. `OnOffClientClusterHandler` command-driven cache updates and timed-off handling.
3. `OtaClientClusterHandler` query-next-image cache update behavior.
4. `IdentifyClusterHandler` trigger-effect event emission.
5. `IasAceClientClusterHandler` arm/disarm/panic state machine and panel status.
6. `IASZoneClusterHandler` status change handling and enroll/configure flow.
7. `IasWdClusterHandler` warning/squawk bit-pack helpers.
8. `DoorLockClusterHandler` operation event mapping and user-code helpers.
9. `SmartThingsAccelerationClusterHandler` extra attribute_updated zha_event.
10. `IkeaRemoteClientClusterHandler` and `IkeaSymfoniskRemoteClientClusterHandler` no-op command suppression behavior.
11. `InovelliNotificationClientClusterHandler` no-op behavior.
12. `LightLinkClusterHandler` configure behavior (BIND=False equivalent).

Target files:
`zha/zha/zigbee/device.py`
`zha/zha/zigbee/endpoint.py`
`zha/zha/application/platforms/light/__init__.py`
`zha/zha/application/platforms/cover/__init__.py`
`zha/zha/application/platforms/alarm_control_panel/__init__.py`
`zha/zha/application/platforms/siren.py`
`zha/zha/application/platforms/lock/__init__.py`
`zha/zha/application/platforms/update.py`
`zha/zha/application/platforms/binary_sensor/__init__.py`

## Phase 8: Replace Registry Side-Effects and Policies
1. Remove `CLUSTER_HANDLER_REGISTRY` and `CLIENT_CLUSTER_HANDLER_REGISTRY` usage.
2. Replace `CLUSTER_HANDLER_ONLY_CLUSTERS` handling with cluster-native entityless policies in endpoint/device lifecycle.
3. Replace `BINDABLE_CLUSTERS` in `zha/zha/application/helpers.py` with cluster-id policy table.
4. Preserve current bindable IDs:
`0x0006, 0x0008, 0x000d, 0x0102, 0x0300`
5. Preserve current entityless-but-configure-required IDs:
`0x0000, 0x0502, 0x1000, 0xef00, 0xfc00, 0xfc06, 0xfc11, 0xfc40, 0xfc7d, 0xfc7f, 0xfc80, 0xfcc0, 0xfd00, 0xff01`

## Phase 9: Merge ZDOHandler into Device
1. Move ZDO listener registration and lifecycle state directly into `zha/zha/zigbee/device.py`.
2. Remove `ZDOClusterHandler` class usage and creation.
3. Remove handler-style status plumbing and replace with `Device`-native status fields/methods.
4. Update tests that reference `zdo_cluster_handler`.

## Phase 10: Remove ClusterHandler Framework
1. Remove `zha/zha/zigbee/cluster_handlers/` package implementation and registries after parity is reached.
2. Remove imports across:
`zha/zha/application/discovery.py`
`zha/zha/zigbee/endpoint.py`
`zha/zha/zigbee/device.py`
all platform files under `zha/zha/application/platforms/`.
3. Replace `tests/test_cluster_handlers.py` with cluster-native runtime tests covering same contracts.

## Phase 11: Diagnostics Schema and Tooling
1. In `zha/zha/zigbee/device.py`:
   - bump diagnostics version from `1` to `2`.
   - migrate entity info object serialization from `cluster_handlers` to `clusters`.
2. In `tools/import_diagnostics.py`, support both v1 and v2 schemas.
3. Regenerate diagnostics fixtures with `tools/regenerate_diagnostics.py`.
4. Update assertions in diagnostics-related tests.

## Phase 12: Verification and Exit Criteria
Run in this order:

1. Contract gate:
```sh
pytest -n auto tests/test_discover.py tests/test_event.py tests/test_device.py
```

2. Focused platform suites:
```sh
pytest -n auto tests/test_sensor.py tests/test_light.py tests/test_climate.py tests/test_switch.py tests/test_number.py tests/test_select.py tests/test_cover.py tests/test_binary_sensor.py tests/test_alarm_control_panel.py tests/test_lock.py tests/test_fan.py tests/test_update.py tests/test_button.py
```

3. Diagnostics regeneration and validation:
```sh
python -m tools.regenerate_diagnostics
pytest -n auto tests/test_discover.py
```

4. Full suite:
```sh
pytest -n auto tests
```

5. Lint/quality:
```sh
pre-commit run --all-files
```

Exit criteria:
1. No remaining imports of `zha.zigbee.cluster_handlers`.
2. Discovery parity holds for representative fixture devices.
3. Entity `unique_id` parity holds.
4. `zha_channel_*` payload schema and values remain unchanged.
5. `zha_event` payload keys and `unique_id` values remain unchanged.
6. Diagnostics v2 schema stable and tooling-compatible.

## Important Public Interfaces/Types to Change
1. `zha/zha/application/platforms/__init__.py`
   - `ClusterHandlerMatch` -> `ClusterMatch`
   - `PlatformEntity._cluster_handler_match` -> `PlatformEntity._cluster_match`
   - `PlatformEntity.__init__(clusters=...)`
   - entity-owned `REPORT_CONFIG` and `ZCL_INIT_ATTRS` schema keyed by cluster name
   - `BaseEntityInfo.cluster_handlers` -> `BaseEntityInfo.clusters`
2. `zha/zha/zigbee/endpoint.py`
   - cluster indexes by name/direction
   - cluster claiming and lifecycle execution without handlers
3. `zha/zha/zigbee/device.py`
   - ZDO lifecycle merged into `Device`
   - legacy event unique-id generators preserved
   - reporting/init aggregation orchestration
4. Event payload contracts remain unchanged in shape and semantics.

## Test Scenarios That Must Exist
1. Discovery: same entity class winners per endpoint as before.
2. Unique ID: no change for existing fixtures.
3. `_cluster_match`: in/out cluster name matching works with profile/model/manufacturer filters and feature priority.
4. Fallback name migration: `cluster_handler_0xNNNN` -> `cluster_0xNNNN`.
5. Reporting merge: entity-entity conflicts resolve to most aggressive config.
6. Reporting merge: quirks direct config overrides aggregated entity config.
7. Init merge: cache conflict resolves to uncached (`False`).
8. Quirks init-only path: cluster claimed but not bound.
9. `zha_channel_bind`: field names and value formats unchanged.
10. `zha_channel_configure_reporting`: field names, nested attributes payload, and statuses unchanged.
11. `zha_event`: payload keys and legacy `unique_id` unchanged.
12. IAS ACE state transitions and panic/fire/emergency behaviors unchanged.
13. IAS Zone enroll and status notification behavior unchanged.
14. Door lock operation event and user-code operations unchanged.
15. Cover remote up/down events unchanged.
16. SmartThings acceleration extra attribute_updated event unchanged.
17. Group light behavior that previously inspected color handler execute-if-off capability remains equivalent.
18. Diagnostics v2 generated data stable and import tool compatibility for v1 data maintained.

## Assumptions and Defaults
1. Full ClusterHandler removal is required; no compatibility adapter layer between entities and clusters.
2. Event schema compatibility is strict and includes exact legacy ID value formats.
3. Diagnostics migration to v2 is allowed and expected.
4. Atomic PR delivery is required; internal phase checkpoints are for execution discipline only.
5. Static scaffolding artifacts listed above are the canonical migration reference and must be persisted in-repo before code mutation starts.
