"""Additional returned measurements, with units and source ordering."""

from copy import deepcopy
import unittest

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import sensor
from custom_components.toyota_na import binary_sensor
from custom_components.toyota_na.patch_base_vehicle import VehicleFeatures


STATUS = {
    "telemetry": {
        "lastUpdateDateTime": "2026-09-14T12:00:00Z",
        "totalAverageFuelConsumption": {"value": 5.4, "unit": "L/100km"},
        "averageFuelConsumptionSinceStart": {"value": 6.1, "unit": "L/100km"},
    },
    "tripdetails": {"tripCount": {"value": 4}},
    "electric": {
        "lastUpdateDateTime": "2026-09-14T12:00:00Z",
        "battery": {"powerSupplyPossibleTime": {"value": 3, "unit": "h"}},
        "gasoline": {
            "powerSupplyPossibleTime": {"value": 8, "unit": "h"},
            "travelableDistance": {"value": 120, "unit": "km"},
        },
        "charging": {
            "remainingChargeTimeTo80Percent": {"value": 0, "unit": "min"},
            "chargeSettings": {"targetLimit": {"value": 80, "unit": "%"}},
        },
    },
}


class AdditionalTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_hatch_and_tire_warnings_use_reported_states_without_pressure(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle._has_remote_subscription = False
        vehicle.apply_graphql_status({
            "vehicleState": {
                "glassHatch": {"position": {"status": "Open"}},
                "tires": {
                    "frontLeft": {"displayLowTirePressureWarning": True},
                    "frontRight": {"displayLowTirePressureWarning": False},
                    "rearLeft": {"displayLowTirePressureWarning": None},
                    "rearRight": {"displayLowTirePressureWarning": "unknown"},
                },
            },
        })
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await binary_sensor.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        entities = {entity.sensor_name: entity for entity in entities}
        self.assertTrue(entities["Glass Hatch"].is_on)
        self.assertTrue(entities["Front Driver Tire Pressure Warning"].is_on)
        self.assertFalse(entities["Front Passenger Tire Pressure Warning"].is_on)
        self.assertNotIn("Rear Driver Tire Pressure Warning", entities)
        self.assertNotIn("Rear Passenger Tire Pressure Warning", entities)
        vehicle.apply_graphql_status({"vehicleState": {"glassHatch": {"position": {"status": None}}}})
        self.assertTrue(entities["Glass Hatch"].is_on)

    async def test_charging_rate_retains_precision_and_actual_unit(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status({"electric": {"charging": {
            "actualChargingRate": {"value": 7.25, "unit": "kW"},
        }}})
        measurement = vehicle.features[VehicleFeatures.ChargingRate]
        self.assertEqual(7.25, measurement.value)
        self.assertEqual("kW", measurement.unit)

    async def test_returned_measurements_are_discovered_without_subscription(self):
        vehicle = behavior.make_vehicle()
        vehicle._has_electric = True
        vehicle._has_remote_subscription = False
        vehicle._feature_flags = {"electric": 2, "vehicleState": 0}
        vehicle.apply_graphql_status(deepcopy(STATUS))
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await sensor.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        entities = {entity.sensor_name: entity for entity in entities}
        for name, value, unit in (
            ("Average Fuel Consumption", 5.4, "L/100km"),
            ("Trip Fuel Consumption", 6.1, "L/100km"),
            ("Trip Count", 4, None),
            ("Battery Power Supply Time", 3, "h"),
            ("Gasoline Power Supply Time", 8, "h"),
            ("Gasoline Range", 120, "km"),
            ("Remaining Charge Time to 80%", 0, "min"),
            ("Charge Target", 80, "%"),
        ):
            with self.subTest(name=name):
                self.assertTrue(entities[name].available)
                self.assertEqual(value, entities[name].state)
                self.assertEqual(unit, entities[name].unit_of_measurement)

    async def test_partial_and_older_updates_keep_observed_values(self):
        vehicle = behavior.make_vehicle()
        vehicle.apply_graphql_status(deepcopy(STATUS))
        vehicle.apply_graphql_status({"electric": {"charging": {"chargeSettings": {}}}})
        older = deepcopy(STATUS)
        older["electric"]["lastUpdateDateTime"] = "2026-09-14T11:00:00Z"
        older["electric"]["charging"]["chargeSettings"]["targetLimit"]["value"] = 90
        older["telemetry"]["lastUpdateDateTime"] = "2026-09-14T11:00:00Z"
        older["telemetry"]["totalAverageFuelConsumption"]["value"] = 9.8
        vehicle.apply_graphql_status(older)
        self.assertEqual(80, vehicle.features[VehicleFeatures.ChargeTargetLimit].value)
        self.assertEqual(5.4, vehicle.features[VehicleFeatures.AverageFuelConsumption].value)

    async def test_gasoline_supply_data_does_not_require_charging_section(self):
        vehicle = behavior.make_vehicle()
        status = deepcopy(STATUS)
        del status["electric"]["charging"]
        vehicle.apply_graphql_status(status)
        self.assertEqual(8, vehicle.features[VehicleFeatures.GasolinePowerSupplyTime].value)

    async def test_electric_observations_use_root_timestamp_when_section_has_none(self):
        vehicle = behavior.make_24mm_vehicle()
        for timestamp, value in (("12:00:00", 80), ("12:10:00", 81), ("12:05:00", 79)):
            vehicle.apply_graphql_status({
                "lastUpdateDateTime": f"2026-09-14T{timestamp}Z",
                "electric": {"battery": {"stateOfChargeDisplay": {"value": value, "unit": "%"}}},
            })
        self.assertEqual(81, vehicle.features[VehicleFeatures.ChargeLevel].value)
