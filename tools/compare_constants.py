#!/usr/bin/env python3
"""Compare ZHA's local copies of HA constants against the canonical `homeassistant` package.

Run this whenever `homeassistant` is bumped to surface drift in unit enums,
device-class enums, and module-level constants that ZHA mirrors.

Two modes:

* Default (check): print the drift and exit 1 if anything is out of sync, 0 if
  fully in sync.
* ``--write``: apply the *safe* fixes in place, then exit 0. Safe fixes are the
  unambiguous, additive ones a human would make mechanically:

  - enum members present in HA but missing from an enum ZHA already mirrors are
    added, and
  - enum-member / module-level-constant value mismatches are corrected to HA's
    value.

  Everything that needs human judgement is *never* written and is only
  reported: ZHA-only members/constants (``extra in ZHA`` / ``not present in
  homeassistant.const``), type mismatches, and entirely new ``UnitOf*`` enums HA
  has that ZHA doesn't mirror at all (ZHA may not need them). Re-run the default
  mode afterwards to see whatever ``--write`` left for manual follow-up.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass, field
from enum import Enum
import importlib
from pathlib import Path
import sys
from typing import Any

import homeassistant.const as ha_const

import zha.units as zha_units

# Explicit (zha, ha) pairs for enums that don't live in `zha.units`.
ENUM_PAIRS: list[tuple[str, str]] = [
    (
        "zha.application.platforms.binary_sensor.device_class.BinarySensorDeviceClass",
        "homeassistant.components.binary_sensor.BinarySensorDeviceClass",
    ),
    (
        "zha.application.platforms.sensor.device_class.SensorDeviceClass",
        "homeassistant.components.sensor.SensorDeviceClass",
    ),
    (
        "zha.application.platforms.sensor.device_class.SensorStateClass",
        "homeassistant.components.sensor.SensorStateClass",
    ),
    (
        "zha.application.platforms.number.device_class.NumberDeviceClass",
        "homeassistant.components.number.NumberDeviceClass",
    ),
    (
        "zha.application.platforms.number.device_class.NumberMode",
        "homeassistant.components.number.NumberMode",
    ),
    # zha.application.Platform is intentionally a Zigbee-only subset of
    # homeassistant.const.Platform — comparison would produce noise.
]

# Constants intentionally defined in ZHA without an HA counterpart.
ZHA_ONLY_CONSTANTS: frozenset[str] = frozenset({"COUNT", "KILOJOULES_PER_KG"})


def import_qualified(qualified_name: str) -> Any:
    """Import a dotted name like 'zha.units.UnitOfTemperature'."""
    module_name, attr = qualified_name.rsplit(".", 1)
    return getattr(importlib.import_module(module_name), attr)


def is_enum_class(obj: Any) -> bool:
    """Return True if obj is an Enum subclass."""
    return isinstance(obj, type) and issubclass(obj, Enum)


@dataclass
class EnumComparison:
    """Structured diff between a ZHA enum and its HA counterpart."""

    label: str
    zha_qualname: str
    # HA member name -> value, present in HA but missing from ZHA (fixable: add).
    missing_in_zha: dict[str, str] = field(default_factory=dict)
    # ZHA member name -> value, present in ZHA but missing from HA (report only).
    extra_in_zha: dict[str, str] = field(default_factory=dict)
    # member name -> (zha value, ha value) (fixable: set ZHA to HA's value).
    value_mismatch: dict[str, tuple[str, str]] = field(default_factory=dict)
    error: str | None = None

    @property
    def has_fixable(self) -> bool:
        """Return True if there is anything ``--write`` can safely apply."""
        return bool(self.missing_in_zha or self.value_mismatch)

    @property
    def report_lines(self) -> list[str]:
        """Return the human-readable difference lines (empty if identical)."""
        if self.error is not None:
            return [f"  failed to import: {self.error}"]
        lines: list[str] = []
        for name in sorted(self.missing_in_zha):
            lines.append(f"  missing in ZHA: {name} = {self.missing_in_zha[name]!r}")
        for name in sorted(self.extra_in_zha):
            lines.append(f"  extra in ZHA:   {name} = {self.extra_in_zha[name]!r}")
        for name in sorted(self.value_mismatch):
            zha_value, ha_value = self.value_mismatch[name]
            lines.append(f"  value mismatch: {name}: ZHA={zha_value!r} HA={ha_value!r}")
        return lines


def diff_enum(
    label: str, zha_qualname: str, zha_enum: type[Enum], ha_enum: type[Enum]
) -> EnumComparison:
    """Return an EnumComparison between two enums."""
    zha_members = {m.name: m.value for m in zha_enum}
    ha_members = {m.name: m.value for m in ha_enum}
    return EnumComparison(
        label=label,
        zha_qualname=zha_qualname,
        missing_in_zha={n: ha_members[n] for n in ha_members if n not in zha_members},
        extra_in_zha={n: zha_members[n] for n in zha_members if n not in ha_members},
        value_mismatch={
            n: (zha_members[n], ha_members[n])
            for n in zha_members.keys() & ha_members.keys()
            if zha_members[n] != ha_members[n]
        },
    )


def compare_units_module() -> tuple[
    list[EnumComparison], list[tuple[str, str, str]], list[tuple[str, list[str]]]
]:
    """Compare zha.units against homeassistant.const in both directions.

    Returns ``(enum_comparisons, const_results, report_only)`` where
    ``const_results`` is ``(name, zha_value, ha_value)`` for module-level string
    constants and ``report_only`` is ``(name, lines)`` for drift that can only be
    fixed by hand (ZHA-only symbols, type mismatches, whole missing enums).
    """
    enum_comparisons: list[EnumComparison] = []
    const_results: list[tuple[str, str, str]] = []
    report_only: list[tuple[str, list[str]]] = []

    for name in sorted(vars(zha_units)):
        if name.startswith("_"):
            continue
        zha_obj = getattr(zha_units, name)

        # Only compare classes/strings defined in zha.units itself
        # (skip re-imports like StrEnum, Final).
        if is_enum_class(zha_obj):
            if zha_obj.__module__ != zha_units.__name__:
                continue
        elif not isinstance(zha_obj, str):
            continue

        if name in ZHA_ONLY_CONSTANTS:
            continue

        if not hasattr(ha_const, name):
            report_only.append((name, ["  not present in homeassistant.const"]))
            continue

        ha_obj = getattr(ha_const, name)

        if is_enum_class(zha_obj) and is_enum_class(ha_obj):
            enum_comparisons.append(
                diff_enum(name, f"zha.units.{name}", zha_obj, ha_obj)
            )
        elif isinstance(zha_obj, str) and isinstance(ha_obj, str):
            const_results.append((name, zha_obj, ha_obj))
        else:
            report_only.append(
                (
                    name,
                    [
                        f"  type mismatch: ZHA={type(zha_obj).__name__} HA={type(ha_obj).__name__}"
                    ],
                )
            )

    # Reverse scan: UnitOf* enums in homeassistant.const that ZHA doesn't have.
    zha_names = {n for n in vars(zha_units) if not n.startswith("_")}
    for name in sorted(vars(ha_const)):
        if not name.startswith("UnitOf"):
            continue
        ha_obj = getattr(ha_const, name)
        if not is_enum_class(ha_obj):
            continue
        if ha_obj.__module__ != ha_const.__name__:
            continue
        if name in zha_names:
            continue
        members = ", ".join(f"{m.name}={m.value!r}" for m in ha_obj)
        report_only.append((name, [f"  not present in zha.units (HA has: {members})"]))

    return enum_comparisons, const_results, report_only


def compare_enum_pairs() -> list[EnumComparison]:
    """Compare each explicit (zha, ha) enum pair from ENUM_PAIRS."""
    results: list[EnumComparison] = []
    for zha_name, ha_name in ENUM_PAIRS:
        label = f"{zha_name.split('.')[-1]} ({zha_name} ↔ {ha_name})"
        try:
            zha_enum = import_qualified(zha_name)
            ha_enum = import_qualified(ha_name)
        except (ImportError, AttributeError) as exc:
            results.append(
                EnumComparison(label=label, zha_qualname=zha_name, error=str(exc))
            )
            continue
        results.append(diff_enum(label, zha_name, zha_enum, ha_enum))
    return results


def _format_str_literal(value: str) -> str:
    """Return a double-quoted literal, keeping unicode readable where possible."""
    if '"' not in value and "\\" not in value:
        return f'"{value}"'
    return repr(value)


class SourceEditor:
    """Apply surgical, AST-located edits to a single source file.

    Edits are collected against one AST snapshot and applied bottom-up so line
    numbers stay valid.
    """

    def __init__(self, path: Path) -> None:
        """Read and parse ``path``."""
        self.path = path
        self.lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        self.tree = ast.parse("".join(self.lines))
        # (start_index, end_index_exclusive, replacement_lines)
        self._edits: list[tuple[int, int, list[str]]] = []

    def _find_class(self, name: str) -> ast.ClassDef:
        for node in self.tree.body:
            if isinstance(node, ast.ClassDef) and node.name == name:
                return node
        raise LookupError(f"class {name!r} not found in {self.path}")

    def _find_assign(self, body: list[ast.stmt], name: str) -> ast.stmt:
        for node in body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets
            ):
                return node
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == name
            ):
                return node
        raise LookupError(f"assignment {name!r} not found in {self.path}")

    def set_value(self, class_name: str | None, name: str, value: str) -> None:
        """Rewrite the RHS of ``name`` (in ``class_name`` or module scope)."""
        body = self._find_class(class_name).body if class_name else self.tree.body
        node = self._find_assign(body, name)
        index = node.lineno - 1
        # Preserve the whole left-hand side (name + any annotation) up to the "=".
        prefix = self.lines[index].split("=", 1)[0]
        self._edits.append(
            (index, node.end_lineno, [f"{prefix}= {_format_str_literal(value)}\n"])
        )

    def add_enum_members(self, class_name: str, members: list[tuple[str, str]]) -> None:
        """Append ``NAME = "value"`` members to the end of an enum class body."""
        cls = self._find_class(class_name)
        member_nodes = [
            n for n in cls.body if isinstance(n, (ast.Assign, ast.AnnAssign))
        ]
        indent = " " * member_nodes[0].col_offset
        # Device-class enums separate members with a blank line (plus a comment or
        # attribute docstring); unit enums list them contiguously. Match whichever
        # style by checking for blank lines within the existing member region.
        region = self.lines[member_nodes[0].lineno - 1 : cls.body[-1].end_lineno]
        spaced = any(not line.strip() for line in region)
        block: list[str] = []
        for member_name, value in members:
            if spaced:
                block.append("\n")
            block.append(f"{indent}{member_name} = {_format_str_literal(value)}\n")
        insert_at = cls.body[
            -1
        ].end_lineno  # 0-based index of the line after the last body line
        self._edits.append((insert_at, insert_at, block))

    def flush(self) -> bool:
        """Apply the collected edits to disk. Return True if anything changed."""
        if not self._edits:
            return False
        lines = list(self.lines)
        for start, end, replacement in sorted(
            self._edits, key=lambda e: e[0], reverse=True
        ):
            lines[start:end] = replacement
        self.path.write_text("".join(lines), encoding="utf-8")
        return True


def _qualname_to_file_and_class(qualname: str) -> tuple[Path, str]:
    """Resolve 'pkg.mod.Class' to the module's source file and the class name."""
    module_name, class_name = qualname.rsplit(".", 1)
    module_file = importlib.import_module(module_name).__file__
    assert module_file is not None
    return Path(module_file), class_name


def apply_fixes(
    enum_comparisons: list[EnumComparison], const_fixes: list[tuple[str, str]]
) -> list[tuple[Path, list[str]]]:
    """Apply the safe fixes in place. Return ``(path, change_descriptions)`` per file."""
    editors: dict[Path, SourceEditor] = {}
    changes: dict[Path, list[str]] = {}

    def editor_for(path: Path) -> SourceEditor:
        if path not in editors:
            editors[path] = SourceEditor(path)
            changes[path] = []
        return editors[path]

    for comparison in enum_comparisons:
        if comparison.error is not None or not comparison.has_fixable:
            continue
        path, class_name = _qualname_to_file_and_class(comparison.zha_qualname)
        editor = editor_for(path)
        for name in sorted(comparison.value_mismatch):
            _zha_value, ha_value = comparison.value_mismatch[name]
            editor.set_value(class_name, name, ha_value)
            changes[path].append(f"{class_name}.{name} value -> {ha_value!r}")
        if comparison.missing_in_zha:
            members = sorted(comparison.missing_in_zha.items())
            editor.add_enum_members(class_name, members)
            changes[path].append(
                f"{class_name}: added {', '.join(name for name, _ in members)}"
            )

    units_path = Path(zha_units.__file__)
    for name, ha_value in const_fixes:
        editor_for(units_path).set_value(None, name, ha_value)
        changes[units_path].append(f"{name} value -> {ha_value!r}")

    written: list[tuple[Path, list[str]]] = []
    for path, editor in editors.items():
        if editor.flush():
            written.append((path, changes[path]))
    return written


def print_block(title: str, entries: list[tuple[str, list[str]]]) -> bool:
    """Print one section from ``(name, lines)`` entries. Return True if any drift."""
    print(title)
    print("=" * len(title))
    drift = False
    in_sync: list[str] = []
    for name, diffs in sorted(entries, key=lambda e: e[0]):
        if not diffs:
            in_sync.append(name)
            continue
        drift = True
        print(f"\n{name}:")
        for line in diffs:
            print(line)
    if in_sync:
        print(f"\nin sync ({len(in_sync)}): {', '.join(in_sync)}")
    print()
    return drift


def main(argv: list[str] | None = None) -> int:
    """Run the comparison and return an exit code (0 = in sync/written, 1 = drift)."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="apply safe fixes (add missing enum members, correct value "
        "mismatches) in place instead of only reporting drift",
    )
    args = parser.parse_args(argv)

    units_enums, const_results, units_report_only = compare_units_module()
    pair_enums = compare_enum_pairs()

    if args.write:
        const_fixes = [
            (name, ha_value)
            for name, zha_value, ha_value in const_results
            if zha_value != ha_value
        ]
        written = apply_fixes(units_enums + pair_enums, const_fixes)
        if written:
            print("Applied safe fixes:")
            for path, descriptions in written:
                print(f"\n{path}:")
                for description in descriptions:
                    print(f"  {description}")
        else:
            print("No safe fixes to apply.")
        print(
            "\nRe-run without --write to see any drift left for manual follow-up "
            "(ZHA-only symbols, type mismatches, whole missing enums)."
        )
        return 0

    units_entries: list[tuple[str, list[str]]] = [
        (c.zha_qualname.split(".")[-1], c.report_lines) for c in units_enums
    ]
    units_entries += [
        (
            name,
            []
            if zha_value == ha_value
            else [f"  value mismatch: ZHA={zha_value!r} HA={ha_value!r}"],
        )
        for name, zha_value, ha_value in const_results
    ]
    units_entries += units_report_only

    pair_entries = [(c.label, c.report_lines) for c in pair_enums]

    drift = False
    drift |= print_block("zha.units vs homeassistant.const", units_entries)
    drift |= print_block("Device classes & Platform enums", pair_entries)

    if drift:
        print("DRIFT detected — ZHA constants are out of sync with homeassistant.")
        return 1
    print("All compared constants match homeassistant.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
