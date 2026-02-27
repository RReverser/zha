"""Test ZHA device discovery."""

import asyncio
from collections import defaultdict
from collections.abc import Callable
import enum
import json
import pathlib
import re
from unittest import mock
from unittest.mock import AsyncMock
import warnings

import pytest
from zhaquirks.ikea import PowerConfig1CRCluster, ScenesCluster
from zhaquirks.xiaomi import (
    BasicCluster,
    LocalIlluminanceMeasurementCluster,
    XiaomiPowerConfigurationPercent,
)
from zhaquirks.xiaomi.aqara.driver_curtain_e1 import (
    WindowCoveringE1,
    XiaomiAqaraDriverE1,
)
import zigpy.device
import zigpy.profiles.zha
import zigpy.quirks
from zigpy.quirks.v2 import (
    BinarySensorMetadata,
    EntityType,
    NumberMetadata,
    QuirkBuilder,
    ZCLSensorMetadata,
)
from zigpy.quirks.v2.homeassistant import UnitOfTime
import zigpy.types
from zigpy.zcl import ClusterType
import zigpy.zcl.clusters.closures
import zigpy.zcl.clusters.general
import zigpy.zcl.clusters.security
import zigpy.zcl.foundation as zcl_f

from tests.common import (
    SIG_EP_INPUT,
    SIG_EP_OUTPUT,
    SIG_EP_PROFILE,
    SIG_EP_TYPE,
    ZhaJsonEncoder,
    create_mock_zigpy_device,
    get_entity,
    join_zigpy_device,
    update_attribute_cache,
    zigpy_device_from_device_data,
    zigpy_device_from_json,
)
from zha.application import Platform
from zha.application.discovery import discover_device_entities, discover_group_entities
from zha.application.gateway import Gateway
from zha.application.helpers import DeviceOverridesConfiguration
from zha.application.platforms import PlatformEntity, binary_sensor, sensor
from zha.application.platforms.light import HueLight
from zha.application.platforms.number import BaseNumber, NumberMode
from zha.zigbee.cluster_handlers.const import PHILLIPS_REMOTE_CLUSTER
from zha.zigbee.group import GroupMemberReference


def _get_identify_cluster(zigpy_device):
    for endpoint in list(zigpy_device.endpoints.values())[1:]:
        if hasattr(endpoint, "identify"):
            return endpoint.identify


def test_discover_device_entities_continues_after_endpoint_exception() -> None:
    """Test endpoint discovery exception does not stop later endpoints."""
    zha_device = mock.MagicMock()
    zha_device.ieee = "00:0d:6f:00:0a:90:69:e7"
    zha_device.name = "FakeManufacturer FakeModel"
    zha_device.is_active_coordinator = False

    endpoint_1 = mock.MagicMock()
    endpoint_1.id = 1
    endpoint_1.device.ieee = zha_device.ieee

    endpoint_2 = mock.MagicMock()
    endpoint_2.id = 2
    endpoint_2.device.ieee = zha_device.ieee

    zha_device.endpoints = {
        endpoint_1.id: endpoint_1,
        endpoint_2.id: endpoint_2,
    }

    discovered_entity = mock.sentinel.discovered_entity

    def _discover(endpoint):
        if endpoint.id == 1:
            raise RuntimeError("endpoint discovery failed")
        return iter([discovered_entity])

    with mock.patch(
        "zha.application.discovery.discover_entities_for_endpoint",
        side_effect=_discover,
    ):
        # Issue being validated:
        # discover_device_entities() wraps iteration-level exceptions, but an endpoint
        # discovery exception currently terminates the underlying generator, so later
        # endpoints are never processed.
        #
        # Why this is a problem:
        # a single bad endpoint prevents discovery for all remaining endpoints on the
        # device, causing partial entity loss.
        entities = list(discover_device_entities(zha_device))

    assert entities == [discovered_entity]


@pytest.mark.parametrize("override_platform", [Platform.SWITCH, Platform.LIGHT])
async def test_device_override(
    zha_gateway: Gateway, override_platform: Platform
) -> None:
    """Test device discovery override."""

    zigpy_device = await zigpy_device_from_json(
        zha_gateway.application_controller,
        "tests/data/devices/sonoff-basiczbr3.json",
    )

    zha_gateway.config.config.device_overrides = {
        f"{zigpy_device.ieee}-1": DeviceOverridesConfiguration(type=override_platform)
    }

    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    # The overridden entity exists
    entity = get_entity(
        zha_device,
        platform=override_platform,
        qualifier_func=(
            lambda entity: (
                entity.cluster_handlers["on_off"].cluster
                == zigpy_device.endpoints[1].on_off
            )
        ),
    )
    assert entity is not None
    assert entity.unique_id == f"{zigpy_device.ieee}-1"

    # The original one does not
    with pytest.raises(KeyError):
        get_entity(
            zha_device,
            platform=(
                Platform.LIGHT
                if override_platform == Platform.SWITCH
                else Platform.SWITCH
            ),
            qualifier_func=lambda entity: (
                entity.cluster_handlers["on_off"].cluster
                == zigpy_device.endpoints[1].on_off
            ),
        )


async def test_device_override_entities(zha_gateway: Gateway) -> None:
    """Test device discovery entity changes."""
    device_data_text = await asyncio.get_running_loop().run_in_executor(
        None, pathlib.Path("tests/data/devices/tz3000-tqlv4ug4-ts0001.json").read_text
    )
    device_data = json.loads(device_data_text)

    zigpy_device = zigpy_device_from_device_data(
        app=zha_gateway.application_controller, device_data=device_data
    )

    zha_gateway.config.config.device_overrides = {
        f"{zigpy_device.ieee}-1": DeviceOverridesConfiguration(type=Platform.SWITCH)
    }

    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    # The light is gone
    with pytest.raises(KeyError):
        get_entity(zha_device, platform=Platform.LIGHT)

    # And has been replaced by a switch with the same unique ID
    switch = get_entity(zha_device, platform=Platform.SWITCH)
    assert switch.unique_id == f"{zigpy_device.ieee}-1"

    # All other entities and diagnostics stay the same
    loaded_device_data = json.loads(
        json.dumps(zha_device.get_diagnostics_json(), cls=ZhaJsonEncoder)
    )

    expected_loaded_device_data = device_data
    expected_loaded_device_data["zha_lib_entities"].pop("light")
    expected_loaded_device_data["zha_lib_entities"]["switch"] = [
        loaded_device_data["zha_lib_entities"]["switch"][0]
    ]

    assert loaded_device_data == expected_loaded_device_data


async def test_device_override_picks_highest_priority(
    zha_gateway: Gateway,
) -> None:
    """Test that a device override selects only the highest-priority match."""

    # A Philips light matches both Light (priority 0) and HueLight (priority 1) in the
    # LIGHT_OR_SWITCH_OR_SHADE feature group. With a SWITCH override, only one Switch
    # entity should be created, not duplicates from collecting all priority levels.
    zigpy_device = await zigpy_device_from_json(
        zha_gateway.application_controller,
        "tests/data/devices/philips-lct014.json",
    )
    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    # Only one light entity will be discovered
    entities = list(discover_device_entities(zha_device))
    light_entities = [e for e in entities if e.PLATFORM == Platform.LIGHT]
    assert len(light_entities) == 1
    assert isinstance(light_entities[0], HueLight)

    # With an override, it is going to be one switch
    zha_gateway.config.config.device_overrides = {
        f"{zigpy_device.ieee}-11": DeviceOverridesConfiguration(type=Platform.SWITCH)
    }

    entities = list(discover_device_entities(zha_device))
    switch_entities = [e for e in entities if e.PLATFORM == Platform.SWITCH]
    assert len(switch_entities) == 1


async def test_device_override_filter_bypassing(
    zha_gateway: Gateway,
) -> None:
    """Test that profile filtering is only bypassed for the override platform."""

    # The sercomm device is an ON_OFF_LIGHT with a PowerConfiguration cluster.
    # DeviceTracker matches PowerConfiguration but is restricted by profile_device_types
    # to the SmartThings arrival sensor device type. A SWITCH override should not cause
    # DeviceTracker to bypass that filter.
    zigpy_device = await zigpy_device_from_json(
        zha_gateway.application_controller,
        "tests/data/devices/sercomm-corp-sz-esw01-au.json",
    )

    zha_gateway.config.config.device_overrides = {
        f"{zigpy_device.ieee}-1": DeviceOverridesConfiguration(type=Platform.SWITCH)
    }

    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    with pytest.raises(KeyError):
        get_entity(zha_device, platform=Platform.DEVICE_TRACKER)


async def test_quirks_v2_entity_discovery(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
) -> None:
    """Test quirks v2 discovery."""

    zigpy_device = create_mock_zigpy_device(
        zha_gateway,
        {
            1: {
                SIG_EP_INPUT: [
                    zigpy.zcl.clusters.general.PowerConfiguration.cluster_id,
                    zigpy.zcl.clusters.general.Groups.cluster_id,
                    zigpy.zcl.clusters.general.OnOff.cluster_id,
                ],
                SIG_EP_OUTPUT: [
                    zigpy.zcl.clusters.general.Scenes.cluster_id,
                ],
                SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.NON_COLOR_CONTROLLER,
                SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
            }
        },
        ieee="01:2d:6f:00:0a:90:69:e8",
        manufacturer="Ikea of Sweden",
        model="TRADFRI remote control",
    )

    (
        QuirkBuilder(
            "Ikea of Sweden", "TRADFRI remote control", zigpy.quirks.DEVICE_REGISTRY
        )
        .replaces(PowerConfig1CRCluster)
        .replaces(ScenesCluster, cluster_type=ClusterType.Client)
        .number(
            zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name,
            zigpy.zcl.clusters.general.OnOff.cluster_id,
            min_value=1,
            max_value=100,
            step=1,
            unit=UnitOfTime.SECONDS,
            multiplier=1,
            mode="box",
            translation_key="off_wait_time",
            fallback_name="Off wait time",
        )
        .add_to_registry()
    )

    zigpy_device = zigpy.quirks.DEVICE_REGISTRY.get_device(zigpy_device)
    zigpy_device.endpoints[1].power.PLUGGED_ATTR_READS = {
        "battery_voltage": 3,
        "battery_percentage_remaining": 100,
    }
    update_attribute_cache(zigpy_device.endpoints[1].power)
    zigpy_device.endpoints[1].on_off.PLUGGED_ATTR_READS = {
        zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name: 3,
    }
    update_attribute_cache(zigpy_device.endpoints[1].on_off)

    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    number_entity = get_entity(zha_device, platform=Platform.NUMBER)
    assert isinstance(number_entity, BaseNumber)
    assert number_entity.mode == NumberMode.BOX  # verify v2 quirk set this


async def test_quirks_v2_entity_discovery_e1_curtain(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
) -> None:
    """Test quirks v2 discovery for e1 curtain motor."""

    class AqaraE1HookState(zigpy.types.enum8):
        """Aqara hook state."""

        Unlocked = 0x00
        Locked = 0x01
        Locking = 0x02
        Unlocking = 0x03

    class FakeXiaomiAqaraDriverE1(XiaomiAqaraDriverE1):
        """Fake XiaomiAqaraDriverE1 cluster."""

        attributes = XiaomiAqaraDriverE1.attributes.copy()
        attributes.update(
            {
                0x9999: ("error_detected", zigpy.types.Bool, True),
            }
        )

    (
        QuirkBuilder("LUMI", "lumi.curtain.agl006")
        .adds(LocalIlluminanceMeasurementCluster)
        .replaces(BasicCluster)
        .replaces(XiaomiPowerConfigurationPercent)
        .replaces(WindowCoveringE1)
        .replaces(FakeXiaomiAqaraDriverE1)
        .removes(FakeXiaomiAqaraDriverE1, cluster_type=ClusterType.Client)
        .enum(
            BasicCluster.AttributeDefs.power_source.name,
            BasicCluster.PowerSource,
            BasicCluster.cluster_id,
            entity_platform=Platform.SENSOR,
            entity_type=EntityType.DIAGNOSTIC,
            translation_key="power_source",
            fallback_name="Power source",
        )
        .enum(
            "hooks_state",
            AqaraE1HookState,
            FakeXiaomiAqaraDriverE1.cluster_id,
            entity_platform=Platform.SENSOR,
            entity_type=EntityType.DIAGNOSTIC,
            translation_key="hooks_state",
            fallback_name="Hooks state",
        )
        .binary_sensor(
            "error_detected",
            FakeXiaomiAqaraDriverE1.cluster_id,
            translation_key="error_detected",
            fallback_name="Error detected",
        )
        .add_to_registry()
    )

    aqara_E1_device = create_mock_zigpy_device(
        zha_gateway,
        {
            1: {
                SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.WINDOW_COVERING_DEVICE,
                SIG_EP_INPUT: [
                    zigpy.zcl.clusters.general.Basic.cluster_id,
                    zigpy.zcl.clusters.general.PowerConfiguration.cluster_id,
                    zigpy.zcl.clusters.general.Identify.cluster_id,
                    zigpy.zcl.clusters.general.Time.cluster_id,
                    WindowCoveringE1.cluster_id,
                    XiaomiAqaraDriverE1.cluster_id,
                ],
                SIG_EP_OUTPUT: [
                    zigpy.zcl.clusters.general.Identify.cluster_id,
                    zigpy.zcl.clusters.general.Time.cluster_id,
                    zigpy.zcl.clusters.general.Ota.cluster_id,
                    XiaomiAqaraDriverE1.cluster_id,
                ],
                SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
            }
        },
        ieee="01:2d:6f:00:0a:90:69:e8",
        manufacturer="LUMI",
        model="lumi.curtain.agl006",
    )
    aqara_E1_device = zigpy.quirks.DEVICE_REGISTRY.get_device(aqara_E1_device)

    aqara_E1_device.endpoints[1].opple_cluster.PLUGGED_ATTR_READS = {
        "hand_open": 0,
        "positions_stored": 0,
        "hooks_lock": 0,
        "hooks_state": AqaraE1HookState.Unlocked,
        "light_level": 0,
        "error_detected": 0,
    }
    update_attribute_cache(aqara_E1_device.endpoints[1].opple_cluster)

    aqara_E1_device.endpoints[1].basic.PLUGGED_ATTR_READS = {
        BasicCluster.AttributeDefs.power_source.name: BasicCluster.PowerSource.Mains_single_phase,
    }
    update_attribute_cache(aqara_E1_device.endpoints[1].basic)

    WCAttrs = zigpy.zcl.clusters.closures.WindowCovering.AttributeDefs
    WCT = zigpy.zcl.clusters.closures.WindowCovering.WindowCoveringType
    WCCS = zigpy.zcl.clusters.closures.WindowCovering.ConfigStatus
    aqara_E1_device.endpoints[1].window_covering.PLUGGED_ATTR_READS = {
        WCAttrs.current_position_lift_percentage.name: 0,
        WCAttrs.window_covering_type.name: WCT.Drapery,
        WCAttrs.config_status.name: WCCS(~WCCS.Open_up_commands_reversed),
    }
    update_attribute_cache(aqara_E1_device.endpoints[1].window_covering)

    zha_device = await join_zigpy_device(zha_gateway, aqara_E1_device)

    power_source_entity = get_entity(
        zha_device,
        platform=Platform.SENSOR,
        exact_entity_type=sensor.EnumSensor,
        qualifier_func=lambda e: e._enum == BasicCluster.PowerSource,
    )
    assert (
        power_source_entity.state["state"]
        == BasicCluster.PowerSource.Mains_single_phase.name
    )

    hook_state_entity = get_entity(
        zha_device,
        platform=Platform.SENSOR,
        exact_entity_type=sensor.EnumSensor,
        qualifier_func=lambda e: e._enum == AqaraE1HookState,
    )
    assert hook_state_entity.state["state"] == AqaraE1HookState.Unlocked.name

    error_detected_entity = get_entity(
        zha_device,
        platform=Platform.BINARY_SENSOR,
        exact_entity_type=binary_sensor.BinarySensor,
        qualifier_func=lambda e: e._attribute_name == "error_detected",
    )
    assert error_detected_entity.state["state"] is False


def _get_test_device(
    zha_gateway: Gateway,
    manufacturer: str,
    model: str,
    augment_method: Callable[[QuirkBuilder], QuirkBuilder] | None = None,
):
    zigpy_device = create_mock_zigpy_device(
        zha_gateway,
        {
            1: {
                SIG_EP_INPUT: [
                    zigpy.zcl.clusters.general.PowerConfiguration.cluster_id,
                    zigpy.zcl.clusters.general.Groups.cluster_id,
                    zigpy.zcl.clusters.general.OnOff.cluster_id,
                ],
                SIG_EP_OUTPUT: [
                    zigpy.zcl.clusters.general.Scenes.cluster_id,
                ],
                SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.NON_COLOR_CONTROLLER,
                SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
            }
        },
        ieee="01:2d:6f:00:0a:90:69:e8",
        manufacturer=manufacturer,
        model=model,
    )

    quirk_builder = (
        QuirkBuilder(manufacturer, model, zigpy.quirks.DEVICE_REGISTRY)
        .replaces(PowerConfig1CRCluster)
        .replaces(ScenesCluster, cluster_type=ClusterType.Client)
        .number(
            zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name,
            zigpy.zcl.clusters.general.OnOff.cluster_id,
            endpoint_id=3,
            min_value=1,
            max_value=100,
            step=1,
            unit=UnitOfTime.SECONDS,
            multiplier=1,
            translation_key="on_off_transition_time",
            fallback_name="On off transition time",
        )
        .number(
            zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name,
            zigpy.zcl.clusters.general.Time.cluster_id,
            min_value=1,
            max_value=100,
            step=1,
            unit=UnitOfTime.SECONDS,
            multiplier=1,
            translation_key="on_off_transition_time",
            fallback_name="On off transition time",
        )
        .sensor(
            zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name,
            zigpy.zcl.clusters.general.OnOff.cluster_id,
            entity_type=EntityType.CONFIG,
            translation_key="analog_input",
            fallback_name="Analog input",
        )
    )

    if augment_method:
        quirk_builder = augment_method(quirk_builder)

    quirk_builder.add_to_registry()

    zigpy_device = zigpy.quirks.DEVICE_REGISTRY.get_device(zigpy_device)
    zigpy_device.endpoints[1].power.PLUGGED_ATTR_READS = {
        "battery_voltage": 3,
        "battery_percentage_remaining": 100,
    }
    update_attribute_cache(zigpy_device.endpoints[1].power)
    zigpy_device.endpoints[1].on_off.PLUGGED_ATTR_READS = {
        zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name: 3,
    }
    update_attribute_cache(zigpy_device.endpoints[1].on_off)
    return zigpy_device


async def test_quirks_v2_entity_no_metadata(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test quirks v2 discovery skipped - no metadata."""

    zigpy_device = _get_test_device(
        zha_gateway, "Ikea of Sweden2", "TRADFRI remote control2"
    )
    setattr(zigpy_device, "_exposes_metadata", {})
    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)
    assert (
        f"Device: {str(zigpy_device.ieee)}-{zha_device.name} does not expose any quirks v2 entities"
        in caplog.text
    )


async def test_quirks_v2_entity_discovery_errors(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test quirks v2 discovery skipped - errors."""

    zigpy_device = _get_test_device(
        zha_gateway, "Ikea of Sweden3", "TRADFRI remote control3"
    )

    # Inject unknown quirks v2 entity metadata
    class UnknownEntityMetadata:
        entity_platform = Platform.UPDATE

    zigpy_device._exposes_metadata[
        (1, zigpy.zcl.clusters.general.OnOff.cluster_id, ClusterType.Server)
    ].append(UnknownEntityMetadata())

    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    assert (
        f"Device: {zigpy_device.ieee}-{zha_device.name} does not have an"
        " endpoint with id: 3 - unable to create entity with"
        " cluster details: (3, 6, <ClusterType.Server: 0>)"
    ) in caplog.text

    time_cluster_id = zigpy.zcl.clusters.general.Time.cluster_id

    assert (
        f"Device: {zigpy_device.ieee}-{zha_device.name} does not have a"
        f" cluster with id: {time_cluster_id} - unable to create entity with"
        f" cluster details: (1, {time_cluster_id}, <ClusterType.Server: 0>)"
    ) in caplog.text

    device_info = f"{zigpy_device.ieee}-{zha_device.name}"
    device_regex = (
        rf"Device: {re.escape(device_info)} has an entity with details: (.*?) that"
        rf" does not have an entity class mapping - unable to create entity"
    )
    assert re.search(device_regex, caplog.text)


DEVICE_CLASS_TYPES = [NumberMetadata, BinarySensorMetadata, ZCLSensorMetadata]


class BadDeviceClass(enum.Enum):
    """Bad device class."""

    BAD = "bad"


def bad_binary_sensor_device_class(
    quirk_builder: QuirkBuilder,
) -> QuirkBuilder:
    """Introduce a bad device class on a binary sensor."""

    return quirk_builder.binary_sensor(
        zigpy.zcl.clusters.general.OnOff.AttributeDefs.on_off.name,
        zigpy.zcl.clusters.general.OnOff.cluster_id,
        translation_key="on_off",
        fallback_name="On off",
        device_class=BadDeviceClass.BAD,
    )


def bad_sensor_device_class(
    quirk_builder: QuirkBuilder,
) -> QuirkBuilder:
    """Introduce a bad device class on a sensor."""

    return quirk_builder.sensor(
        zigpy.zcl.clusters.general.OnOff.AttributeDefs.off_wait_time.name,
        zigpy.zcl.clusters.general.OnOff.cluster_id,
        translation_key="off_wait_time",
        fallback_name="Off wait time",
        device_class=BadDeviceClass.BAD,
    )


def bad_number_device_class(
    quirk_builder: QuirkBuilder,
) -> QuirkBuilder:
    """Introduce a bad device class on a number."""

    return quirk_builder.number(
        zigpy.zcl.clusters.general.OnOff.AttributeDefs.on_time.name,
        zigpy.zcl.clusters.general.OnOff.cluster_id,
        translation_key="on_time",
        fallback_name="On time",
        device_class=BadDeviceClass.BAD,
    )


ERROR_ROOT = "Quirks provided an invalid device class"


@pytest.mark.parametrize(
    ("augment_method", "expected_exception_string"),
    [
        (
            bad_binary_sensor_device_class,
            f"{ERROR_ROOT}: BadDeviceClass.BAD for platform binary_sensor",
        ),
        (
            bad_sensor_device_class,
            f"{ERROR_ROOT}: BadDeviceClass.BAD for platform sensor",
        ),
        (
            bad_number_device_class,
            f"{ERROR_ROOT}: BadDeviceClass.BAD for platform number",
        ),
    ],
)
async def test_quirks_v2_metadata_bad_device_classes(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
    caplog: pytest.LogCaptureFixture,
    augment_method: Callable[[QuirkBuilder], QuirkBuilder],
    expected_exception_string: str,
) -> None:
    """Test bad quirks v2 device classes."""

    # introduce an error
    zigpy_device = _get_test_device(
        zha_gateway,
        "Ikea of Sweden5",
        "TRADFRI remote control5",
        augment_method=augment_method,
    )
    await join_zigpy_device(zha_gateway, zigpy_device)

    assert expected_exception_string in caplog.text

    # remove the device so we don't pollute the rest of the tests
    zigpy.quirks.DEVICE_REGISTRY.remove(zigpy_device)


async def test_quirks_v2_fallback_name(zha_gateway: Gateway) -> None:
    """Test quirks v2 fallback name."""

    zigpy_device = _get_test_device(
        zha_gateway,
        "Ikea of Sweden6",
        "TRADFRI remote control6",
        augment_method=lambda builder: builder.sensor(
            attribute_name=zigpy.zcl.clusters.general.OnOff.AttributeDefs.global_scene_control.name,
            cluster_id=zigpy.zcl.clusters.general.OnOff.cluster_id,
            translation_key="some_sensor",
            fallback_name="Fallback name",
        ),
    )
    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    entity = get_entity(
        zha_device,
        platform=Platform.SENSOR,
        qualifier_func=lambda e: e.fallback_name == "Fallback name",
    )
    assert entity.fallback_name == "Fallback name"


async def test_discover_group_entities_member_drop_runs_group_entity_on_remove(
    zha_gateway: Gateway,
) -> None:
    """Test group member drop cleanup runs GroupEntity.on_remove."""
    switch_endpoints = {
        1: {
            SIG_EP_INPUT: [
                zigpy.zcl.clusters.general.Basic.cluster_id,
                zigpy.zcl.clusters.general.OnOff.cluster_id,
                zigpy.zcl.clusters.general.Groups.cluster_id,
            ],
            SIG_EP_OUTPUT: [],
            SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.ON_OFF_SWITCH,
            SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
        }
    }

    zigpy_device_1 = create_mock_zigpy_device(
        zha_gateway,
        switch_endpoints,
        ieee="01:2d:6f:00:0a:90:69:e8",
    )
    zigpy_device_2 = create_mock_zigpy_device(
        zha_gateway,
        switch_endpoints,
        ieee="02:2d:6f:00:0a:90:69:e9",
    )

    zha_device_1 = await join_zigpy_device(zha_gateway, zigpy_device_1)
    zha_device_2 = await join_zigpy_device(zha_gateway, zigpy_device_2)

    members = [
        GroupMemberReference(ieee=zha_device_1.ieee, endpoint_id=1),
        GroupMemberReference(ieee=zha_device_2.ieee, endpoint_id=1),
    ]
    zha_group = await zha_gateway.async_create_zigpy_group("Test Group", members)
    zha_group.clear_caches()

    for entity in discover_group_entities(zha_group):
        entity.on_add()

    assert zha_group.group_entities
    (group_entity,) = tuple(zha_group.group_entities.values())

    try:
        with mock.patch.object(
            group_entity,
            "on_remove",
            new=AsyncMock(wraps=group_entity.on_remove),
        ) as mocked_on_remove:
            for member in list(zha_group.zigpy_group.members)[1:]:
                zha_group.zigpy_group.members.pop(member)
            zha_group.clear_caches()

            # Issue being validated:
            # when group members drop below 2, discover_group_entities() currently
            # clears group.group_entities directly.
            #
            # Why this is a problem:
            # bypassing GroupEntity.on_remove() skips entity-level cleanup (task/handle
            # cancellation and unregister logic), leaking group entity lifecycle state.
            list(discover_group_entities(zha_group))

            assert mocked_on_remove.await_count == 1
    finally:
        await group_entity.on_remove()


async def test_discover_group_entities_platform_quorum_drop_prunes_stale_platform_entity(
    zha_gateway: Gateway,
) -> None:
    """Test platform quorum drop cleanup removes stale group platform entities."""
    light_endpoints = {
        1: {
            SIG_EP_INPUT: [
                zigpy.zcl.clusters.general.Basic.cluster_id,
                zigpy.zcl.clusters.general.OnOff.cluster_id,
                zigpy.zcl.clusters.general.LevelControl.cluster_id,
                zigpy.zcl.clusters.general.Groups.cluster_id,
            ],
            SIG_EP_OUTPUT: [],
            SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.DIMMABLE_LIGHT,
            SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
        }
    }
    switch_endpoints = {
        1: {
            SIG_EP_INPUT: [
                zigpy.zcl.clusters.general.Basic.cluster_id,
                zigpy.zcl.clusters.general.OnOff.cluster_id,
                zigpy.zcl.clusters.general.Groups.cluster_id,
            ],
            SIG_EP_OUTPUT: [],
            SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.ON_OFF_SWITCH,
            SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
        }
    }

    zigpy_light_1 = create_mock_zigpy_device(
        zha_gateway,
        light_endpoints,
        ieee="11:2d:6f:00:0a:90:69:e1",
    )
    zigpy_light_2 = create_mock_zigpy_device(
        zha_gateway,
        light_endpoints,
        ieee="12:2d:6f:00:0a:90:69:e2",
    )
    zigpy_switch = create_mock_zigpy_device(
        zha_gateway,
        switch_endpoints,
        ieee="13:2d:6f:00:0a:90:69:e3",
    )

    zha_light_1 = await join_zigpy_device(zha_gateway, zigpy_light_1)
    zha_light_2 = await join_zigpy_device(zha_gateway, zigpy_light_2)
    zha_switch = await join_zigpy_device(zha_gateway, zigpy_switch)

    members = [
        GroupMemberReference(ieee=zha_light_1.ieee, endpoint_id=1),
        GroupMemberReference(ieee=zha_light_2.ieee, endpoint_id=1),
        GroupMemberReference(ieee=zha_switch.ieee, endpoint_id=1),
    ]
    zha_group = await zha_gateway.async_create_zigpy_group("Test Group", members)
    stale_light_group_entity = None
    try:
        zha_group.clear_caches()

        for entity in discover_group_entities(zha_group):
            entity.on_add()

        light_group_entities = [
            entity
            for entity in zha_group.group_entities.values()
            if entity.PLATFORM == Platform.LIGHT
        ]
        assert len(light_group_entities) == 1
        stale_light_group_entity = light_group_entities[0]

        removed_light_member = False
        for member in list(zha_group.zigpy_group.members):
            if member[0] == zha_light_2.ieee:
                zha_group.zigpy_group.members.pop(member)
                removed_light_member = True
                break
        assert removed_light_member
        zha_group.clear_caches()

        # Issue being validated:
        # when a single platform's member count drops below 2 (for example, LIGHT),
        # discover_group_entities() yields no replacement but does not prune stale
        # group entities for that platform if total members remain >= 2.
        #
        # Why this is a problem:
        # stale platform group entities remain exposed and continue receiving updates
        # even though the group no longer has enough members for that platform.
        list(discover_group_entities(zha_group))

        assert stale_light_group_entity.unique_id not in zha_group.group_entities
    finally:
        if stale_light_group_entity is not None:
            await stale_light_group_entity.on_remove()
        for entity in tuple(zha_group.group_entities.values()):
            await entity.on_remove()
        zha_group.clear_caches()


async def test_group_info_object_cache_stale_after_unregister_group_entity(
    zha_gateway: Gateway,
) -> None:
    """Test unregistering a group entity invalidates Group.info_object cache."""
    switch_endpoints = {
        1: {
            SIG_EP_INPUT: [
                zigpy.zcl.clusters.general.Basic.cluster_id,
                zigpy.zcl.clusters.general.OnOff.cluster_id,
                zigpy.zcl.clusters.general.Groups.cluster_id,
            ],
            SIG_EP_OUTPUT: [],
            SIG_EP_TYPE: zigpy.profiles.zha.DeviceType.ON_OFF_SWITCH,
            SIG_EP_PROFILE: zigpy.profiles.zha.PROFILE_ID,
        }
    }

    zigpy_switch_1 = create_mock_zigpy_device(
        zha_gateway,
        switch_endpoints,
        ieee="21:2d:6f:00:0a:90:69:e1",
    )
    zigpy_switch_2 = create_mock_zigpy_device(
        zha_gateway,
        switch_endpoints,
        ieee="22:2d:6f:00:0a:90:69:e2",
    )

    zha_switch_1 = await join_zigpy_device(zha_gateway, zigpy_switch_1)
    zha_switch_2 = await join_zigpy_device(zha_gateway, zigpy_switch_2)

    members = [
        GroupMemberReference(ieee=zha_switch_1.ieee, endpoint_id=1),
        GroupMemberReference(ieee=zha_switch_2.ieee, endpoint_id=1),
    ]
    zha_group = await zha_gateway.async_create_zigpy_group("Test Group", members)
    group_entity = None
    try:
        zha_group.clear_caches()

        for entity in discover_group_entities(zha_group):
            entity.on_add()

        assert zha_group.group_entities
        (group_entity,) = tuple(zha_group.group_entities.values())

        assert group_entity.unique_id in zha_group.info_object.entities

        # Issue being validated:
        # unregister_group_entity() mutates group_entities but does not invalidate the
        # cached Group.info_object property.
        #
        # Why this is a problem:
        # consumers of group diagnostics/event payloads can read stale entity metadata
        # after an entity is removed, leading to incorrect state reporting.
        zha_group.unregister_group_entity(group_entity)

        assert group_entity.unique_id not in zha_group.info_object.entities
    finally:
        if group_entity is not None:
            await group_entity.on_remove()
        for entity in tuple(zha_group.group_entities.values()):
            await entity.on_remove()
        zha_group.clear_caches()


def pytest_generate_tests(metafunc):
    """Generate tests for all device files."""
    if "file_path" in metafunc.fixturenames:
        # use the filename as ID for better test names
        file_paths = sorted(pathlib.Path("tests/data/devices").glob("**/*.json"))
        file_paths = [
            f for f in file_paths if f.name != "lumi-lumi-motion-agl04.json"
        ]  # TODO: fix lingering timer for `_Motion._turn_off` in quirks

        metafunc.parametrize("file_path", file_paths, ids=[f.name for f in file_paths])


async def test_devices_from_files(
    zha_gateway: Gateway,  # pylint: disable=unused-argument
    file_path: pathlib.Path,
) -> None:
    """Test all devices."""
    with mock.patch(
        "zigpy.zcl.clusters.general.Identify.request",
        new=AsyncMock(return_value=[mock.sentinel.data, zcl_f.Status.SUCCESS]),
    ):
        device_data_text = await asyncio.get_running_loop().run_in_executor(
            None, file_path.read_text
        )
        device_data = json.loads(device_data_text)

        zigpy_device = zigpy_device_from_device_data(
            app=zha_gateway.application_controller, device_data=device_data
        )

        # XXX: attribute updates during device initialization unfortunately triggers
        # logic within quirks to "fix" attributes. Since these attributes are *read out*
        # in this state, this will compound the "fix" repeatedly.
        with (
            mock.patch("zigpy.zcl.Cluster._update_attribute"),
            mock.patch("zigpy.zcl.helpers.AttributeCache.set_value"),
        ):
            zha_device = await join_zigpy_device(zha_gateway, zigpy_device)
            await zha_gateway.async_block_till_done(wait_background_tasks=True)
            assert zha_device is not None

        unique_id_collisions = defaultdict(list)
        for entity in zha_device.platform_entities.values():
            unique_id_collisions[entity.unique_id].append(entity)

        for unique_id, entities in unique_id_collisions.items():
            if len(entities) == 1:
                continue

            prefixed_unique_ids = [
                f"{entity.PLATFORM.name.lower()}.{entity.unique_id}"
                for entity in entities
            ]

            if len(set(prefixed_unique_ids)) != len(entities):
                raise ValueError(
                    f"Duplicate unique_id {unique_id} found in entities: {entities}"
                )
            else:
                warnings.warn(
                    f"Unique IDs are unique only with platform prefix: {dict(zip(prefixed_unique_ids, entities))}"
                )

        unique_id_migrations: dict[tuple[Platform, str], PlatformEntity] = {}
        for entity in zha_device.platform_entities.values():
            for old_unique_id in entity.migrate_unique_ids:
                key = (entity.PLATFORM, old_unique_id)
                if key in unique_id_migrations:
                    raise ValueError(
                        f"Duplicate unique_id {key} found in migration: "
                        f"{unique_id_migrations[key]} and {entity}"
                    )

                unique_id_migrations[key] = entity

        await zha_device.on_remove()

        # XXX: We re-serialize the JSON because integer enum types are converted when
        # serializing but will not compare properly otherwise
        loaded_device_data = json.loads(
            json.dumps(zha_device.get_diagnostics_json(), cls=ZhaJsonEncoder)
        )
        assert loaded_device_data == device_data

        # Assert identify called on join for devices that support it
        cluster_identify = _get_identify_cluster(zha_device.device)
        if cluster_identify and not zha_device.skip_configuration:
            assert cluster_identify.request.mock_calls == [
                mock.call(
                    False,
                    cluster_identify.commands_by_name["trigger_effect"].id,
                    cluster_identify.commands_by_name["trigger_effect"].schema,
                    effect_id=zigpy.zcl.clusters.general.Identify.EffectIdentifier.Okay,
                    effect_variant=(
                        zigpy.zcl.clusters.general.Identify.EffectVariant.Default
                    ),
                    # enhance this maybe by looking at disable default response?
                    expect_reply=(
                        cluster_identify.endpoint.model
                        not in ("HDC52EastwindFan", "HBUniversalCFRemote")
                    ),
                    manufacturer=None,
                )
            ]


async def test_get_diagnostics_json_repeated_calls(zha_gateway: Gateway) -> None:
    """Test that calling get_diagnostics_json twice produces the same result."""
    zigpy_device = await zigpy_device_from_json(
        zha_gateway.application_controller,
        "tests/data/devices/jasco-products-45856.json",
    )
    zha_device = await join_zigpy_device(zha_gateway, zigpy_device)

    first = json.loads(
        json.dumps(zha_device.get_diagnostics_json(), cls=ZhaJsonEncoder)
    )
    second = json.loads(
        json.dumps(zha_device.get_diagnostics_json(), cls=ZhaJsonEncoder)
    )
    assert first == second


async def test_cluster_handler_only_clusters_are_bound(zha_gateway: Gateway) -> None:
    """Test CLUSTER_HANDLER_ONLY_CLUSTERS causes binds even without entities."""
    zigpy_device = await zigpy_device_from_json(
        zha_gateway.application_controller,
        "tests/data/devices/signify-netherlands-b-v-rwl022.json",
    )

    # The Philips remote cluster (0xFC00) is in CLUSTER_HANDLER_ONLY_CLUSTERS: it
    # doesn't produce any entities but must still be bound
    philips_cluster = zigpy_device.endpoints[1].in_clusters[PHILLIPS_REMOTE_CLUSTER]

    await join_zigpy_device(zha_gateway, zigpy_device)
    await zha_gateway.async_block_till_done(wait_background_tasks=True)

    assert len(philips_cluster.bind.mock_calls) == 1
