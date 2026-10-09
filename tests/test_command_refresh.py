"""Engine follow-up polls only the requested vehicle and stops promptly."""

import asyncio
import types
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from common import FakeVehicle, entity_id, make_17cy_vehicle, make_vehicle
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.toyota_na import command_refresh
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
)


async def press(hass, name):
    await hass.services.async_call(
        "button",
        "press",
        {"entity_id": entity_id(hass, "button", f"TESTVIN.{name}")},
        blocking=True,
    )


async def test_engine_button_polls_until_started_without_account_polls_or_wakes(
    hass, setup_vehicles
):
    client = types.SimpleNamespace(
        remote_request_21mm=AsyncMock(),
        get_engine_status_21mm=AsyncMock(side_effect=[{"status": "OFF"}, {"status": "ON"}]),
    )
    vehicle = make_vehicle(client)
    other = FakeVehicle(set(), vin="OTHER")
    other.poll_engine_status = AsyncMock()
    account = await setup_vehicles([vehicle, other])
    refreshes = account.get_vehicles.await_count
    with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
        await press(hass, "Remote Start")
        await hass.async_block_till_done()
    client.remote_request_21mm.assert_awaited_once()
    assert client.get_engine_status_21mm.await_count == 2
    running = entity_id(hass, "binary_sensor", "TESTVIN.Remote Start")
    assert hass.states.get(running).state == "on"
    assert account.get_vehicles.await_count == refreshes
    other.poll_engine_status.assert_not_awaited()


@pytest.mark.parametrize("status, count", [("ON", 4), ("OFF", 1)])
@pytest.mark.parametrize(
    "factory, generation, getter",
    [
        (make_17cy_vehicle, ApiVehicleGeneration.CY17, "get_engine_status_17cy"),
        (make_vehicle, ApiVehicleGeneration.CY17PLUS, "get_engine_status_17cyplus"),
        (make_vehicle, ApiVehicleGeneration.MM21, "get_engine_status_21mm"),
    ],
)
async def test_stop_followup_is_limited_even_if_status_never_changes(
    hass, setup_vehicles, factory, generation, getter, status, count
):
    read = AsyncMock(return_value={"status": status})
    client = types.SimpleNamespace(
        **{getter: read},
        remote_request_17cy=AsyncMock(),
        remote_request_17cyplus=AsyncMock(),
        remote_request_21mm=AsyncMock(),
    )
    vehicle = factory(client)
    vehicle._generation = generation
    account = await setup_vehicles([vehicle])
    refreshes = account.get_vehicles.await_count
    button = entity_id(hass, "button", "TESTVIN.Remote Stop")
    device_id = er.async_get(hass).async_get(button).device_id
    with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
        await hass.services.async_call(DOMAIN, "engine_stop", {"vehicle": device_id}, blocking=True)
        await hass.async_block_till_done()
    assert read.await_count == count
    assert account.get_vehicles.await_count == refreshes


async def test_followup_does_not_treat_cached_engine_state_as_confirmation(hass, setup_vehicles):
    client = types.SimpleNamespace(
        remote_request_21mm=AsyncMock(), get_engine_status_21mm=AsyncMock(return_value={})
    )
    vehicle = make_vehicle(client)
    vehicle._parse_engine_status({"status": "ON"})
    await setup_vehicles([vehicle])
    with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
        await press(hass, "Remote Start")
        await hass.async_block_till_done()
    assert client.get_engine_status_21mm.await_count == 4


async def test_missing_vehicle_or_elapsed_deadline_stops_followup(hass, setup_vehicles):
    vehicle = make_vehicle(types.SimpleNamespace(remote_request_21mm=AsyncMock()))
    vehicle.poll_engine_status = AsyncMock()
    account = await setup_vehicles([vehicle])
    with patch.object(command_refresh, "ENGINE_STATUS_TIMEOUT", 0):
        await press(hass, "Remote Start")
        await hass.async_block_till_done()
    # wait_for cancels an expired read before it is awaited, so check for any call.
    vehicle.poll_engine_status.assert_not_called()
    account.coordinator.async_set_updated_data([])
    await hass.async_block_till_done()
    # The button can't be pressed without its vehicle, so follow up as it would.
    await command_refresh.refresh_after_command(
        account.coordinator, vehicle.vin, RemoteRequestCommand.EngineStart
    )
    vehicle.poll_engine_status.assert_not_called()


async def test_unload_cancels_engine_followup(hass, setup_vehicles):
    vehicle = make_vehicle(types.SimpleNamespace(remote_request_21mm=AsyncMock()))
    vehicle.poll_engine_status = AsyncMock()
    account = await setup_vehicles([vehicle])
    await press(hass, "Remote Start")
    unloaded = await hass.config_entries.async_unload(account.entry.entry_id)
    # Past the follow-up's first wait, a task that wasn't cancelled would read now.
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=100))
    async with asyncio.timeout(1):
        await hass.async_block_till_done()
    vehicle.poll_engine_status.assert_not_awaited()
    # A failed unload skips the coordinator's shutdown, so stop its timers here.
    await account.coordinator.async_shutdown()
    assert unloaded


async def test_stalled_engine_read_is_cancelled_at_the_followup_deadline(hass, setup_vehicles):
    cancelled = asyncio.Event()

    async def stall():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    vehicle = make_vehicle(types.SimpleNamespace(remote_request_21mm=AsyncMock()))
    vehicle.poll_engine_status = AsyncMock(side_effect=stall)
    await setup_vehicles([vehicle])
    with (
        patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0),
        patch.object(command_refresh, "ENGINE_STATUS_TIMEOUT", 0.02),
    ):
        await press(hass, "Remote Stop")
        await hass.async_block_till_done()
    assert cancelled.is_set()
    vehicle.poll_engine_status.assert_awaited_once()
