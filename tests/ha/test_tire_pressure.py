"""Legacy tire readings share entities and source ordering with AppSync."""

import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from common import entity_id, make_17cy_vehicle, make_24mm_vehicle, make_vehicle
from toyota_na.exceptions import LoginError

from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, VehicleFeatures

TIRES = {
    "vin": "TESTVIN",
    "tirePressureTimestamp": "2026-09-21T12:00:00Z",
    "flTirePressure": {"value": 210, "unit": "kPa", "displayLowTirePressureWarning": True},
    "frTirePressure": {"value": 35, "unit": "psi", "displayLowTirePressureWarning": False},
    "rlTirePressure": {"displayLowTirePressureWarning": None},
    "rrTirePressure": {"displayLowTirePressureWarning": "false"},
    "spareTirePressure": {"displayLowTirePressureWarning": True},
}


@pytest.mark.parametrize("factory", [make_17cy_vehicle, make_vehicle])
async def test_legacy_reads_create_reported_warning_entities_without_a_subscription(
    hass, setup_vehicles, factory
):
    client = types.SimpleNamespace(get_tire_pressure=AsyncMock(return_value=deepcopy(TIRES)))
    vehicle = factory(client)
    vehicle._has_remote_subscription = False
    await vehicle.update_tire_pressure()
    client.get_tire_pressure.assert_awaited_once_with(
        vehicle.vin,
        vehicle.api_generation,
        vehicle.region,
        vehicle.brand,
    )

    await setup_vehicles([vehicle])

    def warning(name):
        return entity_id(hass, "binary_sensor", f"TESTVIN.{name} Tire Pressure Warning")

    assert hass.states.get(warning("Front Driver")).state == "on"
    assert hass.states.get(warning("Front Passenger")).state == "off"
    assert hass.states.get(warning("Spare")).state == "on"
    assert warning("Rear Driver") is None
    assert warning("Rear Passenger") is None
    assert vehicle.features[VehicleFeatures.FrontDriverTire].value == 210
    assert vehicle.features[VehicleFeatures.FrontDriverTire].unit == "kPa"


class TirePressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_appsync_cars_do_not_make_the_extra_rest_request(self):
        for generation in (ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            client = types.SimpleNamespace(get_tire_pressure=AsyncMock())
            vehicle = make_24mm_vehicle(client)
            vehicle._generation = generation
            await vehicle.update_tire_pressure()
            client.get_tire_pressure.assert_not_awaited()

    async def test_rest_request_follows_the_tire_pressure_feature_like_toyotas_app(self):
        for flags, requested in (
            (None, True),
            ({"tirePressure": 1}, True),
            ({"tirePressure": 0}, False),
            ({}, False),
        ):
            with self.subTest(flags=flags):
                client = types.SimpleNamespace(get_tire_pressure=AsyncMock(return_value=None))
                vehicle = make_vehicle(client)
                vehicle._feature_flags = flags
                await vehicle.update_tire_pressure()
                self.assertEqual(requested, client.get_tire_pressure.await_count == 1)

    def test_missing_malformed_or_older_readings_preserve_known_warnings(self):
        for factory in (make_17cy_vehicle, make_vehicle):
            with self.subTest(factory=factory.__name__):
                previous = factory()
                previous._parse_tire_pressure(TIRES)
                vehicle = factory()
                vehicle.inherit_state(previous)
                for payload in (
                    None,
                    [],
                    {},
                    {"flTirePressure": []},
                    {**TIRES, "vin": "OTHER"},
                    {
                        "tirePressureTimestamp": "2026-09-21T11:00:00Z",
                        "flTirePressure": {"value": 35, "displayLowTirePressureWarning": False},
                    },
                    {"flTirePressure": {"value": 35, "displayLowTirePressureWarning": False}},
                    {
                        "tirePressureTimestamp": "2026-09-21T13:00:00Z",
                        "flTirePressure": {"value": None, "displayLowTirePressureWarning": "false"},
                    },
                ):
                    vehicle._parse_tire_pressure(payload)
                    self.assertFalse(
                        vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed
                    )
                    self.assertEqual(210, vehicle.features[VehicleFeatures.FrontDriverTire].value)
                vehicle._parse_tire_pressure(
                    {
                        "tirePressureTimestamp": "2026-09-21T14:00:00Z",
                        "flTirePressure": {"displayLowTirePressureWarning": False},
                    }
                )
                self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)

    def test_rest_and_push_warnings_share_timestamps_and_entity_features(self):
        vehicle = make_vehicle()
        vehicle.apply_graphql_status(
            {
                "vehicleState": {
                    "tires": {
                        "lastUpdateDateTime": "2026-09-21T13:00:00Z",
                        "frontLeft": {"psi": 35, "displayLowTirePressureWarning": False},
                    }
                }
            }
        )
        vehicle._parse_tire_pressure(TIRES)
        self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
        self.assertEqual(35, vehicle.features[VehicleFeatures.FrontDriverTire].value)
        vehicle._parse_tire_pressure({**TIRES, "tirePressureTimestamp": "2026-09-21T14:00:00Z"})
        vehicle.apply_graphql_status(
            {
                "vehicleState": {
                    "tires": {
                        "lastUpdateDateTime": "2026-09-21T13:30:00Z",
                        "frontLeft": {"displayLowTirePressureWarning": False},
                    }
                }
            }
        )
        self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)

    async def test_poll_reads_tires_and_tolerates_endpoint_failure_but_propagates_auth_failure(
        self,
    ):
        for factory in (make_17cy_vehicle, make_vehicle):
            with self.subTest(factory=factory.__name__):
                client = types.SimpleNamespace(
                    **{
                        name: AsyncMock(return_value={})
                        for name in (
                            "get_vehicle_status_17cy",
                            "get_vehicle_status_21mm",
                            "get_electric_status",
                            "get_engine_status_17cy",
                            "get_engine_status_21mm",
                            "get_climate_settings",
                            "get_telemetry",
                        )
                    },
                    get_tire_pressure=AsyncMock(return_value=deepcopy(TIRES)),
                )
                vehicle = factory(client)
                await vehicle.update()
                self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
                client.get_tire_pressure.side_effect = RuntimeError("Not available")
                await vehicle.update()
                self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
                client.get_tire_pressure.side_effect = LoginError()
                with self.assertRaises(LoginError):
                    await vehicle.update()
