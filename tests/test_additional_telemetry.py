"""Additional returned measurements, with units and source ordering."""

import unittest
from copy import deepcopy
from datetime import UTC, datetime

import pytest
from common import entity_id, make_17cy_vehicle, make_24mm_vehicle, make_vehicle
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening

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


@pytest.mark.parametrize("section", ["Back window", "Glass Hatch"])
@pytest.mark.parametrize("factory", [make_17cy_vehicle, make_vehicle])
async def test_legacy_glass_hatch_reports_position_without_adding_a_lock(
    hass, setup_vehicles, factory, section
):
    vehicle = factory()
    vehicle._has_remote_subscription = False

    def update(timestamp, values):
        vehicle._parse_vehicle_status(
            {
                "occurrenceDate": f"2026-09-15T{timestamp}Z",
                "vehicleStatus": [
                    {
                        "category": "Other",
                        "sections": [
                            {
                                "section": section,
                                "values": values,
                            }
                        ],
                    }
                ],
            }
        )

    update("12:00:00", [{"value": "unknown"}])
    assert VehicleFeatures.GlassHatch not in vehicle.features
    update("12:02:00", [{"value": "open"}, {"value": "unlocked", "status": 1}])
    update("12:01:00", [{"value": "closed"}])
    update("12:03:00", [])
    assert vehicle.features[VehicleFeatures.GlassHatch].closed is False
    assert not isinstance(vehicle.features[VehicleFeatures.GlassHatch], ToyotaLockableOpening)

    await setup_vehicles([vehicle])

    assert hass.states.get(entity_id(hass, "binary_sensor", "TESTVIN.Glass Hatch")).state == "on"


@pytest.mark.parametrize(
    ("extra", "unit"),
    [
        ({"evDistanceUnit": "km"}, "km"),
        ({"evDistanceUnit": "mi"}, "mi"),
        ({"evDistanceUnit": None}, "mi"),
        ({}, "mi"),
    ],
)
@pytest.mark.parametrize("factory", [make_17cy_vehicle, make_vehicle])
async def test_rest_ranges_keep_zero_values_and_report_distance_units(
    hass, setup_vehicles, factory, extra, unit
):
    vehicle = factory()
    vehicle._has_electric = True
    vehicle._has_remote_subscription = False
    vehicle._parse_electric_status(
        {
            "vehicleInfo": {
                "chargeInfo": {
                    "evDistance": 24,
                    "evDistanceAC": 20,
                    "evTravelableDistance": 24,
                    "gasolineTravelableDistance": 0,
                    **extra,
                }
            }
        }
    )

    await setup_vehicles([vehicle])

    for name, value in (
        ("EV Range", "24"),
        ("EV Range AC", "20"),
        ("EV Travelable Distance", "24"),
        ("Gasoline Range", "0"),
    ):
        state = hass.states.get(entity_id(hass, "sensor", f"TESTVIN.{name}"))
        assert state.state == value
        assert state.attributes["unit_of_measurement"] == unit


async def test_graphql_updates_timestamp_sensors_without_regressing_other_sections(
    hass, setup_vehicles
):
    vehicle = make_24mm_vehicle()
    vehicle.apply_graphql_status(
        {
            "lastUpdateDateTime": "2026-09-15T12:05:00Z",
            "vehicleState": {
                "lastUpdateDateTime": "2026-09-15T12:00:00Z",
                "tires": {
                    "lastUpdateDateTime": "2026-09-15T11:58:00Z",
                    "frontLeft": {"psi": 35},
                },
            },
            "telemetry": {"lastUpdateDateTime": "2026-09-15T12:03:00Z", "odo": {"value": 123}},
        }
    )
    vehicle._parse_telemetry(
        {
            "lastTimestamp": "2026-09-15T12:01:00Z",
            "tirePressureTimestamp": "2026-09-15T11:57:00Z",
        }
    )
    account = await setup_vehicles([vehicle])
    updated = entity_id(hass, "sensor", "TESTVIN24.Last Update Timestamp")
    tire_updated = entity_id(hass, "sensor", "TESTVIN24.Last Tire Pressure Update Timestamp")
    assert hass.states.get(updated).state == datetime(2026, 9, 15, 12, 5, tzinfo=UTC).isoformat()
    assert (
        hass.states.get(tire_updated).state == datetime(2026, 9, 15, 11, 58, tzinfo=UTC).isoformat()
    )

    account.websocket.on_status(
        "TESTVIN24",
        {
            "location": {
                "lastUpdateDateTime": "2026-09-15T12:10:00Z",
                "latitude": 1,
                "longitude": 2,
            }
        },
    )
    account.websocket.on_status(
        "TESTVIN24",
        {
            "lastUpdateDateTime": "2026-09-15T12:04:00Z",
            "vehicleState": {
                "tires": {
                    "lastUpdateDateTime": "2026-09-15T12:02:00Z",
                    "frontLeft": {"psi": 36},
                }
            },
        },
    )
    await hass.async_block_till_done()
    # The next poll builds a new vehicle that inherits the pushed state.
    replacement = make_24mm_vehicle()
    replacement.inherit_state(vehicle)
    replacement._parse_telemetry({"lastTimestamp": "2026-09-15T12:07:00Z"})
    account.get_vehicles.return_value = [replacement]
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(updated).state == datetime(2026, 9, 15, 12, 10, tzinfo=UTC).isoformat()
    assert (
        hass.states.get(tire_updated).state == datetime(2026, 9, 15, 12, 2, tzinfo=UTC).isoformat()
    )


async def test_hatch_and_tire_warnings_use_reported_states_without_pressure(hass, setup_vehicles):
    vehicle = make_24mm_vehicle()
    vehicle._has_remote_subscription = False
    vehicle.apply_graphql_status(
        {
            "vehicleState": {
                "glassHatch": {"position": {"status": "Open"}},
                "tires": {
                    "frontLeft": {"displayLowTirePressureWarning": True},
                    "frontRight": {"displayLowTirePressureWarning": False},
                    "rearLeft": {"displayLowTirePressureWarning": None},
                    "rearRight": {"displayLowTirePressureWarning": "unknown"},
                },
            },
        }
    )
    account = await setup_vehicles([vehicle])

    def binary_sensor(name):
        return entity_id(hass, "binary_sensor", f"TESTVIN24.{name}")

    assert hass.states.get(binary_sensor("Glass Hatch")).state == "on"
    assert hass.states.get(binary_sensor("Front Driver Tire Pressure Warning")).state == "on"
    assert hass.states.get(binary_sensor("Front Passenger Tire Pressure Warning")).state == "off"
    assert binary_sensor("Rear Driver Tire Pressure Warning") is None
    assert binary_sensor("Rear Passenger Tire Pressure Warning") is None
    account.websocket.on_status(
        "TESTVIN24", {"vehicleState": {"glassHatch": {"position": {"status": None}}}}
    )
    await hass.async_block_till_done()
    assert hass.states.get(binary_sensor("Glass Hatch")).state == "on"


async def test_returned_measurements_are_discovered_without_subscription(
    hass, setup_vehicles, subtests
):
    vehicle = make_vehicle()
    vehicle._has_electric = True
    vehicle._has_remote_subscription = False
    vehicle._feature_flags = {"electric": 2, "vehicleState": 0}
    vehicle.apply_graphql_status(deepcopy(STATUS))

    await setup_vehicles([vehicle])

    for name, value, unit in (
        ("Average Fuel Consumption", "5.4", "L/100km"),
        ("Trip Fuel Consumption", "6.1", "L/100km"),
        ("Trip Count", "4", None),
        ("Battery Power Supply Time", "3", "h"),
        ("Gasoline Power Supply Time", "8", "h"),
        ("Gasoline Range", "120", "km"),
        ("Remaining Charge Time to 80%", "0", "min"),
        ("Charge Target", "80", "%"),
    ):
        with subtests.test(name=name):
            state = hass.states.get(entity_id(hass, "sensor", f"TESTVIN.{name}"))
            assert state.state != "unavailable"
            assert state.state == value
            assert state.attributes.get("unit_of_measurement") == unit


class AdditionalTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_tire_section_does_not_invent_a_tire_timestamp(self):
        vehicle = make_24mm_vehicle()
        vehicle.apply_graphql_status(
            {"vehicleState": {"lastUpdateDateTime": "2026-09-15T12:00:00Z", "tires": None}}
        )
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
                    vehicle = make_24mm_vehicle()
                    update = {**deepcopy(status), section: malformed}
                    original = deepcopy(update)

                    self.assertTrue(vehicle.apply_graphql_status(update))

                    for key, (feature, attribute, expected) in readings.items():
                        if key == section:
                            self.assertNotIn(feature, vehicle.features)
                        else:
                            self.assertEqual(
                                expected, getattr(vehicle.features[feature], attribute)
                            )
                    self.assertEqual(
                        datetime(2026, 9, 15, 12, tzinfo=UTC).timestamp(),
                        vehicle.features[VehicleFeatures.LastTimeStamp].value,
                    )
                    self.assertEqual(original, update)
                    vehicle._parse_graphql_vehicle_status(vehicle._last_graphql_status)

    async def test_unusable_graphql_updates_preserve_cached_state(self):
        vehicle = make_24mm_vehicle()
        vehicle.apply_graphql_status({"telemetry": {"odo": {"value": 123}}})
        cached = vehicle._last_graphql_status
        features = vehicle.features.copy()
        for malformed in (None, [], {}, False, True, 7, "invalid", ["invalid"]):
            for update in (malformed, {"telemetry": malformed}):
                with self.subTest(update=update):
                    self.assertFalse(vehicle.apply_graphql_status(update))
                    self.assertIs(cached, vehicle._last_graphql_status)
                    self.assertEqual(features, vehicle.features)

        self.assertTrue(
            vehicle.apply_graphql_status(
                {
                    "telemetry": ["invalid"],
                    "location": {"latitude": 1, "longitude": 2},
                }
            )
        )
        self.assertEqual(123, vehicle.features[VehicleFeatures.Odometer].value)
        self.assertEqual(1, vehicle.features[VehicleFeatures.ParkingLocation].lat)

    async def test_charging_rate_retains_precision_and_actual_unit(self):
        vehicle = make_24mm_vehicle()
        vehicle.apply_graphql_status(
            {
                "electric": {
                    "charging": {
                        "actualChargingRate": {"value": 7.25, "unit": "kW"},
                    }
                }
            }
        )
        measurement = vehicle.features[VehicleFeatures.ChargingRate]
        self.assertEqual(7.25, measurement.value)
        self.assertEqual("kW", measurement.unit)

    async def test_partial_and_older_updates_keep_observed_values(self):
        vehicle = make_vehicle()
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
        vehicle = make_vehicle()
        status = deepcopy(STATUS)
        del status["electric"]["charging"]
        vehicle.apply_graphql_status(status)
        self.assertEqual(8, vehicle.features[VehicleFeatures.GasolinePowerSupplyTime].value)

    async def test_electric_observations_use_root_timestamp_when_section_has_none(self):
        vehicle = make_24mm_vehicle()
        for timestamp, value in (("12:00:00", 80), ("12:10:00", 81), ("12:05:00", 79)):
            vehicle.apply_graphql_status(
                {
                    "lastUpdateDateTime": f"2026-09-14T{timestamp}Z",
                    "electric": {
                        "battery": {"stateOfChargeDisplay": {"value": value, "unit": "%"}}
                    },
                }
            )
        self.assertEqual(81, vehicle.features[VehicleFeatures.ChargeLevel].value)
