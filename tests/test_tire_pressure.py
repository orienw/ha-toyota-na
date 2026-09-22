"""Legacy tire readings share entities and source ordering with AppSync."""

from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock

from toyota_na.exceptions import LoginError

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import binary_sensor
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, VehicleFeatures


TIRES = {
    "vin": "TESTVIN", "tirePressureTimestamp": "2026-09-21T12:00:00Z",
    "flTirePressure": {"value": 210, "unit": "kPa", "displayLowTirePressureWarning": True},
    "frTirePressure": {"value": 35, "unit": "psi", "displayLowTirePressureWarning": False},
    "rlTirePressure": {"displayLowTirePressureWarning": None},
    "rrTirePressure": {"displayLowTirePressureWarning": "false"},
    "spareTirePressure": {"displayLowTirePressureWarning": True},
}


class TirePressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_reads_create_reported_warning_entities_without_a_subscription(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_vehicle):
            with self.subTest(factory=factory.__name__):
                client = types.SimpleNamespace(get_tire_pressure=AsyncMock(return_value=deepcopy(TIRES)))
                vehicle = factory(client)
                vehicle._has_remote_subscription = False
                await vehicle.update_tire_pressure()
                client.get_tire_pressure.assert_awaited_once_with(
                    vehicle.vin, vehicle.api_generation, vehicle.region, vehicle.brand,
                )
                entities = []
                coordinator = ha.DataUpdateCoordinator([vehicle])
                await binary_sensor.async_setup_entry(
                    ha.FakeHass(coordinator), ha.ConfigEntry(), lambda added, update: entities.extend(added),
                )
                by_name = {entity.sensor_name: entity for entity in entities}
                self.assertTrue(by_name["Front Driver Tire Pressure Warning"].is_on)
                self.assertFalse(by_name["Front Passenger Tire Pressure Warning"].is_on)
                self.assertTrue(by_name["Spare Tire Pressure Warning"].is_on)
                self.assertNotIn("Rear Driver Tire Pressure Warning", by_name)
                self.assertNotIn("Rear Passenger Tire Pressure Warning", by_name)
                self.assertEqual(210, vehicle.features[VehicleFeatures.FrontDriverTire].value)
                self.assertEqual("kPa", vehicle.features[VehicleFeatures.FrontDriverTire].unit)

    async def test_appsync_cars_do_not_make_the_extra_rest_request(self):
        for generation in (ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            client = types.SimpleNamespace(get_tire_pressure=AsyncMock())
            vehicle = behavior.make_24mm_vehicle(client)
            vehicle._generation = generation
            await vehicle.update_tire_pressure()
            client.get_tire_pressure.assert_not_awaited()

    def test_missing_malformed_or_older_readings_preserve_known_warnings(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_vehicle):
            with self.subTest(factory=factory.__name__):
                previous = factory()
                previous._parse_tire_pressure(TIRES)
                vehicle = factory()
                vehicle.inherit_state(previous)
                for payload in (
                    None, [], {}, {"flTirePressure": []},
                    {**TIRES, "vin": "OTHER"},
                    {"tirePressureTimestamp": "2026-09-21T11:00:00Z", "flTirePressure": {"value": 35, "displayLowTirePressureWarning": False}},
                    {"flTirePressure": {"value": 35, "displayLowTirePressureWarning": False}},
                    {"tirePressureTimestamp": "2026-09-21T13:00:00Z", "flTirePressure": {"value": None, "displayLowTirePressureWarning": "false"}},
                ):
                    vehicle._parse_tire_pressure(payload)
                    self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
                    self.assertEqual(210, vehicle.features[VehicleFeatures.FrontDriverTire].value)
                vehicle._parse_tire_pressure({
                    "tirePressureTimestamp": "2026-09-21T14:00:00Z",
                    "flTirePressure": {"displayLowTirePressureWarning": False},
                })
                self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)

    def test_rest_and_push_warnings_share_timestamps_and_entity_features(self):
        vehicle = behavior.make_vehicle()
        vehicle.apply_graphql_status({"vehicleState": {"tires": {
            "lastUpdateDateTime": "2026-09-21T13:00:00Z",
            "frontLeft": {"psi": 35, "displayLowTirePressureWarning": False},
        }}})
        vehicle._parse_tire_pressure(TIRES)
        self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
        self.assertEqual(35, vehicle.features[VehicleFeatures.FrontDriverTire].value)
        vehicle._parse_tire_pressure({**TIRES, "tirePressureTimestamp": "2026-09-21T14:00:00Z"})
        vehicle.apply_graphql_status({"vehicleState": {"tires": {
            "lastUpdateDateTime": "2026-09-21T13:30:00Z",
            "frontLeft": {"displayLowTirePressureWarning": False},
        }}})
        self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)

    async def test_poll_reads_tires_and_tolerates_endpoint_failure_but_propagates_auth_failure(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_vehicle):
            with self.subTest(factory=factory.__name__):
                client = types.SimpleNamespace(**{
                    name: AsyncMock(return_value={}) for name in (
                        "get_vehicle_status_17cy", "get_vehicle_status_21mm", "get_electric_status",
                        "get_engine_status_17cy", "get_engine_status_21mm", "get_climate_settings", "get_telemetry",
                    )
                }, get_tire_pressure=AsyncMock(return_value=deepcopy(TIRES)))
                vehicle = factory(client)
                await vehicle.update()
                self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
                client.get_tire_pressure.side_effect = RuntimeError("Not available")
                await vehicle.update()
                self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverTireWarning].closed)
                client.get_tire_pressure.side_effect = LoginError()
                with self.assertRaises(LoginError):
                    await vehicle.update()
