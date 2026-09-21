"""Additional returned measurements, with units and source ordering."""

from copy import deepcopy
from datetime import datetime, timezone
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
    async def test_legacy_glass_hatch_reports_position_without_adding_a_lock(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_vehicle):
            for section in ("Back window", "Glass Hatch"):
                with self.subTest(factory=factory.__name__, section=section):
                    vehicle = factory()
                    vehicle._has_remote_subscription = False

                    def update(timestamp, values):
                        vehicle._parse_vehicle_status({
                            "occurrenceDate": f"2026-09-15T{timestamp}Z",
                            "vehicleStatus": [{"category": "Other", "sections": [{
                                "section": section, "values": values,
                            }]}],
                        })

                    update("12:00:00", [{"value": "unknown"}])
                    self.assertNotIn(VehicleFeatures.GlassHatch, vehicle.features)
                    update("12:02:00", [{"value": "open"}, {"value": "unlocked", "status": 1}])
                    update("12:01:00", [{"value": "closed"}])
                    update("12:03:00", [])
                    self.assertFalse(vehicle.features[VehicleFeatures.GlassHatch].closed)
                    self.assertNotIsInstance(vehicle.features[VehicleFeatures.GlassHatch], ha.ToyotaLockableOpening)
                    entities = []
                    await binary_sensor.async_setup_entry(ha.FakeHass(ha.DataUpdateCoordinator([vehicle])), ha.ConfigEntry(), lambda added, update: entities.extend(added))
                    self.assertTrue(next(entity for entity in entities if entity.sensor_name == "Glass Hatch").is_on)

    async def test_rest_ranges_keep_zero_values_and_report_distance_units(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_vehicle):
            for extra, unit in (({"evDistanceUnit": "km"}, "km"), ({"evDistanceUnit": "mi"}, "mi"), ({"evDistanceUnit": None}, "mi"), ({}, "mi")):
                with self.subTest(factory=factory.__name__, extra=extra):
                    vehicle = factory()
                    vehicle._has_electric = True
                    vehicle._has_remote_subscription = False
                    vehicle._parse_electric_status({"vehicleInfo": {"chargeInfo": {
                        "evDistance": 24, "evDistanceAC": 20,
                        "evTravelableDistance": 24, "gasolineTravelableDistance": 0, **extra,
                    }}})
                    entities = []
                    await sensor.async_setup_entry(ha.FakeHass(ha.DataUpdateCoordinator([vehicle])), ha.ConfigEntry(), lambda added, update: entities.extend(added))
                    by_name = {entity.sensor_name: entity for entity in entities}
                    for name, value in (("EV Range", 24), ("EV Range AC", 20), ("EV Travelable Distance", 24), ("Gasoline Range", 0)):
                        self.assertEqual(value, by_name[name].native_value)
                        self.assertEqual(unit, by_name[name].native_unit_of_measurement)

    async def test_graphql_updates_timestamp_sensors_without_regressing_other_sections(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status({
            "lastUpdateDateTime": "2026-09-15T12:05:00Z",
            "vehicleState": {"lastUpdateDateTime": "2026-09-15T12:00:00Z", "tires": {
                "lastUpdateDateTime": "2026-09-15T11:58:00Z", "frontLeft": {"psi": 35},
            }},
            "telemetry": {"lastUpdateDateTime": "2026-09-15T12:03:00Z", "odo": {"value": 123}},
        })
        vehicle._parse_telemetry({"lastTimestamp": "2026-09-15T12:01:00Z", "tirePressureTimestamp": "2026-09-15T11:57:00Z"})
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await sensor.async_setup_entry(ha.FakeHass(coordinator), ha.ConfigEntry(), lambda added, update: entities.extend(added))
        by_name = {entity.sensor_name: entity for entity in entities}
        updated = by_name["Last Update Timestamp"]
        tire_updated = by_name["Last Tire Pressure Update Timestamp"]
        self.assertEqual(datetime(2026, 9, 15, 12, 5, tzinfo=timezone.utc), updated.native_value)
        self.assertEqual(datetime(2026, 9, 15, 11, 58, tzinfo=timezone.utc), tire_updated.native_value)

        vehicle.apply_graphql_status({"location": {"lastUpdateDateTime": "2026-09-15T12:10:00Z", "latitude": 1, "longitude": 2}})
        vehicle.apply_graphql_status({"lastUpdateDateTime": "2026-09-15T12:04:00Z", "vehicleState": {"tires": {
            "lastUpdateDateTime": "2026-09-15T12:02:00Z", "frontLeft": {"psi": 36},
        }}})
        replacement = behavior.make_24mm_vehicle()
        replacement.inherit_state(vehicle)
        replacement._parse_telemetry({"lastTimestamp": "2026-09-15T12:07:00Z"})
        coordinator.data = [replacement]
        self.assertEqual(datetime(2026, 9, 15, 12, 10, tzinfo=timezone.utc), updated.native_value)
        self.assertEqual(datetime(2026, 9, 15, 12, 2, tzinfo=timezone.utc), tire_updated.native_value)

    async def test_missing_tire_section_does_not_invent_a_tire_timestamp(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status({"vehicleState": {"lastUpdateDateTime": "2026-09-15T12:00:00Z", "tires": None}})
        self.assertNotIn(VehicleFeatures.LastTirePressureTimeStamp, vehicle.features)

    async def test_malformed_graphql_sections_do_not_drop_other_readings(self):
        status = {
            "lastUpdateDateTime": "2026-09-15T12:00:00Z",
            "vehicleState": {"doors": {"driverSide": {"position": {"status": "close"}}}},
            "location": {"latitude": 1, "longitude": 2},
            "telemetry": {"odo": {"value": 123}},
            "electric": {"battery": {"stateOfChargeDisplay": {"value": 80}}},
            "tripdetails": {"tripCount": {"value": 3}},
        }
        readings = {
            "vehicleState": (VehicleFeatures.FrontDriverDoor, "closed", True),
            "location": (VehicleFeatures.ParkingLocation, "lat", 1),
            "telemetry": (VehicleFeatures.Odometer, "value", 123),
            "electric": (VehicleFeatures.ChargeLevel, "value", 80),
            "tripdetails": (VehicleFeatures.TripCount, "value", 3),
        }
        for section in readings:
            for malformed in (["invalid"], "invalid", 7, True, None, [], {}):
                with self.subTest(section=section, malformed=malformed):
                    vehicle = behavior.make_24mm_vehicle()
                    update = {**deepcopy(status), section: malformed}
                    original = deepcopy(update)

                    self.assertTrue(vehicle.apply_graphql_status(update))

                    for key, (feature, attribute, expected) in readings.items():
                        if key == section:
                            self.assertNotIn(feature, vehicle.features)
                        else:
                            self.assertEqual(expected, getattr(vehicle.features[feature], attribute))
                    self.assertEqual(
                        datetime(2026, 9, 15, 12, tzinfo=timezone.utc).timestamp(),
                        vehicle.features[VehicleFeatures.LastTimeStamp].value,
                    )
                    self.assertEqual(original, update)
                    vehicle._parse_graphql_vehicle_status(vehicle._last_graphql_status)

    async def test_unusable_graphql_updates_preserve_cached_state(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status({"telemetry": {"odo": {"value": 123}}})
        cached = vehicle._last_graphql_status
        features = vehicle.features.copy()
        for malformed in (None, [], {}, False, True, 7, "invalid", ["invalid"]):
            for update in (malformed, {"telemetry": malformed}):
                with self.subTest(update=update):
                    self.assertFalse(vehicle.apply_graphql_status(update))
                    self.assertIs(cached, vehicle._last_graphql_status)
                    self.assertEqual(features, vehicle.features)

        self.assertTrue(vehicle.apply_graphql_status({
            "telemetry": ["invalid"], "location": {"latitude": 1, "longitude": 2},
        }))
        self.assertEqual(123, vehicle.features[VehicleFeatures.Odometer].value)
        self.assertEqual(1, vehicle.features[VehicleFeatures.ParkingLocation].lat)

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
