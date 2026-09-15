"""Readable sensor states retain the existing entity IDs."""

from datetime import datetime, timezone
import unittest

import test_button as ha
from custom_components.toyota_na.patch_base_vehicle import VehicleFeatures as F
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric


class SensorStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = ha.FakeVehicle(set())
        self.vehicle.electric = True
        self.vehicle.features = {
            F.PlugStatus: ToyotaNumeric(40, ""),
            F.ConnectorStatus: ToyotaNumeric(5, ""),
            F.LastTimeStamp: ToyotaNumeric(1789473600, ""),
            F.LastTirePressureTimeStamp: ToyotaNumeric(1789470000, ""),
            F.Speed: ToyotaNumeric(100, "km/h"),
        }
        coordinator = ha.DataUpdateCoordinator([self.vehicle])
        entities = []
        await ha.sensor_platform.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        coordinator.notify_listeners()
        self.assertEqual(len(entities), 5)
        self.entities = {entity.sensor_name: entity for entity in entities}
        self.assertEqual(set(self.entities), {
            "Plug Status", "Connector Status", "Last Update Timestamp",
            "Last Tire Pressure Update Timestamp", "Speed",
        })
        for name, entity in self.entities.items():
            self.assertEqual(entity.unique_id, f"TESTVIN.{name}")
            self.assertTrue(entity.entity_registry_enabled_default)

    async def test_plug_states_cover_legacy_and_appsync_values(self):
        entity = self.entities["Plug Status"]
        for raw, expected in (
            (12, "unplugged"), (36, "waiting"), (40, "charging"),
            (45, "charge_complete"), (56, "fast_charging"), (60, "fast_charge_complete"),
            ("PLUGGED_IN", "plugged_in"), ("charging", "charging"),
            ("charge_now", "waiting"), ("resume_charging", "paused"),
            ("external_power_active", "power_supply"),
            ("external_power_active_hybrid", "power_supply"),
            ("no_controls", "unplugged"), ("unavailable", "unplugged"),
            (987, None), (None, None),
        ):
            with self.subTest(raw=raw):
                self.vehicle.features[F.PlugStatus] = ToyotaNumeric(raw, "")
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(entity.extra_state_attributes, {"raw_value": raw})
                if expected is not None:
                    self.assertIn(expected, entity.options)
        self.assertIsNone(entity.state_class)
        self.assertIsNone(entity.native_unit_of_measurement)
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.ENUM)

    async def test_connector_states_leave_unrecognized_codes_unknown(self):
        entity = self.entities["Connector Status"]
        for raw, expected in (
            (2, "disconnected"), (4, "unlocked"), (5, "locked"),
            ("connected", "connected"), ("LOCKED", "locked"), (987, None),
        ):
            with self.subTest(raw=raw):
                self.vehicle.features[F.ConnectorStatus] = ToyotaNumeric(raw, "")
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(entity.extra_state_attributes, {"raw_value": raw})
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.ENUM)
        self.assertIsNone(entity.state_class)

    async def test_timestamps_are_timezone_aware(self):
        for name, hour in (("Last Update Timestamp", 12), ("Last Tire Pressure Update Timestamp", 11)):
            with self.subTest(name=name):
                entity = self.entities[name]
                expected = datetime(2026, 9, 15, hour, tzinfo=timezone.utc)
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(entity.device_class, ha.SensorDeviceClass.TIMESTAMP)
                self.assertIsNone(entity.native_unit_of_measurement)
                self.assertIsNone(entity.state_class)

    async def test_speed_uses_reported_units_with_legacy_fallback(self):
        entity = self.entities["Speed"]
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.SPEED)
        for unit, expected in (("km/h", "km/h"), ("mph", "mph"), ("", "km/h")):
            self.vehicle.features[F.Speed] = ToyotaNumeric(100, unit)
            self.assertEqual(entity.native_value, 100)
            self.assertEqual(entity.native_unit_of_measurement, expected)
