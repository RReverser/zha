"""Tests for the quirks device registry."""

from pathlib import Path
from unittest import mock

from zha.quirks import (
    DeviceMatch,
    DeviceRegistry,
    ModelInfo,
    QuirkPriority,
    QuirkRegistryEntry,
    QuirkSource,
)

KEY = ModelInfo("manufacturer", "model")
BUILTIN_FILE = "/builtin/zhaquirks/quirk.py"


def make_entry(
    label: str,
    *,
    priority: int,
    file: str = BUILTIN_FILE,
    applies_to: tuple[ModelInfo, ...] = (KEY,),
) -> QuirkRegistryEntry:
    """Create a registry entry; the unique transform prevents deduplication."""
    return QuirkRegistryEntry(
        device_match=DeviceMatch(applies_to=applies_to),
        zigpy_transforms=(lambda device: device,),
        priority=priority,
        source=QuirkSource(module="module", file=file, line=1, label=label),
    )


def test_register_orders_by_priority(tmp_path: Path) -> None:
    """Entries match custom v2 > built-in v2 > custom v1 > built-in v1."""
    registry = DeviceRegistry()
    registry.purge_custom_quirks(tmp_path)
    custom_file = str(tmp_path / "custom_quirk.py")

    # Register fully inverted: with recency-only matching, the last registered
    # entry would win.
    registry.register(
        make_entry("custom v2 old", priority=QuirkPriority.V2, file=custom_file)
    )
    registry.register(
        make_entry("custom v2 new", priority=QuirkPriority.V2, file=custom_file)
    )
    registry.register(make_entry("built-in v2", priority=QuirkPriority.V2))
    registry.register(
        make_entry("custom v1", priority=QuirkPriority.V1, file=custom_file)
    )
    registry.register(make_entry("built-in v1", priority=QuirkPriority.V1))

    assert [entry.source.label for entry in registry._registry[KEY]] == [
        # Within a tier, later-registered entries still match first.
        "custom v2 new",
        "custom v2 old",
        "built-in v2",
        "custom v1",
        "built-in v1",
    ]

    device = mock.Mock(manufacturer=KEY.manufacturer, model=KEY.model)
    entry = registry.match_entry(device)
    assert entry is not None
    assert entry.source.label == "custom v2 new"


def test_register_orders_wildcard_registry_by_priority(tmp_path: Path) -> None:
    """Wildcard (signature-only) entries are also ordered by priority."""
    registry = DeviceRegistry()
    registry.purge_custom_quirks(tmp_path)
    custom_file = str(tmp_path / "custom_quirk.py")

    registry.register(
        make_entry(
            "custom v1", priority=QuirkPriority.V1, file=custom_file, applies_to=()
        )
    )
    registry.register(
        make_entry("built-in v1", priority=QuirkPriority.V1, applies_to=())
    )

    assert [entry.source.label for entry in registry._wildcard_registry] == [
        "custom v1",
        "built-in v1",
    ]


def test_register_without_custom_quirks_root(tmp_path: Path) -> None:
    """Without a known custom quirks root, no entry gets the custom bump."""
    registry = DeviceRegistry()
    custom_file = str(tmp_path / "custom_quirk.py")

    registry.register(
        make_entry("custom v2", priority=QuirkPriority.V2, file=custom_file)
    )
    registry.register(make_entry("built-in v2", priority=QuirkPriority.V2))

    # Same tier: most recently registered first
    assert [entry.source.label for entry in registry._registry[KEY]] == [
        "built-in v2",
        "custom v2",
    ]


def test_preserve_state_restores_custom_quirks_root(tmp_path: Path) -> None:
    """`preserve_state` snapshots the custom quirks root along with the entries."""
    registry = DeviceRegistry()
    registry.purge_custom_quirks(tmp_path)

    with registry.preserve_state():
        registry.purge_custom_quirks(tmp_path / "elsewhere")
        assert registry._custom_quirks_root == tmp_path / "elsewhere"

    assert registry._custom_quirks_root == tmp_path
