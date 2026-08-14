# ruff: noqa: I001

import asyncio
from enum import Enum
from pathlib import Path
import sys
import types
import unittest

import voluptuous as vol


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "custom_components"
INTEGRATION = COMPONENTS / "toyota_na"

custom_components = sys.modules.setdefault(
    "custom_components", types.ModuleType("custom_components")
)
custom_components.__path__ = [str(COMPONENTS)]
integration = sys.modules.setdefault(
    "custom_components.toyota_na", types.ModuleType("custom_components.toyota_na")
)
integration.__path__ = [str(INTEGRATION)]


def module(name):
    value = sys.modules.setdefault(name, types.ModuleType(name))
    value.__path__ = []
    return value


module("homeassistant")
module("homeassistant.components")
module("homeassistant.helpers")


class BinarySensorDeviceClass(Enum):
    BATTERY_CHARGING = "battery_charging"
    DOOR = "door"
    LOCK = "lock"
    RUNNING = "running"
    WINDOW = "window"


class SensorStateClass(Enum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class UnitOfPressure:
    PSI = "psi"


class Subscriptable:
    @classmethod
    def __class_getitem__(cls, item):
        return cls


class CoordinatorEntity(Subscriptable):
    def __init__(self, coordinator):
        self.coordinator = coordinator


class DataUpdateCoordinator(Subscriptable):
    def __init__(self, data):
        self.data = data
        self.refreshes = 0
        self.listeners = []

    async def async_request_refresh(self):
        self.refreshes += 1

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def remove_listener():
            self.listeners.remove(listener)

        return remove_listener

    def notify_listeners(self):
        for listener in list(self.listeners):
            listener()


class ConfigEntry:
    def __init__(self):
        self.entry_id = "entry"
        self.title = "primary@example.com"
        self.data = {}
        self.options = {}
        self.unload_callbacks = []

    def async_on_unload(self, callback):
        self.unload_callbacks.append(callback)


class LockEntity:
    def async_write_ha_state(self):
        pass


class ConfigFlow:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()


class OptionsFlow:
    def async_create_entry(self, *, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, *, step_id, data_schema):
        return {"type": "form", "step_id": step_id, "data_schema": data_schema}

    def async_abort(self, *, reason):
        return {"type": "abort", "reason": reason}


binary_sensor = module("homeassistant.components.binary_sensor")
binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass
binary_sensor.BinarySensorEntity = type("BinarySensorEntity", (), {})
button_component = module("homeassistant.components.button")
button_component.ButtonEntity = type("ButtonEntity", (), {})
lock_component = module("homeassistant.components.lock")
lock_component.LockEntity = LockEntity
sensor = module("homeassistant.components.sensor")
sensor.SensorStateClass = SensorStateClass

config_entries = module("homeassistant.config_entries")
config_entries.ConfigEntry = ConfigEntry
config_entries.ConfigFlow = ConfigFlow
config_entries.OptionsFlow = OptionsFlow
core = module("homeassistant.core")
core.callback = lambda function: function
core.HomeAssistant = type("HomeAssistant", (), {})
ha_const = module("homeassistant.const")
ha_const.PERCENTAGE = "%"
ha_const.UnitOfPressure = UnitOfPressure
entity = module("homeassistant.helpers.entity")
entity.DeviceInfo = dict
device_registry = module("homeassistant.helpers.device_registry")
device_registry.DeviceEntry = object
device_registry.async_get = lambda hass: hass.device_registry
device_registry.async_entries_for_config_entry = (
    lambda registry, entry_id: [
        device
        for device in registry.devices
        if entry_id in device.config_entries
    ]
)
entity_registry = module("homeassistant.helpers.entity_registry")
entity_registry.async_get = lambda hass: hass.entity_registry
issue_registry = module("homeassistant.helpers.issue_registry")
issue_registry.IssueSeverity = type("IssueSeverity", (), {"WARNING": "warning"})
issue_registry.async_get = lambda hass: hass.issue_registry
issue_registry.async_create_issue = (
    lambda hass, domain, issue_id, **kwargs: hass.issue_registry.create(
        domain, issue_id, kwargs
    )
)
issue_registry.async_delete_issue = (
    lambda hass, domain, issue_id: hass.issue_registry.delete(domain, issue_id)
)
entity_platform = module("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
config_validation = module("homeassistant.helpers.config_validation")


def multi_select(options):
    def validate(value):
        if not isinstance(value, list):
            raise vol.Invalid("expected a list")
        if any(item not in options for item in value):
            raise vol.Invalid("unknown selection")
        return value

    return validate


config_validation.multi_select = multi_select
update_coordinator = module("homeassistant.helpers.update_coordinator")
update_coordinator.CoordinatorEntity = CoordinatorEntity
update_coordinator.DataUpdateCoordinator = DataUpdateCoordinator

import toyota_na.vehicle.base_vehicle as upstream_base
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    ToyotaVehicle,
    VehicleFeatures,
)

upstream_base.ApiVehicleGeneration = ApiVehicleGeneration
upstream_base.RemoteRequestCommand = RemoteRequestCommand
upstream_base.ToyotaVehicle = ToyotaVehicle
upstream_base.VehicleFeatures = VehicleFeatures

from custom_components.toyota_na import button
from custom_components.toyota_na import binary_sensor as binary_sensor_platform
from custom_components.toyota_na import config_flow
from custom_components.toyota_na import lock as lock_platform
from custom_components.toyota_na import shared_vehicles
from custom_components.toyota_na.const import (
    CONF_MANAGED_VINS,
    DOMAIN,
    OPT_EXCLUDED_VINS,
)
from custom_components.toyota_na.wake_policy import (
    CONF_WAKE_INTERVAL,
    LAST_VEHICLE_WAKES,
    LAST_WAKE_AT,
)


class FakeVehicle:
    def __init__(self, supported, vin="TESTVIN"):
        self.vin = vin
        self.subscribed = True
        self.supported = set(supported)
        self.sent = []
        self.refresh_requests = 0
        self.features = {}
        self.electric = False
        self.backdoor_type = "trunk"
        self.model_year = "2024"
        self.model_name = "LC 500 2-DOOR COUPE"
        self.brand = "L"

    def supports_command(self, command):
        return command in self.supported

    async def send_command(self, command):
        self.sent.append(command)

    async def poll_vehicle_refresh(self):
        self.refresh_requests += 1


class FakeHass:
    def __init__(self, coordinator):
        self.data = {DOMAIN: {"entry": {"coordinator": coordinator}}}
        self.tasks = []
        self.config_entries = self
        self.entity_registry = FakeEntityRegistry()
        self.device_registry = FakeDeviceRegistry()
        self.issue_registry = FakeIssueRegistry()
        self.entries = {}
        self.reloads = []

    def async_create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def async_update_entry(self, entry, *, data):
        entry.data = data

    def async_get_entry(self, entry_id):
        return self.entries.get(entry_id)

    async def async_reload(self, entry_id):
        self.reloads.append(entry_id)


class FakeEntityRegistry:
    def __init__(self):
        self.entities = {}
        self.removed = []

    def async_get_entity_id(self, platform, domain, unique_id):
        return self.entities.get((platform, domain, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


class FakeDeviceRegistry:
    def __init__(self):
        self.devices = []
        self.updated = []

    def async_update_device(self, device_id, *, remove_config_entry_id):
        self.updated.append((device_id, remove_config_entry_id))


class FakeIssueRegistry:
    def __init__(self):
        self.issues = {}

    def create(self, domain, issue_id, data):
        self.issues[(domain, issue_id)] = data

    def delete(self, domain, issue_id):
        self.issues.pop((domain, issue_id), None)


class ButtonTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        button.COMMAND_REFRESH_DELAY = 0
        lock_platform.COMMAND_REFRESH_DELAY = 0
        self.vehicle = FakeVehicle(
            {
                RemoteRequestCommand.DoorLock,
                RemoteRequestCommand.DoorUnlock,
                RemoteRequestCommand.EngineStart,
                RemoteRequestCommand.EngineStop,
                RemoteRequestCommand.HazardsOn,
                RemoteRequestCommand.VehicleFinder,
            }
        )
        self.coordinator = DataUpdateCoordinator([self.vehicle])
        self.hass = FakeHass(self.coordinator)
        self.config_entry = ConfigEntry()
        self.entities = []

        await button.async_setup_entry(
            self.hass,
            self.config_entry,
            lambda entities, update_before_add: self.entities.extend(entities),
        )
        for entity_instance in self.entities:
            entity_instance.hass = self.hass

    async def test_creates_only_supported_lc_controls(self):
        self.assertEqual(
            [entity.sensor_name for entity in self.entities],
            [
                "Remote Start",
                "Remote Stop",
                "Flash Hazards",
                "Find Vehicle",
                "Refresh Status",
            ],
        )
        self.assertEqual(
            [entity.name for entity in self.entities],
            [
                "Remote Start",
                "Remote Stop",
                "Flash Hazards",
                "Find Vehicle",
                "Refresh Status",
            ],
        )

    async def test_command_button_does_not_issue_an_extra_vehicle_wake(self):
        await self.entities[0].async_press()
        await asyncio.gather(*self.hass.tasks)

        self.assertEqual(self.vehicle.sent, [RemoteRequestCommand.EngineStart])
        self.assertEqual(self.vehicle.refresh_requests, 0)
        self.assertEqual(self.coordinator.refreshes, 1)
        self.assertIn(LAST_WAKE_AT, self.config_entry.data)
        self.assertEqual(
            self.config_entry.data[LAST_VEHICLE_WAKES],
            [{"vin": "TESTVIN", "timestamp": self.config_entry.data[LAST_WAKE_AT]}],
        )

    async def test_refresh_button_explicitly_requests_vehicle_status(self):
        await self.entities[-1].async_press()
        await asyncio.gather(*self.hass.tasks)

        self.assertEqual(self.vehicle.refresh_requests, 1)
        self.assertEqual(self.coordinator.refreshes, 1)
        self.assertIn(LAST_WAKE_AT, self.config_entry.data)
        self.assertEqual(
            self.config_entry.data[LAST_VEHICLE_WAKES][0]["vin"],
            "TESTVIN",
        )

    async def test_capability_change_marks_command_unavailable(self):
        self.vehicle.supported.remove(RemoteRequestCommand.HazardsOn)

        self.assertFalse(self.entities[2].available)

    async def test_new_command_capability_adds_button_without_reload(self):
        vehicle = FakeVehicle({RemoteRequestCommand.EngineStart})
        coordinator = DataUpdateCoordinator([vehicle])
        hass = FakeHass(coordinator)
        config_entry = ConfigEntry()
        entities = []

        await button.async_setup_entry(
            hass,
            config_entry,
            lambda added, update_before_add: entities.extend(added),
        )
        self.assertEqual(
            [entity.sensor_name for entity in entities],
            ["Remote Start", "Refresh Status"],
        )

        vehicle.supported.add(RemoteRequestCommand.EngineStop)
        coordinator.notify_listeners()
        coordinator.notify_listeners()

        self.assertEqual(
            [entity.sensor_name for entity in entities],
            ["Remote Start", "Refresh Status", "Remote Stop"],
        )

    async def test_lock_command_does_not_issue_an_extra_vehicle_wake(self):
        entities = []
        await lock_platform.async_setup_entry(
            self.hass,
            self.config_entry,
            lambda added, update_before_add: entities.extend(added),
        )
        lock = entities[0]
        lock.hass = self.hass

        await lock.async_lock()
        await asyncio.gather(*self.hass.tasks)

        self.assertEqual(self.vehicle.sent, [RemoteRequestCommand.DoorLock])
        self.assertEqual(self.vehicle.refresh_requests, 0)
        self.assertEqual(self.coordinator.refreshes, 1)
        self.assertIn(LAST_WAKE_AT, self.config_entry.data)

    async def test_failed_lock_command_clears_transition_state(self):
        async def fail_command(command):
            raise RuntimeError("command rejected")

        self.vehicle.send_command = fail_command
        entities = []
        await lock_platform.async_setup_entry(
            self.hass,
            self.config_entry,
            lambda added, update_before_add: entities.extend(added),
        )
        lock = entities[0]
        lock.hass = self.hass

        with self.assertRaisesRegex(RuntimeError, "command rejected"):
            await lock.async_lock()

        self.assertFalse(lock._state_changing)
        self.assertNotIn(LAST_WAKE_AT, self.config_entry.data)
        self.assertEqual(self.hass.tasks, [])

    async def test_lock_availability_tracks_capabilities(self):
        entities = []
        await lock_platform.async_setup_entry(
            self.hass,
            self.config_entry,
            lambda added, update_before_add: entities.extend(added),
        )

        self.vehicle.supported.remove(RemoteRequestCommand.DoorUnlock)

        self.assertFalse(entities[0].available)


class BinarySensorCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_tailgate_removes_stale_trunk_entities(self):
        vehicle = FakeVehicle(set())
        vehicle.backdoor_type = "tailgate"
        coordinator = DataUpdateCoordinator([vehicle])
        hass = FakeHass(coordinator)
        config_entry = ConfigEntry()
        hass.entity_registry.entities = {
            (
                "binary_sensor",
                DOMAIN,
                "TESTVIN.Trunk",
            ): "binary_sensor.testvin_trunk",
            (
                "binary_sensor",
                DOMAIN,
                "TESTVIN.Trunk Door Lock",
            ): "binary_sensor.testvin_trunk_door_lock",
        }

        await binary_sensor_platform.async_setup_entry(
            hass,
            config_entry,
            lambda entities, update_before_add: None,
        )

        self.assertEqual(
            hass.entity_registry.removed,
            [
                "binary_sensor.testvin_trunk",
                "binary_sensor.testvin_trunk_door_lock",
            ],
        )


class SharedVehicleLifecycleTests(unittest.TestCase):
    def test_conflict_creates_entry_scoped_repair(self):
        hass = FakeHass(DataUpdateCoordinator([]))
        losing_entry = ConfigEntry()
        losing_entry.entry_id = "losing-entry"
        managing_entry = ConfigEntry()
        managing_entry.entry_id = "managing-entry"
        managing_entry.title = "manager@example.com"
        hass.entries[managing_entry.entry_id] = managing_entry
        vehicle = FakeVehicle(set())

        with self.assertLogs(shared_vehicles._LOGGER, level="WARNING"):
            shared_vehicles.report_vehicle_conflict(
                hass,
                losing_entry,
                vehicle,
                managing_entry.entry_id,
            )

        issue_id = shared_vehicles.shared_vehicle_issue_id(
            vehicle.vin,
            losing_entry.entry_id,
        )
        issue = hass.issue_registry.issues[(DOMAIN, issue_id)]
        self.assertEqual(issue["translation_key"], "shared_vehicle")
        self.assertEqual(
            issue["translation_placeholders"]["managing_account"],
            "manager@example.com",
        )

    def test_clears_only_repairs_owned_by_entry(self):
        hass = FakeHass(DataUpdateCoordinator([]))
        owned = (DOMAIN, "shared_vehicle_TESTVIN_losing-entry")
        other = (DOMAIN, "shared_vehicle_TESTVIN_other-entry")
        unrelated = (DOMAIN, "another_issue_losing-entry")
        hass.issue_registry.issues = {
            owned: {},
            other: {},
            unrelated: {},
        }

        shared_vehicles.clear_entry_conflicts(hass, "losing-entry")

        self.assertEqual(
            set(hass.issue_registry.issues),
            {other, unrelated},
        )

    def test_prunes_only_confirmed_vehicle_devices(self):
        hass = FakeHass(DataUpdateCoordinator([]))
        entry = ConfigEntry()
        hass.device_registry.devices = [
            types.SimpleNamespace(
                id="excluded-device",
                identifiers={(DOMAIN, "EXCLUDEDVIN")},
                config_entries={entry.entry_id},
            ),
            types.SimpleNamespace(
                id="kept-device",
                identifiers={(DOMAIN, "KEPTVIN")},
                config_entries={entry.entry_id},
            ),
        ]

        shared_vehicles.prune_entry_devices(
            hass,
            entry,
            {"EXCLUDEDVIN"},
        )

        self.assertEqual(
            hass.device_registry.updated,
            [("excluded-device", entry.entry_id)],
        )

    def test_active_vehicle_device_cannot_be_removed_as_orphaned(self):
        vehicle = FakeVehicle(set())
        hass = FakeHass(DataUpdateCoordinator([vehicle]))
        entry = ConfigEntry()
        active_device = types.SimpleNamespace(
            identifiers={(DOMAIN, vehicle.vin)}
        )
        orphaned_device = types.SimpleNamespace(
            identifiers={(DOMAIN, "SOLDVIN")}
        )

        self.assertTrue(
            shared_vehicles.entry_manages_device(hass, entry, active_device)
        )
        self.assertFalse(
            shared_vehicles.entry_manages_device(hass, entry, orphaned_device)
        )


class SharedVehicleOptionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_policy_change_does_not_reload_entry(self):
        hass = FakeHass(DataUpdateCoordinator([]))
        entry = ConfigEntry()
        hass.data[DOMAIN][entry.entry_id]["excluded_vins_snapshot"] = set()
        entry.options = {CONF_WAKE_INTERVAL: 0}

        await shared_vehicles.async_update_options(hass, entry)

        self.assertEqual(hass.reloads, [])

    async def test_vehicle_assignment_change_reloads_entry(self):
        hass = FakeHass(DataUpdateCoordinator([]))
        entry = ConfigEntry()
        hass.data[DOMAIN][entry.entry_id]["excluded_vins_snapshot"] = set()
        entry.options = {OPT_EXCLUDED_VINS: ["TESTVIN"]}

        await shared_vehicles.async_update_options(hass, entry)

        self.assertEqual(hass.reloads, [entry.entry_id])


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    def make_flow(self, *, options=None, vehicles=None):
        class Client:
            async def get_user_vehicle_list(self):
                if vehicles is not None:
                    return vehicles
                return [
                    {
                        "vin": "TESTVIN",
                        "modelYear": "2024",
                        "modelName": "LC 500 2-DOOR COUPE",
                        "generation": "21MM",
                    }
                ]

        config_entry = ConfigEntry()
        config_entry.options = options or {}
        hass = FakeHass(DataUpdateCoordinator([]))
        hass.data[DOMAIN][config_entry.entry_id]["toyota_na_client"] = Client()
        flow = config_flow.ToyotaNAOptionsFlow()
        flow.config_entry = config_entry
        flow.hass = hass
        return flow

    async def test_default_preserves_existing_two_hour_behavior(self):
        result = await self.make_flow().async_step_init()

        self.assertEqual(
            result["data_schema"]({}),
            {
                CONF_WAKE_INTERVAL: str(2 * 3600),
                CONF_MANAGED_VINS: ["TESTVIN"],
            },
        )

    async def test_unloaded_entry_aborts_cleanly(self):
        config_entry = ConfigEntry()
        hass = FakeHass(DataUpdateCoordinator([]))
        hass.data[DOMAIN][config_entry.entry_id].pop("coordinator")
        flow = config_flow.ToyotaNAOptionsFlow()
        flow.config_entry = config_entry
        flow.hass = hass

        result = await flow.async_step_init()

        self.assertEqual(
            result,
            {"type": "abort", "reason": "entry_not_loaded"},
        )

    async def test_account_without_supported_vehicles_aborts_cleanly(self):
        result = await self.make_flow(vehicles=[]).async_step_init()

        self.assertEqual(
            result,
            {"type": "abort", "reason": "no_vehicles"},
        )

    async def test_manual_only_is_saved(self):
        result = await self.make_flow().async_step_init(
            {
                CONF_WAKE_INTERVAL: "0",
                CONF_MANAGED_VINS: ["TESTVIN"],
            }
        )

        self.assertEqual(
            result,
            {
                "type": "create_entry",
                "title": "",
                "data": {
                    CONF_WAKE_INTERVAL: 0,
                    OPT_EXCLUDED_VINS: [],
                },
            },
        )

    async def test_saved_interval_is_selected_when_reopened(self):
        for interval in (0, 12 * 3600):
            with self.subTest(interval=interval):
                result = await self.make_flow(
                    options={CONF_WAKE_INTERVAL: interval}
                ).async_step_init()

                self.assertEqual(
                    result["data_schema"]({}),
                    {
                        CONF_WAKE_INTERVAL: str(interval),
                        CONF_MANAGED_VINS: ["TESTVIN"],
                    },
                )

    async def test_unchecked_vehicle_is_excluded(self):
        result = await self.make_flow().async_step_init(
            {
                CONF_WAKE_INTERVAL: str(2 * 3600),
                CONF_MANAGED_VINS: [],
            }
        )

        self.assertEqual(result["data"][OPT_EXCLUDED_VINS], ["TESTVIN"])

    async def test_exclusion_survives_a_partial_vehicle_list(self):
        result = await self.make_flow(
            options={OPT_EXCLUDED_VINS: ["MISSINGVIN"]}
        ).async_step_init(
            {
                CONF_WAKE_INTERVAL: str(2 * 3600),
                CONF_MANAGED_VINS: ["TESTVIN"],
            }
        )

        self.assertEqual(result["data"][OPT_EXCLUDED_VINS], ["MISSINGVIN"])


if __name__ == "__main__":
    unittest.main()
