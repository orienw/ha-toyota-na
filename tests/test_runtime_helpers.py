"""Tests for config-entry runtime coordination helpers."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/toyota_na"


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, COMPONENT / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


entity_discovery = load_module("test_entity_discovery", "entity_discovery.py")
vehicle_claims = load_module("test_vehicle_claims", "vehicle_claims.py")


class Entity:
    def __init__(self, unique_id):
        self.unique_id = unique_id


class Coordinator:
    def __init__(self):
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def remove_listener():
            self.listeners.remove(listener)

        return remove_listener

    def notify(self):
        for listener in list(self.listeners):
            listener()


class ConfigEntry:
    def __init__(self):
        self.unload_callbacks = []

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)


class Vehicle:
    def __init__(self, vin):
        self.vin = vin


class EntityDiscoveryTests(unittest.TestCase):
    def test_adds_new_entities_once_and_unregisters_listener(self):
        coordinator = Coordinator()
        config_entry = ConfigEntry()
        available_ids = ["first"]
        added = []

        entity_discovery.setup_entity_discovery(
            config_entry,
            coordinator,
            lambda entities, update_before_add: added.extend(entities),
            lambda: [Entity(unique_id) for unique_id in available_ids],
        )

        self.assertEqual([entity.unique_id for entity in added], ["first"])
        available_ids.append("second")
        coordinator.notify()
        coordinator.notify()
        self.assertEqual(
            [entity.unique_id for entity in added],
            ["first", "second"],
        )

        config_entry.unload_callbacks[0]()
        self.assertEqual(coordinator.listeners, [])


class VehicleClaimTests(unittest.TestCase):
    def test_only_one_entry_can_claim_a_shared_vin(self):
        claims = {}
        vehicle = Vehicle("SHAREDVIN")

        first, first_conflicts = vehicle_claims.claim_vehicles(
            claims, "first-entry", [vehicle]
        )
        second, second_conflicts = vehicle_claims.claim_vehicles(
            claims, "second-entry", [vehicle]
        )

        self.assertEqual(first, [vehicle])
        self.assertEqual(first_conflicts, [])
        self.assertEqual(second, [])
        self.assertEqual(second_conflicts, [vehicle])
        self.assertEqual(claims, {"SHAREDVIN": "first-entry"})

    def test_release_allows_another_entry_to_claim_vehicle(self):
        claims = {"SHAREDVIN": "first-entry", "OTHERVIN": "other-entry"}
        vehicle_claims.release_vehicle_claims(claims, "first-entry")

        claimed, conflicts = vehicle_claims.claim_vehicles(
            claims, "second-entry", [Vehicle("SHAREDVIN")]
        )

        self.assertEqual([vehicle.vin for vehicle in claimed], ["SHAREDVIN"])
        self.assertEqual(conflicts, [])
        self.assertEqual(
            claims,
            {
                "OTHERVIN": "other-entry",
                "SHAREDVIN": "second-entry",
            },
        )


if __name__ == "__main__":
    unittest.main()
