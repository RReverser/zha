"""Test cluster-native endpoint lifecycle behavior."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, call

from zigpy.const import SIG_EP_INPUT, SIG_EP_OUTPUT, SIG_EP_PROFILE, SIG_EP_TYPE
import zigpy.exceptions
import zigpy.profiles.zha
from zigpy.typing import UNDEFINED
from zigpy.zcl.clusters.general import LevelControl, OnOff

from tests.common import create_mock_zigpy_device
from zha.application.const import (
    ZHA_CLUSTER_HANDLER_MSG_BIND,
    ZHA_CLUSTER_HANDLER_MSG_CFG_RPT,
)
from zha.application.gateway import Gateway
from zha.zigbee.endpoint import Endpoint


def _make_endpoint(
    zha_gateway: Gateway,
    *,
    in_clusters: list[int],
    out_clusters: list[int] | None = None,
) -> tuple[Endpoint, Any]:
    """Create an endpoint backed by patched test clusters."""
    zigpy_dev = create_mock_zigpy_device(
        zha_gateway,
        {
            1: {
                SIG_EP_INPUT: in_clusters,
                SIG_EP_OUTPUT: out_clusters or [],
                SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.ON_OFF_SWITCH,
                SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
            }
        },
        ieee="00:11:22:33:44:55:66:77",
        manufacturer="test manufacturer",
        model="test model",
    )

    zha_device = MagicMock()
    zha_device.unique_id = "00:11:22:33:44:55:66:77"
    zha_device.nwk = 0xBEEF
    zha_device.skip_configuration = False
    zha_device.emit = MagicMock()

    endpoint = Endpoint.new(zigpy_dev.endpoints[1], zha_device)
    return endpoint, zigpy_dev.endpoints[1]


def test_resolve_cluster_name_fallback() -> None:
    """Unknown clusters fall back to cluster_0xNNNN names."""
    cluster = SimpleNamespace(ep_attribute=None, cluster_id=0xFC45)
    assert Endpoint.resolve_cluster_name(cluster) == "cluster_0xfc45"


def test_set_cluster_report_config_merges_aggressively(zha_gateway: Gateway) -> None:
    """Report config conflicts choose the most aggressive values."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    endpoint.claim_clusters([cluster])

    endpoint.set_cluster_report_config(cluster, {"on_off": (5, 900, 2)}, set())
    endpoint.set_cluster_report_config(cluster, {"on_off": (1, 600, 1)}, set())

    cluster_key = endpoint._cluster_key(cluster)
    assert endpoint._cluster_report_config[cluster_key]["on_off"] == (1, 600, 1)


def test_set_cluster_report_config_direct_override(zha_gateway: Gateway) -> None:
    """Direct reporting config takes precedence over aggregated config."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    endpoint.claim_clusters([cluster])

    endpoint.set_cluster_report_config(cluster, {"on_off": (5, 900, 2)}, set())
    endpoint.set_cluster_report_config(
        cluster,
        {"on_off": (7, 700, 7)},
        {"on_off"},
    )
    endpoint.set_cluster_report_config(cluster, {"on_off": (1, 100, 1)}, set())

    cluster_key = endpoint._cluster_key(cluster)
    assert endpoint._cluster_report_config[cluster_key]["on_off"] == (7, 700, 7)


def test_set_cluster_init_attrs_cache_conflict(zha_gateway: Gateway) -> None:
    """Cache conflicts resolve to uncached reads."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    endpoint.claim_clusters([cluster])

    endpoint.set_cluster_init_attrs(cluster, {"on_off": True})
    endpoint.set_cluster_init_attrs(cluster, {"on_off": False})

    cluster_key = endpoint._cluster_key(cluster)
    assert endpoint._cluster_init_attrs[cluster_key]["on_off"] is False


async def test_endpoint_configure_emits_legacy_bind_and_reporting_events(
    zha_gateway: Gateway,
) -> None:
    """Configure emits legacy bind/configure reporting event payloads."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    endpoint.claim_clusters([cluster])
    endpoint.set_cluster_bind(cluster, True)
    endpoint.set_cluster_report_config(cluster, {"on_off": (0, 900, 1)}, set())

    await endpoint.async_configure()

    assert cluster.bind.await_count == 1
    assert cluster.configure_reporting_multiple.await_count == 1

    bind_events = [
        mock_call.args[1]
        for mock_call in endpoint.device.emit.call_args_list
        if mock_call.args[0] == ZHA_CLUSTER_HANDLER_MSG_BIND
    ]
    assert len(bind_events) == 1
    bind_event = bind_events[0]
    assert bind_event.success is True
    assert bind_event.cluster_handler_unique_id == "00:11:22:33:44:55:66:77:1:0x0006"

    reporting_events = [
        mock_call.args[1]
        for mock_call in endpoint.device.emit.call_args_list
        if mock_call.args[0] == ZHA_CLUSTER_HANDLER_MSG_CFG_RPT
    ]
    assert len(reporting_events) == 1
    reporting_event = reporting_events[0]
    assert (
        reporting_event.cluster_handler_unique_id == "00:11:22:33:44:55:66:77:1:0x0006"
    )
    assert reporting_event.attributes["on_off"]["status"] == "SUCCESS"


async def test_endpoint_configure_bind_error_emits_failed_bind_event(
    zha_gateway: Gateway,
) -> None:
    """Bind errors emit failed bind events while keeping configure flow alive."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    cluster.bind.side_effect = zigpy.exceptions.ZigbeeException

    endpoint.claim_clusters([cluster])
    endpoint.set_cluster_bind(cluster, True)

    await endpoint.async_configure()

    assert cluster.bind.await_count == 3
    bind_events = [
        mock_call.args[1]
        for mock_call in endpoint.device.emit.call_args_list
        if mock_call.args[0] == ZHA_CLUSTER_HANDLER_MSG_BIND
    ]
    assert len(bind_events) == 1
    assert bind_events[0].success is False


async def test_endpoint_configure_reporting_error_emits_config_event(
    zha_gateway: Gateway,
) -> None:
    """Reporting errors still emit configure-reporting payloads."""
    endpoint, zigpy_ep = _make_endpoint(zha_gateway, in_clusters=[OnOff.cluster_id])
    cluster = zigpy_ep.on_off
    cluster.configure_reporting_multiple.side_effect = zigpy.exceptions.ZigbeeException

    endpoint.claim_clusters([cluster])
    endpoint.set_cluster_bind(cluster, True)
    endpoint.set_cluster_report_config(cluster, {"on_off": (0, 900, 1)}, set())

    await endpoint.async_configure()

    assert cluster.configure_reporting_multiple.await_count == 3
    reporting_events = [
        mock_call.args[1]
        for mock_call in endpoint.device.emit.call_args_list
        if mock_call.args[0] == ZHA_CLUSTER_HANDLER_MSG_CFG_RPT
    ]
    assert len(reporting_events) == 1
    assert reporting_events[0].attributes["on_off"]["status"] is None


async def test_endpoint_initialize_reads_cached_then_uncached_in_legacy_order(
    zha_gateway: Gateway,
) -> None:
    """Initialization preserves legacy attribute read order/chunking."""
    endpoint, zigpy_ep = _make_endpoint(
        zha_gateway, in_clusters=[LevelControl.cluster_id]
    )
    cluster = zigpy_ep.level
    endpoint.claim_clusters([cluster])
    endpoint.set_cluster_init_attrs(
        cluster,
        {
            "on_off_transition_time": True,
            "on_level": True,
            "on_transition_time": True,
            "off_transition_time": True,
            "default_move_rate": True,
            "start_up_current_level": True,
        },
    )
    endpoint.set_cluster_report_config(cluster, {"current_level": (1, 900, 1)}, set())

    await endpoint.async_initialize(from_cache=False)

    assert cluster.read_attributes.call_args_list == [
        call(
            [
                "on_off_transition_time",
                "on_level",
                "on_transition_time",
                "off_transition_time",
                "default_move_rate",
            ],
            allow_cache=True,
            only_cache=False,
            manufacturer=UNDEFINED,
        ),
        call(
            ["start_up_current_level"],
            allow_cache=True,
            only_cache=False,
            manufacturer=UNDEFINED,
        ),
        call(
            ["current_level"],
            allow_cache=False,
            only_cache=False,
            manufacturer=UNDEFINED,
        ),
    ]
