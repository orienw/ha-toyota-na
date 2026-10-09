"""Expected failures reach Home Assistant through every command entry point."""

import asyncio
import json
import types
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientResponseError
from common import FakeVehicle, entity_id, get_entity
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from toyota_na.exceptions import TokenExpired

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import RemoteRequestCommand


@pytest.fixture(autouse=True)
def no_command_delay():
    """Poll right after a command, like setup_vehicles does for the integration's services.

    The button and lock platforms import their own copy of the delay.
    """
    with (
        patch("custom_components.toyota_na.button.COMMAND_REFRESH_DELAY", 0),
        patch("custom_components.toyota_na.lock.COMMAND_REFRESH_DELAY", 0),
    ):
        yield


@pytest.fixture
async def commands(hass, setup_vehicles):
    vehicle = FakeVehicle(
        {
            RemoteRequestCommand.DoorLock,
            RemoteRequestCommand.DoorUnlock,
            RemoteRequestCommand.EngineStart,
        }
    )
    account = await setup_vehicles([vehicle])
    # Run the poll entities requested as they were added and wait out its cooldown, so a
    # poll after a failed command would run right away and show in the count.
    for _ in range(2):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
        await hass.async_block_till_done()
    lock = entity_id(hass, "lock", "TESTVIN.")
    device_id = er.async_get(hass).async_get(lock).device_id
    target = {"entity_id": entity_id(hass, "button", "TESTVIN.Remote Start")}
    refresh = {"entity_id": entity_id(hass, "button", "TESTVIN.Refresh Status")}
    actions = [
        ("button", "press", target, "send_command"),
        ("button", "press", refresh, "poll_vehicle_refresh"),
        ("lock", "lock", {"entity_id": lock}, "send_command"),
        ("lock", "unlock", {"entity_id": lock}, "send_command"),
        (DOMAIN, "door_lock", {"vehicle": device_id}, "send_command"),
        (DOMAIN, "refresh", {"vehicle": device_id}, "poll_vehicle_refresh"),
    ]
    return vehicle, account, lock, actions


async def test_expected_command_failures_use_home_assistant_errors(hass, commands, subtests):
    vehicle, account, lock, actions = commands
    data = dict(account.entry.data)
    polls = account.get_vehicles.await_count
    request_info = types.SimpleNamespace(real_url="https://example.invalid/remote/command")
    detail = "Vehicle not reachable [ONE-RES-40001]"
    for error, expected_type, message in (
        (
            ValueError("Command is unavailable."),
            ServiceValidationError,
            "Command is unavailable.",
        ),
        (
            RuntimeError("Toyota rejected the command."),
            HomeAssistantError,
            "Toyota rejected the command.",
        ),
        (
            TokenExpired(),
            HomeAssistantError,
            "Toyota authentication failed. Sign in again.",
        ),
        (
            json.JSONDecodeError("Expecting value", "invalid", 0),
            HomeAssistantError,
            "Toyota returned an invalid response.",
        ),
        (
            ClientResponseError(request_info, (), status=400, message=detail),
            HomeAssistantError,
            detail,
        ),
        (
            ClientResponseError(request_info, (), status=400),
            HomeAssistantError,
            "The Toyota request failed. Try again.",
        ),
    ):
        for domain, service, target, operation in actions:
            with subtests.test(domain=domain, service=service, target=target, error=type(error)):
                with patch.object(vehicle, operation, AsyncMock(side_effect=error)) as send:
                    with pytest.raises(expected_type) as raised:
                        await hass.services.async_call(domain, service, target, blocking=True)
                    await hass.async_block_till_done()
                assert type(raised.value) is expected_type
                assert str(raised.value) == message
                assert raised.value.__cause__ is error
                send.assert_awaited_once()
                # With no door lock states the lock can't show locking, so read the flag itself.
                assert not get_entity(hass, lock)._state_changing
                assert account.entry.data == data
                assert account.get_vehicles.await_count == polls


async def test_cancellation_and_unexpected_errors_propagate(hass, commands, subtests):
    vehicle, account, lock, actions = commands
    data = dict(account.entry.data)
    polls = account.get_vehicles.await_count
    for error in (asyncio.CancelledError(), KeyError("broken payload")):
        for domain, service, target, operation in actions:
            with subtests.test(domain=domain, service=service, target=target, error=type(error)):
                with patch.object(vehicle, operation, AsyncMock(side_effect=error)):
                    with pytest.raises(type(error)) as raised:
                        await hass.services.async_call(domain, service, target, blocking=True)
                    await hass.async_block_till_done()
                assert raised.value is error
                # With no door lock states the lock can't show locking, so read the flag itself.
                assert not get_entity(hass, lock)._state_changing
                assert account.entry.data == data
                assert account.get_vehicles.await_count == polls
