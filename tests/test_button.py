# ruff: noqa: I001

import asyncio
from enum import Enum
from pathlib import Path
import sys
import types
import unittest


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

    async def async_request_refresh(self):
        self.refreshes += 1


class ConfigEntry:
    def __init__(self):
        self.entry_id = "entry"
        self.data = {}
        self.options = {}


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


binary_sensor = module("homeassistant.components.binary_sensor")
binary_sensor.BinarySensorDeviceClass = BinarySensorDeviceClass
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
entity_platform = module("homeassistant.helpers.entity_platform")
entity_platform.AddEntitiesCallback = object
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
from custom_components.toyota_na import config_flow
from custom_components.toyota_na import lock as lock_platform
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL, LAST_WAKE_AT


class FakeVehicle:
    def __init__(self, supported):
        self.vin = "TESTVIN"
        self.subscribed = True
        self.supported = set(supported)
        self.sent = []
        self.refresh_requests = 0
        self.features = {}
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

    def async_create_task(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    def async_update_entry(self, entry, *, data):
        entry.data = data


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
                "Remote Start 2024 LC 500 2-DOOR COUPE",
                "Remote Stop 2024 LC 500 2-DOOR COUPE",
                "Flash Hazards 2024 LC 500 2-DOOR COUPE",
                "Find Vehicle 2024 LC 500 2-DOOR COUPE",
                "Refresh Status 2024 LC 500 2-DOOR COUPE",
            ],
        )

    async def test_command_button_does_not_issue_an_extra_vehicle_wake(self):
        await self.entities[0].async_press()
        await asyncio.gather(*self.hass.tasks)

        self.assertEqual(self.vehicle.sent, [RemoteRequestCommand.EngineStart])
        self.assertEqual(self.vehicle.refresh_requests, 0)
        self.assertEqual(self.coordinator.refreshes, 1)
        self.assertIn(LAST_WAKE_AT, self.config_entry.data)

    async def test_refresh_button_explicitly_requests_vehicle_status(self):
        await self.entities[-1].async_press()
        await asyncio.gather(*self.hass.tasks)

        self.assertEqual(self.vehicle.refresh_requests, 1)
        self.assertEqual(self.coordinator.refreshes, 1)
        self.assertIn(LAST_WAKE_AT, self.config_entry.data)

    async def test_capability_change_marks_command_unavailable(self):
        self.vehicle.supported.remove(RemoteRequestCommand.HazardsOn)

        self.assertFalse(self.entities[2].available)

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

    async def test_lock_availability_tracks_capabilities(self):
        entities = []
        await lock_platform.async_setup_entry(
            self.hass,
            self.config_entry,
            lambda added, update_before_add: entities.extend(added),
        )

        self.vehicle.supported.remove(RemoteRequestCommand.DoorUnlock)

        self.assertFalse(entities[0].available)


class OptionsFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_preserves_existing_two_hour_behavior(self):
        result = await config_flow.ToyotaNAOptionsFlow(
            ConfigEntry()
        ).async_step_init()

        self.assertEqual(
            result["data_schema"]({}),
            {CONF_WAKE_INTERVAL: str(2 * 3600)},
        )

    async def test_manual_only_is_saved(self):
        result = await config_flow.ToyotaNAOptionsFlow(
            ConfigEntry()
        ).async_step_init({CONF_WAKE_INTERVAL: "0"})

        self.assertEqual(
            result,
            {
                "type": "create_entry",
                "title": "",
                "data": {CONF_WAKE_INTERVAL: 0},
            },
        )

    async def test_saved_interval_is_selected_when_reopened(self):
        for interval in (0, 12 * 3600):
            with self.subTest(interval=interval):
                config_entry = ConfigEntry()
                config_entry.options = {CONF_WAKE_INTERVAL: interval}

                result = await config_flow.ToyotaNAOptionsFlow(
                    config_entry
                ).async_step_init()

                self.assertEqual(
                    result["data_schema"]({}),
                    {CONF_WAKE_INTERVAL: str(interval)},
                )


if __name__ == "__main__":
    unittest.main()
