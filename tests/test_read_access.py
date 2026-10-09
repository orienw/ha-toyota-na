"""Cached readings and shutdown independently of command access."""

import json
import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from common import entity_id, make_17cy_vehicle, make_24mm_vehicle, make_vehicle
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na import patch_client
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    VehicleFeatures,
)

RECOVERY_STATUS = {
    "vin": "TESTVIN24",
    "telemetry": {"odo": {"value": 1234, "unit": "mi"}},
    "electric": None,
}
ELECTRIC = {
    "battery": {"stateOfChargeDisplay": {"value": 0, "unit": "%"}},
    "charging": {"chargingState": "charging"},
}
FIELD_ERROR = {
    "path": ["getVehicleStatus", "electric", "charging", "chargeSettings", "schedules"],
    "message": "Field failed",
}


class _Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"data": {"getVehicleStatus": {"vin": "TESTVIN24"}}} if body is None else body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return json.dumps(self.body)


def appsync_client():
    client = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            get_guid=AsyncMock(return_value="guid"),
            get_device_id=lambda: "device",
            get_access_token=AsyncMock(return_value="token"),
        ),
        get_telemetry=AsyncMock(return_value={}),
    )
    client.graphql_request = types.MethodType(patch_client.graphql_request, client)
    client.graphql_get_vehicle_status = types.MethodType(
        patch_client.graphql_get_vehicle_status, client
    )
    return client


def http_session():
    session = MagicMock()
    session.__aenter__.return_value = session
    return session


def reading_names(hass, entry, vin):
    """Names of a vehicle's sensors and binary sensors."""
    return {
        item.unique_id.removeprefix(f"{vin}.")
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain in ("sensor", "binary_sensor") and item.unique_id.startswith(f"{vin}.")
    }


@pytest.mark.parametrize(
    "response",
    [
        {"vehicleInfo": {"chargeInfo": None}},
        {"vehicleInfo": {"chargeInfo": {"gasolineTravelableDistance": 0}}},
        patch_client.aiohttp.ClientResponseError(MagicMock(), (), status=401),
    ],
)
async def test_21mm_legacy_fallback_restores_all_nine_ev_entities(hass, setup_vehicles, response):
    status = {
        "vehicleInfo": {
            "acquisitionDatetime": "2026-09-27T12:00:00Z",
            "chargeInfo": {
                "evDistance": 200,
                "evDistanceAC": 180,
                "evDistanceUnit": "km",
                "chargeRemainingAmount": 85,
                "plugStatus": 40,
                "remainingChargeTime": 90,
                "evTravelableDistance": 200,
                "chargeType": 2,
                "connectorStatus": 5,
            },
        }
    }
    expected = {
        "EV Range": "200",
        "EV Range AC": "180",
        "EV Battery Level": "85",
        "Plug Status": "charging",
        "Remaining Charge Time": "90",
        "EV Travelable Distance": "200",
        "Charge Type": "2",
        "Connector Status": "locked",
    }
    client = types.SimpleNamespace(
        api_get=AsyncMock(side_effect=[response, status]),
        get_telemetry=AsyncMock(return_value={}),
        get_vehicle_status_21mm=AsyncMock(return_value={}),
        get_engine_status_21mm=AsyncMock(return_value={}),
        get_climate_settings=AsyncMock(return_value=None),
    )
    client.get_electric_status = types.MethodType(patch_client.get_electric_status, client)
    vehicle = make_vehicle(client)
    vehicle._has_electric = True
    await vehicle.update()

    account = await setup_vehicles([vehicle])

    assert reading_names(hass, account.entry, "TESTVIN") == set(expected) | {"Charging Status"}
    for name, value in expected.items():
        assert hass.states.get(entity_id(hass, "sensor", f"TESTVIN.{name}")).state == value, name
    charging = hass.states.get(entity_id(hass, "binary_sensor", "TESTVIN.Charging Status"))
    assert charging.state == "on"
    assert [call.args[0] for call in client.api_get.call_args_list] == [
        "v3/electric/status",
        "v2/electric/status",
    ]


async def test_failed_unload_keeps_live_subscription_running(hass, setup_vehicles):
    account = await setup_vehicles([])
    with patch.object(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False)):
        assert not await hass.config_entries.async_unload(account.entry.entry_id)
    account.websocket.stop.assert_not_awaited()
    assert account.entry.state is ConfigEntryState.FAILED_UNLOAD
    # Home Assistant won't unload an entry again once its unload failed,
    # so mark it loaded to retry.
    account.entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await hass.config_entries.async_unload(account.entry.entry_id)
    account.websocket.stop.assert_awaited_once()
    assert account.entry.entry_id not in hass.data[DOMAIN]


@pytest.mark.parametrize(
    ("response", "calls"),
    [
        (
            {
                "errors": [
                    {"message": "Validation error of type FieldUndefined: actualChargingRate"}
                ]
            },
            2,
        ),
        ({"data": {"getVehicleStatus": RECOVERY_STATUS}, "errors": [FIELD_ERROR]}, 2),
        (
            {
                "data": {"getVehicleStatus": {**RECOVERY_STATUS, "electric": ELECTRIC}},
                "errors": [FIELD_ERROR],
            },
            1,
        ),
    ],
)
async def test_recovery_creates_battery_and_charging_entities_after_vehicle_update(
    hass, setup_vehicles, response, calls
):
    healthy = deepcopy({**RECOVERY_STATUS, "electric": ELECTRIC})
    vehicle = make_24mm_vehicle(appsync_client())
    session = http_session()
    session.post.side_effect = [
        _Response(200, deepcopy(response)),
        _Response(200, {"data": {"getVehicleStatus": healthy}}),
    ]
    with patch.object(patch_client.aiohttp, "ClientSession", return_value=session):
        await vehicle.update()

    await setup_vehicles([vehicle])

    battery = entity_id(hass, "sensor", "TESTVIN24.EV Battery Level")
    charging = entity_id(hass, "binary_sensor", "TESTVIN24.Charging Status")
    assert hass.states.get(battery).state == "0"
    assert hass.states.get(charging).state == "on"
    assert hass.states.get(entity_id(hass, "sensor", "TESTVIN24.Odometer")).state == "1234"
    assert session.post.call_count == calls


class ReadAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_supported_generations_read_cached_status_without_subscription(self):
        rest_status = {
            "vehicleStatus": [
                {
                    "category": "Driver Side",
                    "sections": [
                        {
                            "section": "Door",
                            "values": [{"value": "closed"}, {"value": "locked"}],
                        }
                    ],
                }
            ]
        }
        graphql_status = {
            "vehicleState": {
                "doors": {
                    "driverSide": {"position": {"status": "closed"}, "lock": {"status": "locked"}},
                }
            }
        }
        for generation in (
            ApiVehicleGeneration.CY17,
            ApiVehicleGeneration.CY17PLUS,
            ApiVehicleGeneration.MM21,
            ApiVehicleGeneration.MM24,
            ApiVehicleGeneration.BEV26,
        ):
            with self.subTest(generation=generation):
                client = types.SimpleNamespace(
                    get_telemetry=AsyncMock(return_value={}),
                    get_vehicle_status_17cy=AsyncMock(return_value=rest_status),
                    get_vehicle_status_17cyplus=AsyncMock(return_value=rest_status),
                    get_vehicle_status_21mm=AsyncMock(return_value=rest_status),
                    get_engine_status_17cy=AsyncMock(return_value={}),
                    get_engine_status_17cyplus=AsyncMock(return_value={}),
                    get_engine_status_21mm=AsyncMock(return_value={}),
                    graphql_get_vehicle_status=AsyncMock(return_value=graphql_status),
                    graphql_pre_wake=AsyncMock(),
                )
                vehicle = (
                    make_17cy_vehicle(client)
                    if generation == ApiVehicleGeneration.CY17
                    else make_vehicle(client)
                )
                vehicle._generation = generation
                vehicle._has_electric = False
                vehicle._has_remote_subscription = False
                vehicle._feature_flags = {"vehicleState": 2, "remoteCommands": 2}
                await vehicle.update()
                self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].locked)
                self.assertFalse(vehicle.supports_command(RemoteRequestCommand.Refresh))
                self.assertFalse(vehicle.supports_command(RemoteRequestCommand.DoorUnlock))
                client.graphql_pre_wake.assert_not_awaited()


class StatusRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = appsync_client()
        self.session = http_session()
        session_patch = patch.object(
            patch_client.aiohttp, "ClientSession", return_value=self.session
        )
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.status = deepcopy(RECOVERY_STATUS)
        self.electric = deepcopy(ELECTRIC)
        self.error = deepcopy(FIELD_ERROR)

    async def test_recovered_sections_do_not_inherit_a_newer_primary_timestamp(self):
        for fallback_time in ("2026-09-27T11:00:00Z", None):
            with self.subTest(fallback_time=fallback_time):
                vehicle = make_24mm_vehicle(self.client)
                vehicle.apply_graphql_status(
                    {
                        "lastUpdateDateTime": "2026-09-27T11:30:00Z",
                        "electric": {
                            "battery": {"stateOfChargeDisplay": {"value": 50}},
                            "charging": {"chargingState": "45"},
                        },
                    }
                )
                primary = {**self.status, "lastUpdateDateTime": "2026-09-27T12:00:00Z"}
                fallback = {
                    "lastUpdateDateTime": fallback_time,
                    "electric": self.electric,
                    "telemetry": {"odo": {"value": 999}},
                }
                self.session.post.side_effect = [
                    _Response(200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}),
                    _Response(200, {"data": {"getVehicleStatus": fallback}}),
                ]
                await vehicle.update()
                self.assertEqual(50, vehicle.features[VehicleFeatures.ChargeLevel].value)
                self.assertTrue(vehicle.features[VehicleFeatures.ChargingStatus].closed)
                self.assertEqual(1234, vehicle.features[VehicleFeatures.Odometer].value)
                vehicle.apply_graphql_status(
                    {
                        "lastUpdateDateTime": "2026-09-27T11:45:00Z",
                        "telemetry": {"odo": {"value": 999}},
                    }
                )
                self.assertEqual(1234, vehicle.features[VehicleFeatures.Odometer].value)

    async def test_charging_recovery_preserves_primary_readings_and_source_timestamps(self):
        for cached in (False, True):
            for fallback_time, preserve_cached in (
                ("2026-09-27T11:00:00Z", True),
                (None, True),
                ("2026-09-27T13:00:00Z", False),
            ):
                with self.subTest(cached=cached, fallback_time=fallback_time):
                    vehicle = make_24mm_vehicle(self.client)
                    if cached:
                        vehicle.apply_graphql_status(
                            {
                                "lastUpdateDateTime": "2026-09-27T11:30:00Z",
                                "electric": {
                                    "battery": {"stateOfChargeDisplay": {"value": 50}},
                                    "charging": {"chargingState": "45"},
                                },
                            }
                        )
                    primary = {
                        "vin": "TESTVIN24",
                        "lastUpdateDateTime": "2026-09-27T12:00:00Z",
                        "electric": {
                            "battery": {
                                "stateOfChargeDisplay": {"value": 85},
                                "travelableDistance": {"value": 200},
                            },
                            "gasoline": {"travelableDistance": {"value": 300}},
                            "charging": None,
                        },
                    }
                    fallback = {"lastUpdateDateTime": fallback_time, "electric": self.electric}
                    self.session.post.reset_mock()
                    self.session.post.side_effect = [
                        _Response(
                            200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}
                        ),
                        _Response(200, {"data": {"getVehicleStatus": fallback}}),
                    ]
                    await vehicle.update()
                    self.assertEqual(2, self.session.post.call_count)
                    self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                    self.assertEqual(200, vehicle.features[VehicleFeatures.ChargeDistance].value)
                    self.assertEqual(300, vehicle.features[VehicleFeatures.GasolineRange].value)
                    self.assertEqual(
                        cached and preserve_cached,
                        vehicle.features[VehicleFeatures.ChargingStatus].closed,
                    )
                    vehicle.apply_graphql_status(
                        {
                            "lastUpdateDateTime": "2026-09-27T11:45:00Z",
                            "electric": {
                                "battery": {"stateOfChargeDisplay": {"value": 10}},
                            },
                        }
                    )
                    self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                    latest = (
                        "2026-09-27T12:00:00+00:00"
                        if preserve_cached
                        else "2026-09-27T13:00:00+00:00"
                    )
                    self.assertEqual(
                        latest,
                        vehicle._feature_timestamps[
                            (VehicleFeatures.LastTimeStamp, "value")
                        ].isoformat(),
                    )

    async def test_failed_charging_recovery_keeps_valid_battery_data(self):
        primary = {
            "electric": {"battery": {"stateOfChargeDisplay": {"value": 85}}, "charging": None}
        }
        for fallback in (
            {"electric": None},
            {"electric": {"charging": None}},
            {"electric": {"battery": {"stateOfChargeDisplay": {"value": 10}}}},
        ):
            with self.subTest(fallback=fallback):
                vehicle = make_24mm_vehicle(self.client)
                self.session.post.reset_mock()
                self.session.post.side_effect = [
                    _Response(200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}),
                    _Response(200, {"data": {"getVehicleStatus": fallback}}),
                ]
                await vehicle.update()
                self.assertEqual(2, self.session.post.call_count)
                self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                self.assertNotIn(VehicleFeatures.ChargingStatus, vehicle.features)
