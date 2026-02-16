#!/usr/bin/env python3
"""Verify ClusterHandler-removal scaffolding artifact checksums."""

from __future__ import annotations

import hashlib
from pathlib import Path
import sys

ARTIFACT_DIR = Path("docs/refactor-data/ch_to_entity")
EXPECTED_SHA256 = {
    "entity_inventory_runtime.json": "08e3a0b7eee502b6a87b79946b71107320813d36bd7416fa9a6477feacdac560",
    "cluster_name_to_id_map.json": "0ebe9909ef350c5d6e94ecfe9c076676859ddfa0272783ff3066a923dacc95a0",
    "cluster_registry_map.json": "0913a35fc318c8671ccf920e24f99c91b18ed7990baf3f4278ad65863e05dbbe",
    "cluster_bind_policy.json": "d19a68a1e803e5cbdfdb015eea9b74d1470cadf39759820180da3d705058cfb8",
    "cluster_handler_class_inventory.json": "b004b0588c4ff99cc8c342645e6c8a4a031adf928ab815898bbf5362273e62d5",
    "cluster_attribute_matrix.json": "a48a7869470c5b7b08bd9f2978fe06fd491895793175d2de1b2b3620670384f5",
    "entity_attribute_requirements.json": "dab59268769f08cb133df3118e22a816a22e328a6f5c99171becd36c6a9550f8",
    "entity_handler_api_usage.txt": "5c475f95f44f0015b513b9752465833132e63470b6c23b433826835f4e8c5f9d",
    "cluster_event_behavior_inventory.json": "b60c5bc0d9db7e1e43c2564f3551cb46989f13049cd14220605774e58c13f308",
    "event_payload_contract.json": "1618319616f4a1be273307a5c689920c10cf1618b762c805482e90e150c64067",
    "test_refactor_impact_manifest.txt": "68412920b7ee1ddb91a7d2d9aff54b47c804d108cb59b94698f664a7728346f1",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    missing = []
    mismatches = []

    for filename, expected in EXPECTED_SHA256.items():
        artifact = ARTIFACT_DIR / filename
        if not artifact.exists():
            missing.append(filename)
            continue
        actual = sha256(artifact)
        if actual != expected:
            mismatches.append((filename, expected, actual))

    if missing:
        print("Missing artifacts:")
        for filename in missing:
            print(f"  - {filename}")

    if mismatches:
        print("Checksum mismatches:")
        for filename, expected, actual in mismatches:
            print(f"  - {filename}: expected={expected} actual={actual}")

    if missing or mismatches:
        return 1

    print("All refactor scaffolding artifacts verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
