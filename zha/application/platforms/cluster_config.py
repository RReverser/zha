"""Entity-centric cluster configuration primitives and merge logic."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ClusterTarget:
    """Unique target cluster descriptor."""

    endpoint_id: int
    cluster_id: int
    is_client: bool


@dataclass(frozen=True, slots=True)
class ReportingConfig:
    """Per-attribute reporting configuration."""

    attribute: str
    config: tuple[int, int, int | float]


@dataclass(frozen=True, slots=True)
class ClusterConfigContribution:
    """Configuration contribution from one entity or quirk metadata."""

    target: ClusterTarget
    source: str
    order: int
    feature_priority: int
    explicit_quirk: bool = False
    bind: bool | None = None
    reporting: tuple[ReportingConfig, ...] = ()
    init_attrs: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MergedClusterConfig:
    """Merged cluster configuration for one target cluster."""

    bind: bool | None
    reporting: tuple[ReportingConfig, ...]
    init_attrs: dict[str, bool]


def _is_more_demanding(current: ReportingConfig, candidate: ReportingConfig) -> bool:
    """Return True if candidate is a stricter reporting config than current."""

    cur_min, cur_max, cur_change = current.config
    cand_min, cand_max, cand_change = candidate.config

    # Smaller intervals and smaller reportable change are more demanding.
    if cand_min != cur_min:
        return cand_min < cur_min
    if cand_max != cur_max:
        return cand_max < cur_max

    try:
        if cand_change != cur_change:
            return float(cand_change) < float(cur_change)
    except (TypeError, ValueError):
        return False

    return False


class ClusterConfigMerger:
    """Merge cluster configuration contributions from entities and quirks."""

    def __init__(self) -> None:
        self._contributions: defaultdict[
            ClusterTarget, list[ClusterConfigContribution]
        ] = defaultdict(list)

    def reset(self) -> None:
        """Clear all accumulated contributions."""
        self._contributions.clear()

    def add(self, contribution: ClusterConfigContribution) -> None:
        """Add a contribution."""
        self._contributions[contribution.target].append(contribution)

    def merge(self) -> dict[ClusterTarget, MergedClusterConfig]:
        """Merge all contributions by target cluster.

        Merge algorithm order:
        1. Primary contribution baseline by highest feature priority.
        2. Explicit quirk override.
        3. Most demanding merge for remaining conflicts.
        4. First-match tie breaker.
        """
        merged: dict[ClusterTarget, MergedClusterConfig] = {}

        for target, contributions in self._contributions.items():
            if not contributions:
                continue

            ordered = sorted(contributions, key=lambda conf: conf.order)

            non_quirk = [conf for conf in ordered if not conf.explicit_quirk]
            if non_quirk:
                highest_priority = max(conf.feature_priority for conf in non_quirk)
                primary = next(
                    conf
                    for conf in non_quirk
                    if conf.feature_priority == highest_priority
                )
            else:
                primary = ordered[0]

            bind = self._merge_bind(primary, ordered)
            reporting = self._merge_reporting(primary, ordered)
            init_attrs = self._merge_init_attrs(primary, ordered)

            merged[target] = MergedClusterConfig(
                bind=bind,
                reporting=tuple(reporting[attr] for attr in sorted(reporting)),
                init_attrs=init_attrs,
            )

        return merged

    def _merge_bind(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> bool | None:
        """Merge bind config for a cluster."""
        bind_candidates = [conf for conf in ordered if conf.bind is not None]
        if not bind_candidates:
            return None

        selected: bool | None = (
            primary.bind if primary.bind is not None else bind_candidates[0].bind
        )

        quirk_candidates = [
            conf
            for conf in bind_candidates
            if conf.explicit_quirk and conf.bind is not None
        ]
        if quirk_candidates:
            return quirk_candidates[-1].bind

        if any(conf.bind for conf in bind_candidates):
            return True

        return selected

    def _merge_reporting(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> dict[str, ReportingConfig]:
        """Merge reporting config for a cluster by attribute."""
        by_attr: defaultdict[str, list[tuple[int, ReportingConfig, bool]]] = (
            defaultdict(list)
        )

        for conf in ordered:
            for report in conf.reporting:
                by_attr[report.attribute].append(
                    (conf.order, report, conf.explicit_quirk)
                )

        merged: dict[str, ReportingConfig] = {}
        primary_by_attr = {rep.attribute: rep for rep in primary.reporting}

        for attr, candidates in by_attr.items():
            candidates.sort(key=lambda item: item[0])
            selected = primary_by_attr.get(attr, candidates[0][1])

            quirk_override = [rep for _o, rep, is_quirk in candidates if is_quirk]
            if quirk_override:
                merged[attr] = quirk_override[-1]
                continue

            most_demanding = selected
            for _order, candidate, _is_quirk in candidates:
                if _is_more_demanding(most_demanding, candidate):
                    most_demanding = candidate

            merged[attr] = most_demanding

        return merged

    def _merge_init_attrs(
        self,
        primary: ClusterConfigContribution,
        ordered: list[ClusterConfigContribution],
    ) -> dict[str, bool]:
        """Merge initialization attributes by attribute name."""
        attr_values: defaultdict[str, list[tuple[int, bool, bool]]] = defaultdict(list)

        for conf in ordered:
            for attr_name, from_cache in conf.init_attrs.items():
                attr_values[attr_name].append(
                    (conf.order, from_cache, conf.explicit_quirk)
                )

        merged: dict[str, bool] = {}

        for attr, candidates in attr_values.items():
            candidates.sort(key=lambda item: item[0])

            selected = (
                primary.init_attrs.get(attr)
                if attr in primary.init_attrs
                else candidates[0][1]
            )

            quirk_override = [
                from_cache for _o, from_cache, is_quirk in candidates if is_quirk
            ]
            if quirk_override:
                merged[attr] = quirk_override[-1]
                continue

            # False means "uncached read required" and is more demanding.
            if any(from_cache is False for _o, from_cache, _q in candidates):
                merged[attr] = False
            else:
                merged[attr] = selected

        return merged


def cluster_target_from_handler(cluster_handler: Any) -> ClusterTarget:
    """Build a target descriptor for a cluster handler instance."""

    return ClusterTarget(
        endpoint_id=cluster_handler.cluster.endpoint.endpoint_id,
        cluster_id=cluster_handler.cluster.cluster_id,
        is_client=cluster_handler.cluster.is_client,
    )
