"""Readable sensor states retain the original raw entities."""

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
        self.assertEqual(len(entities), 9)
        self.entities = {entity.sensor_name: entity for entity in entities}

    async def test_plug_states_cover_legacy_and_appsync_values(self):
        entity = self.entities["Plug State"]
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
                self.assertEqual(self.entities["Plug Status"].native_value, raw)
                if expected is not None:
                    self.assertIn(expected, entity.options)
        self.assertIsNone(entity.state_class)
        self.assertIsNone(entity.native_unit_of_measurement)
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.ENUM)

    async def test_connector_states_leave_unrecognized_codes_unknown(self):
        entity = self.entities["Connector State"]
        for raw, expected in (
            (2, "disconnected"), (4, "unlocked"), (5, "locked"),
            ("connected", "connected"), ("LOCKED", "locked"), (987, None),
        ):
            with self.subTest(raw=raw):
                self.vehicle.features[F.ConnectorStatus] = ToyotaNumeric(raw, "")
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(self.entities["Connector Status"].native_value, raw)
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.ENUM)
        self.assertIsNone(entity.state_class)

    async def test_timestamps_are_timezone_aware_and_raw_ids_are_preserved(self):
        for name, hour in (("Last Update", 12), ("Last Tire Pressure Update", 11)):
            with self.subTest(name=name):
                entity = self.entities[name]
                expected = datetime(2026, 9, 15, hour, tzinfo=timezone.utc)
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(entity.device_class, ha.SensorDeviceClass.TIMESTAMP)
                self.assertIsNone(entity.native_unit_of_measurement)
                self.assertIsNone(entity.state_class)
                raw = self.entities[f"{name} Timestamp"]
                self.assertEqual(raw.native_value, expected.timestamp())
                self.assertEqual(raw.unique_id, f"TESTVIN.{name} Timestamp")

        for name in ("Plug Status", "Connector Status", "Last Update Timestamp", "Last Tire Pressure Update Timestamp"):
            entity = self.entities[name]
            self.assertEqual(entity.unique_id, f"TESTVIN.{name}")
            self.assertFalse(entity.entity_registry_enabled_default)
        self.assertTrue(self.entities["Plug State"].entity_registry_enabled_default)

    async def test_speed_uses_reported_units_with_legacy_fallback(self):
        entity = self.entities["Speed"]
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.SPEED)
        for unit, expected in (("km/h", "km/h"), ("mph", "mph"), ("", "km/h")):
            self.vehicle.features[F.Speed] = ToyotaNumeric(100, unit)
            self.assertEqual(entity.native_value, 100)
            self.assertEqual(entity.native_unit_of_measurement, expected)
