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
            F.FuelLevel: ToyotaNumeric(79, "%"),
        }
        coordinator = ha.DataUpdateCoordinator([self.vehicle])
        entities = []
        await ha.sensor_platform.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        coordinator.notify_listeners()
        self.assertEqual(len(entities), 6)
        self.entities = {entity.sensor_name: entity for entity in entities}
        self.assertEqual(set(self.entities), {
            "Plug Status", "Connector Status", "Last Update Timestamp",
            "Last Tire Pressure Update Timestamp", "Speed", "Fuel Level",
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

    async def test_fuel_level_clamps_display_without_changing_raw_value(self):
        entity = self.entities["Fuel Level"]
        for raw, expected in (
            (-1, 0), (0, 0), (79, 79), (79.5, 79.5),
            (100, 100), (104.0, 100), (None, None), ("75", "75"),
        ):
            with self.subTest(raw=raw):
                self.vehicle.features[F.FuelLevel] = ToyotaNumeric(raw, "%")
                self.assertEqual(entity.native_value, expected)
                self.assertEqual(self.vehicle.features[F.FuelLevel].value, raw)
        self.assertEqual(entity.native_unit_of_measurement, "%")

    async def test_speed_uses_reported_units_with_legacy_fallback(self):
        entity = self.entities["Speed"]
        self.assertEqual(entity.device_class, ha.SensorDeviceClass.SPEED)
        for unit, expected in (("km/h", "km/h"), ("mph", "mph"), ("", "km/h")):
            self.vehicle.features[F.Speed] = ToyotaNumeric(100, unit)
            self.assertEqual(entity.native_value, 100)
            self.assertEqual(entity.native_unit_of_measurement, expected)


class RemoteAccessSensorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = ha.FakeVehicle(set())
        self.vehicle.remote_display = 7
        self.other = ha.FakeVehicle(set(), vin="OTHERVIN")
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle, self.other])
        self.entities = []
        await ha.sensor_platform.async_setup_entry(
            ha.FakeHass(self.coordinator), ha.ConfigEntry(),
            lambda added, update: self.entities.extend(added),
        )
        self.assertEqual([entity.unique_id for entity in self.entities], ["TESTVIN.Remote Access"])
        self.entity = self.entities[0]

    async def test_states_follow_the_app_banners(self):
        for raw, expected in (
            (0, "unsupported"), (1, "authorization_required"),
            (2, "subscription_cancelled"), (3, "subscription_cancelled"),
            (4, "activation_failed"), (5, "activation_pending"), (6, "activation_error"),
            (7, "active"), (8, "subscription_expired"), (9, "subscription_expired"),
            (10, "stolen"), (11, "stolen_immobilizer"), (12, None), (-1, None),
        ):
            with self.subTest(raw=raw):
                self.vehicle.remote_display = raw
                self.assertTrue(self.entity.available)
                self.assertEqual(self.entity.native_value, expected)
                self.assertEqual(self.entity.extra_state_attributes, {"raw_value": raw})
                if expected is not None:
                    self.assertIn(expected, self.entity.options)
        self.assertEqual(self.entity.device_class, ha.SensorDeviceClass.ENUM)
        self.assertEqual(self.entity._attr_entity_category, "diagnostic")

    async def test_unavailable_when_the_vehicle_list_drops_the_state(self):
        self.vehicle.remote_display = None
        self.assertFalse(self.entity.available)
        self.assertIsNone(self.entity.native_value)
        self.assertIsNone(self.entity.extra_state_attributes)

    async def test_added_once_a_vehicle_reports_the_state(self):
        self.other.remote_display = 1
        self.coordinator.notify_listeners()
        self.assertEqual(
            [entity.unique_id for entity in self.entities],
            ["TESTVIN.Remote Access", "OTHERVIN.Remote Access"],
        )
