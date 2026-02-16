# Architecture Guide for Coding Agents

This guide is a repository-internal architecture reference for making large, behavior-safe changes in ZHA.

It is written for coding agents and contributors who need to understand module boundaries, runtime flow, and compatibility constraints before refactoring.

## Read Order

1. [System Map](./system-map.md)
2. [Runtime Flows](./runtime-flows.md)
3. [Extension Contracts](./extension-contracts.md)
4. [Refactor Playbooks](./refactor-playbooks.md)
5. [Verification Matrix](./verification-matrix.md)

## Scope

- In scope: architecture and behavior visible in this repository.
- Out of scope: Home Assistant internals outside this repository.

## Non-Negotiable Compatibility Contracts

Any large refactor must preserve:

- Entity `unique_id` stability.
- Event payload/schema stability.
- Registry and discovery behavior parity.

## Fast Orientation

- Application orchestration starts in `zha/application/gateway.py` (`Gateway`, `async_from_config`, `_async_initialize`).
- Device lifecycle and endpoint orchestration are centered in `zha/zigbee/device.py` (`Device`, `async_configure`, `async_initialize`) and `zha/zigbee/endpoint.py`.
- Discovery and entity selection logic are in `zha/application/discovery.py` and registry contracts in `zha/application/platforms/__init__.py`.
- Cluster-handler behavior and registration are in `zha/zigbee/cluster_handlers/`.
- Async task and event scaffolding are in `zha/async_.py` and `zha/event.py`.

## How to Use This Guide During Large Changes

1. Identify the target subsystem in the system map.
2. Trace the affected runtime paths in runtime flows.
3. List impacted contracts from extension contracts.
4. Follow the matching playbook for execution order and risk controls.
5. Run verification in the required sequence from the verification matrix.
