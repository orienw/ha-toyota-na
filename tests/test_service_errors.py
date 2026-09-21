"""Expected failures reach Home Assistant through every command entry point."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

from toyota_na.exceptions import TokenExpired

import test_button as ha


class CommandServiceErrorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = ha.FakeVehicle({
            ha.RemoteRequestCommand.DoorLock, ha.RemoteRequestCommand.DoorUnlock,
            ha.RemoteRequestCommand.EngineStart,
        })
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle])
        self.hass = ha.FakeHass(self.coordinator)
        self.entry = ha.ConfigEntry()
        buttons = []
        await ha.button.async_setup_entry(self.hass, self.entry, lambda added, update: buttons.extend(added))
        for button in buttons:
            button.hass = self.hass
        self.lock = ha.lock_platform.ToyotaLock(self.entry, self.coordinator, "", self.vehicle.vin)
        self.lock.hass = self.hass
        handlers = {}
        self.hass.services = types.SimpleNamespace(async_register=lambda domain, name, handler: handlers.update({name: handler}))
        self.hass.async_get_entry = lambda entry_id: self.entry
        self.hass.device_registry = types.SimpleNamespace(async_get=lambda device_id: types.SimpleNamespace(
            config_entries={self.entry.entry_id}, identifiers={(ha.DOMAIN, self.vehicle.vin)},
        ))
        await ha.integration_runtime.async_setup(self.hass, {})
        self.actions = [
            (buttons[0].async_press, (), "send_command"),
            (buttons[-1].async_press, (), "poll_vehicle_refresh"),
            (self.lock.async_lock, (), "send_command"),
            (self.lock.async_unlock, (), "send_command"),
        ]
        for service, operation in ((ha.lock_platform.DOOR_LOCK, "send_command"), ("refresh", "poll_vehicle_refresh")):
            call = types.SimpleNamespace(service=service, data={"vehicle": "device"})
            self.actions.append((handlers[service], (call,), operation))

    async def test_expected_command_failures_use_home_assistant_errors(self):
        for error, expected_type, message in (
            (ValueError("Command is unavailable."), ha.exceptions.ServiceValidationError, "Command is unavailable."),
            (RuntimeError("Toyota rejected the command."), ha.exceptions.HomeAssistantError, "Toyota rejected the command."),
            (TokenExpired(), ha.exceptions.HomeAssistantError, "Toyota authentication failed. Sign in again."),
            (json.JSONDecodeError("Expecting value", "invalid", 0), ha.exceptions.HomeAssistantError, "Toyota returned an invalid response."),
        ):
            for action, args, operation in self.actions:
                with self.subTest(action=action, args=args, error=type(error)):
                    with patch.object(self.vehicle, operation, AsyncMock(side_effect=error)) as send:
                        with self.assertRaises(expected_type) as raised:
                            await action(*args)
                    self.assertIs(type(raised.exception), expected_type)
                    self.assertEqual(message, str(raised.exception))
                    self.assertIs(error, raised.exception.__cause__)
                    send.assert_awaited_once()
                    self.assertFalse(self.lock._state_changing)
                    self.assertEqual({}, self.entry.data)
                    self.assertEqual([], self.hass.tasks)

    async def test_cancellation_and_unexpected_errors_propagate(self):
        for error in (asyncio.CancelledError(), KeyError("broken payload")):
            for action, args, operation in self.actions:
                with self.subTest(action=action, args=args, error=type(error)):
                    with patch.object(self.vehicle, operation, AsyncMock(side_effect=error)):
                        with self.assertRaises(type(error)) as raised:
                            await action(*args)
                    self.assertIs(error, raised.exception)
                    self.assertFalse(self.lock._state_changing)
                    self.assertEqual({}, self.entry.data)
                    self.assertEqual([], self.hass.tasks)
