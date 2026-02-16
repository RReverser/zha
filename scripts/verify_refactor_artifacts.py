#!/usr/bin/env python3
"""Verify refactor scaffolding artifact checksums."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ARTIFACT_DIR = Path("docs/refactor-data/ch_to_entity")
MANIFEST_PATH = ARTIFACT_DIR / "plan_scaffolding_manifest.json"


def sha256(path: Path) -> str:
    """Return the SHA256 checksum for a file."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_expected_sha256() -> dict[str, str]:
    """Load expected artifact checksums from the frozen scaffolding manifest."""
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    expected: dict[str, str] = {}

    for entry in manifest:
        filename = Path(entry["path"]).name
        expected[filename] = entry["sha256"]

    return expected


def main() -> int:
    """Verify that all frozen refactor artifacts exist and match expected hashes."""
    if not MANIFEST_PATH.exists():
        sys.stderr.write(f"Missing scaffolding manifest: {MANIFEST_PATH}\n")
        return 1

    expected_sha256 = load_expected_sha256()
    missing = []
    mismatches = []

    for filename, expected in expected_sha256.items():
        artifact = ARTIFACT_DIR / filename
        if not artifact.exists():
            missing.append(filename)
            continue
        actual = sha256(artifact)
        if actual != expected:
            mismatches.append((filename, expected, actual))

    if missing:
        sys.stderr.write("Missing artifacts:\n")
        for filename in missing:
            sys.stderr.write(f"  - {filename}\n")

    if mismatches:
        sys.stderr.write("Checksum mismatches:\n")
        for filename, expected, actual in mismatches:
            sys.stderr.write(f"  - {filename}: expected={expected} actual={actual}\n")

    if missing or mismatches:
        return 1

    sys.stdout.write("All refactor scaffolding artifacts verified.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
