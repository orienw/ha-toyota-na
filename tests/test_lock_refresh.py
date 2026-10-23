"""Lock follow-up reads only the requested vehicle's cached status."""

import asyncio
import types
from unittest.mock import AsyncMock

import pytest
from common import entity_id, make_17cy_vehicle, make_24mm_vehicle, make_vehicle
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na.command_refresh import refresh_after_command
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.exceptions import TokenExpired
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
)


def rest_status(locked, timestamp=None):
    return {
        "occurrenceDate": timestamp,
        "vehicleStatus": [
            {
                "category": "Driver Side",
                "sections": [
                    {
                        "section": "Door",
                        "values": [
                            {"value": "closed"},
                            {"value": "locked" if locked else "unlocked"},
                        ],
                    }
                ],
            }
        ],
    }


def graphql_status(locked, timestamp=None):
    return {
        "vehicleState": {
            "lastUpdateDateTime": timestamp,
            "doors": {"driverSide": {"lock": {"status": "lock" if locked else "unlock"}}},
        }
    }


@pytest.mark.parametrize("native", [True, False], ids=["native_lock", "toyota_service"])
@pytest.mark.parametrize("locked", [True, False], ids=["lock", "unlock"])
async def test_delayed_status_updates_the_lock_without_account_reads_or_wakes(
    hass, setup_vehicles, native, locked
):
    read = AsyncMock(side_effect=[rest_status(not locked), rest_status(locked)])
    client = types.SimpleNamespace(remote_request_21mm=AsyncMock(), get_vehicle_status_21mm=read)
    vehicle = make_vehicle(client)
    vehicle._parse_vehicle_status(rest_status(not locked))
    vehicle.poll_vehicle_refresh = AsyncMock()
    other_read = AsyncMock()
    other = make_vehicle(types.SimpleNamespace(get_vehicle_status_21mm=other_read))
    other._vin = "OTHER"
    account = await setup_vehicles([vehicle, other])
    polls = account.get_vehicles.await_count
    lock_id = entity_id(hass, "lock", "TESTVIN.")
    action = "lock" if locked else "unlock"
    await hass.services.async_call(
        "lock" if native else DOMAIN,
        action if native else f"door_{action}",
        {"entity_id": lock_id}
        if native
        else {"vehicle": er.async_get(hass).async_get(lock_id).device_id},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.states.get(lock_id).state == ("locked" if locked else "unlocked")
    assert read.await_count == 2
    assert all(call.args == (vehicle.vin, vehicle.region) for call in read.await_args_list)
    client.remote_request_21mm.assert_awaited_once_with(
        vehicle.vin, "door-lock" if locked else "door-unlock", vehicle.region
    )
    assert account.get_vehicles.await_count == polls
    other_read.assert_not_awaited()
    vehicle.poll_vehicle_refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "responses, initial, expected_reads",
    [
        ([rest_status(True)], False, 1),
        ([rest_status(False)] * 3, False, 3),
        ([None, {}, None], True, 3),
        ([RuntimeError("offline"), rest_status(True)], False, 2),
        ([{"vehicleStatus": []}], True, 1),
    ],
    ids=["already_updated", "still_stale", "empty", "retry_read_failure", "partial_status"],
)
async def test_followup_stops_on_the_requested_state_and_caps_cached_reads(
    hass, setup_vehicles, responses, initial, expected_reads
):
    read = AsyncMock(side_effect=responses)
    vehicle = make_vehicle(types.SimpleNamespace(get_vehicle_status_21mm=read))
    vehicle._parse_vehicle_status(rest_status(initial))
    account = await setup_vehicles([vehicle])
    polls = account.get_vehicles.await_count

    await refresh_after_command(
        account.coordinator, vehicle.vin, RemoteRequestCommand.DoorLock, delay=0
    )

    assert read.await_count == expected_reads
    assert account.get_vehicles.await_count == polls


@pytest.mark.parametrize(
    "generation, getter",
    [
        (ApiVehicleGeneration.CY17, "get_vehicle_status_17cy"),
        (ApiVehicleGeneration.CY17PLUS, "get_vehicle_status_17cyplus"),
        (ApiVehicleGeneration.MM21, "get_vehicle_status_21mm"),
        (ApiVehicleGeneration.GR86, "get_vehicle_status_route"),
        (ApiVehicleGeneration.NG86, "get_vehicle_status_route"),
        (ApiVehicleGeneration.MM24, "graphql_get_vehicle_status"),
        (ApiVehicleGeneration.BEV26, "graphql_get_vehicle_status"),
    ],
)
async def test_cached_status_uses_the_vehicle_transport(generation, getter):
    graphql = getter == "graphql_get_vehicle_status"
    read = AsyncMock(return_value=graphql_status(True) if graphql else rest_status(True))
    client = types.SimpleNamespace(**{getter: read})
    if generation == ApiVehicleGeneration.CY17:
        vehicle = make_17cy_vehicle(client)
    elif graphql:
        vehicle = make_24mm_vehicle(client)
    else:
        vehicle = make_vehicle(client)
    vehicle._generation = generation

    assert await vehicle.poll_vehicle_status()

    assert vehicle.doors_locked is True
    if graphql:
        read.assert_awaited_once_with(vehicle.vin, vehicle.backdoor_type, vehicle.region)
    elif getter == "get_vehicle_status_route":
        read.assert_awaited_once_with(vehicle.vin, generation.value, vehicle.region, vehicle.brand)
    else:
        read.assert_awaited_once_with(vehicle.vin, vehicle.region)


async def test_expired_login_starts_reauthentication_without_an_account_read(hass, setup_vehicles):
    read = AsyncMock(side_effect=TokenExpired())
    vehicle = make_vehicle(types.SimpleNamespace(get_vehicle_status_21mm=read))
    account = await setup_vehicles([vehicle])
    polls = account.get_vehicles.await_count

    await refresh_after_command(
        account.coordinator, vehicle.vin, RemoteRequestCommand.DoorLock, delay=0
    )
    await hass.async_block_till_done()

    read.assert_awaited_once()
    assert account.get_vehicles.await_count == polls
    assert any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )


async def test_followup_resolves_the_current_vehicle_before_each_read(hass, setup_vehicles):
    replacement_read = AsyncMock(return_value=rest_status(True))
    replacement = make_vehicle(types.SimpleNamespace(get_vehicle_status_21mm=replacement_read))
    client = types.SimpleNamespace()
    vehicle = make_vehicle(client)
    account = await setup_vehicles([vehicle])

    async def replace(vin, region):
        account.coordinator.async_set_updated_data([replacement])
        return rest_status(False)

    client.get_vehicle_status_21mm = AsyncMock(side_effect=replace)
    await refresh_after_command(
        account.coordinator, vehicle.vin, RemoteRequestCommand.DoorLock, delay=0
    )

    client.get_vehicle_status_21mm.assert_awaited_once()
    replacement_read.assert_awaited_once()
    assert account.coordinator.data == [replacement]
    assert replacement.doors_locked is True


async def test_followup_stops_after_the_vehicle_is_removed(hass, setup_vehicles):
    client = types.SimpleNamespace()
    vehicle = make_vehicle(client)
    account = await setup_vehicles([vehicle])

    async def remove(vin, region):
        account.coordinator.async_set_updated_data([])
        return rest_status(False)

    client.get_vehicle_status_21mm = AsyncMock(side_effect=remove)
    await refresh_after_command(
        account.coordinator, vehicle.vin, RemoteRequestCommand.DoorLock, delay=0
    )

    client.get_vehicle_status_21mm.assert_awaited_once()


async def test_cached_status_cannot_replace_a_newer_push(hass, setup_vehicles):
    reading = asyncio.Event()
    release = asyncio.Event()

    async def read(vin, backdoor_type, region):
        reading.set()
        await release.wait()
        return graphql_status(False, "2026-10-22T12:00:00Z")

    vehicle = make_24mm_vehicle(
        types.SimpleNamespace(graphql_get_vehicle_status=AsyncMock(side_effect=read))
    )
    account = await setup_vehicles([vehicle])
    task = asyncio.create_task(
        refresh_after_command(
            account.coordinator, vehicle.vin, RemoteRequestCommand.DoorLock, delay=0
        )
    )
    await asyncio.wait_for(reading.wait(), 1)
    account.websocket.on_status(vehicle.vin, graphql_status(True, "2026-10-22T12:01:00Z"))
    release.set()
    await task

    assert vehicle.doors_locked is True
    vehicle._client.graphql_get_vehicle_status.assert_awaited_once()
    assert hass.states.get(entity_id(hass, "lock", "TESTVIN24.")).state == "locked"


async def test_unload_cancels_an_inflight_service_followup(hass, setup_vehicles):
    reading = asyncio.Event()
    cancelled = asyncio.Event()

    async def stall(*args):
        reading.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    read = AsyncMock(side_effect=stall)
    vehicle = make_vehicle(
        types.SimpleNamespace(remote_request_21mm=AsyncMock(), get_vehicle_status_21mm=read)
    )
    account = await setup_vehicles([vehicle])
    lock_id = entity_id(hass, "lock", "TESTVIN.")
    device_id = er.async_get(hass).async_get(lock_id).device_id
    await hass.services.async_call(DOMAIN, "door_lock", {"vehicle": device_id}, blocking=True)
    await asyncio.wait_for(reading.wait(), 1)
    polls = account.get_vehicles.await_count

    assert await hass.config_entries.async_unload(account.entry.entry_id)
    await hass.async_block_till_done()

    assert cancelled.is_set()
    read.assert_awaited_once()
    assert account.get_vehicles.await_count == polls
