"""Cluster metadata facade for application/discovery layers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from zigpy.zcl.clusters.closures import WindowCovering
from zigpy.zcl.clusters.general import AnalogOutput, Basic, LevelControl, OnOff
from zigpy.zcl.clusters.lighting import Color
from zigpy.zcl.clusters.lightlink import LightLink
from zigpy.zcl.clusters.measurement import OccupancySensing
from zigpy.zcl.clusters.security import IasWd

from zha.application.platforms.cluster_config import get_default_entity_cluster_config
from zha.application.platforms.cluster_names import (
    AQARA_OPPLE_CLUSTER,
    IKEA_AIR_PURIFIER_CLUSTER,
    IKEA_REMOTE_CLUSTER,
    IKEA_SHORTCUT_V1_CLUSTER,
    LEGRAND_CABLE_OUTLET_CLUSTER,
    OSRAM_BUTTON_CLUSTER,
    PHILIPS_CONTACT_CLUSTER,
    PHILLIPS_REMOTE_CLUSTER,
    SINOPE_MANUFACTURER_CLUSTER,
    SONOFF_CLUSTER,
    TUYA_MANUFACTURER_CLUSTER,
)

if TYPE_CHECKING:
    from zha.zigbee.endpoint import Endpoint

BINDABLE_CLUSTERS: frozenset[int] = frozenset(
    {
        AnalogOutput.cluster_id,
        LevelControl.cluster_id,
        OnOff.cluster_id,
        WindowCovering.cluster_id,
        Color.cluster_id,
    }
)
CLUSTER_ONLY_CLUSTERS: frozenset[int] = frozenset(
    {
        Basic.cluster_id,
        LightLink.cluster_id,
        IasWd.cluster_id,
        OSRAM_BUTTON_CLUSTER,
        PHILIPS_CONTACT_CLUSTER,
        PHILLIPS_REMOTE_CLUSTER,
        TUYA_MANUFACTURER_CLUSTER,
        AQARA_OPPLE_CLUSTER,
        IKEA_AIR_PURIFIER_CLUSTER,
        IKEA_REMOTE_CLUSTER,
        IKEA_SHORTCUT_V1_CLUSTER,
        SONOFF_CLUSTER,
        SINOPE_MANUFACTURER_CLUSTER,
        LEGRAND_CABLE_OUTLET_CLUSTER,
    }
)


def _apply_model_specific_default_init_attrs(
    cluster: Any, init_attrs: dict[str, bool]
) -> dict[str, bool]:
    """Apply model-specific init attrs for clusters without custom adapters."""
    if cluster.cluster_id != OccupancySensing.cluster_id:
        return init_attrs

    manufacturer = cluster.endpoint.manufacturer
    model = cluster.endpoint.model
    adjusted = dict(init_attrs)

    if manufacturer in ("Philips", "Signify Netherlands B.V.") and model in (
        "SML001",
        "SML002",
        "SML003",
        "SML004",
    ):
        adjusted["sensitivity"] = True

    if manufacturer == "SONOFF" and model in ("SNZB-06P", "SNZB-03P"):
        adjusted["ultrasonic_o_to_u_delay"] = True
        adjusted["ultrasonic_u_to_o_threshold"] = True

    return adjusted


def _cluster_only_default_configuration(
    cluster_id: int,
    *,
    is_client: bool,
) -> tuple[bool | None, tuple[dict[str, Any], ...], dict[str, bool]] | None:
    """Return fallback defaults for cluster-only clusters.

    Legacy runtime defaulted to bind enabled unless explicitly disabled.
    For cluster-only clusters missing an explicit entity config entry, keep that
    behavior.
    """
    if is_client or cluster_id not in CLUSTER_ONLY_CLUSTERS:
        return None

    bind = cluster_id not in {Basic.cluster_id, LightLink.cluster_id}
    return bind, (), {}


def get_cluster_default_configuration(
    cluster: Any,
    _endpoint: Endpoint,
    *,
    is_client: bool,
) -> tuple[bool | None, tuple[dict[str, Any], ...], dict[str, bool]]:
    """Return default bind/report/init configuration for a cluster."""
    cluster_name = getattr(cluster, "ep_attribute", None) or (
        f"cluster_0x{cluster.cluster_id:04x}"
    )
    default_config = get_default_entity_cluster_config(
        cluster_name,
        is_client=is_client,
    )
    if default_config is None:
        fallback = _cluster_only_default_configuration(
            int(cluster.cluster_id),
            is_client=is_client,
        )
        if fallback is not None:
            return fallback
        return None, (), {}

    reporting: tuple[dict[str, Any], ...] = tuple(
        {
            "attr": report.attribute,
            "config": report.config,
        }
        for report in (default_config.reporting or ())
    )
    init_attrs = dict(default_config.init_attrs or {})

    final_init_attrs = _apply_model_specific_default_init_attrs(
        cluster,
        init_attrs,
    )

    return (
        default_config.bind,
        reporting,
        final_init_attrs,
    )
