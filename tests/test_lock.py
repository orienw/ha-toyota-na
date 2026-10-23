"""Native lock progress follows the requested command and reported door state."""

import asyncio
import types
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from common import entity_id, get_entity, make_24mm_vehicle
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening

from custom_components.toyota_na import lock as lock_module
from custom_components.toyota_na.patch_base_vehicle import VehicleFeatures


@pytest.fixture
async def lock_control(hass, setup_vehicles, monkeypatch):
    vehicle = make_24mm_vehicle(types.SimpleNamespace(remote_request_24mm=AsyncMock()))
    vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(
        closed=True, locked=False
    )
    account = await setup_vehicles([vehicle])
    monkeypatch.setattr(lock_module, "COMMAND_REFRESH_DELAY", 10)
    return vehicle, account, entity_id(hass, "lock", "TESTVIN24.")


def push_lock_state(account, locked):
    account.websocket.on_status(
        "TESTVIN24",
        {
            "vin": "TESTVIN24",
            "vehicleState": {
                "doors": {"driverSide": {"lock": {"status": "lock" if locked else "unlock"}}}
            },
        },
    )


@pytest.mark.parametrize("service", ["lock", "unlock"])
@pytest.mark.parametrize("locked", [True, False, None])
async def test_command_progress_keeps_its_requested_direction(
    hass, lock_control, freezer, service, locked
):
    vehicle, account, lock_id = lock_control
    vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(
        closed=True, locked=locked
    )
    account.coordinator.async_update_listeners()
    desired = service == "lock"
    final = "unknown" if locked is None else "locked" if locked else "unlocked"

    await hass.services.async_call("lock", service, {"entity_id": lock_id}, blocking=True)

    expected = final
    if locked is not None and locked != desired:
        expected = "locking" if desired else "unlocking"
    assert hass.states.get(lock_id).state == expected
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(lock_id).state == final


@pytest.mark.parametrize("service", ["lock", "unlock"])
async def test_status_push_finishes_progress_without_reversing_it(
    hass, lock_control, freezer, service
):
    _, account, lock_id = lock_control
    desired = service == "lock"
    push_lock_state(account, not desired)
    await hass.services.async_call("lock", service, {"entity_id": lock_id}, blocking=True)
    assert hass.states.get(lock_id).state == ("locking" if desired else "unlocking")

    push_lock_state(account, desired)

    final = "locked" if desired else "unlocked"
    assert hass.states.get(lock_id).state == final
    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert hass.states.get(lock_id).state == final


@pytest.mark.parametrize("services", [("lock", "unlock"), ("lock", "unlock", "lock")])
async def test_older_followup_does_not_clear_newer_command_progress(
    hass, lock_control, freezer, monkeypatch, services
):
    _, account, lock_id = lock_control
    finished = asyncio.Event()
    original_refresh = lock_module.ToyotaLock._background_refresh

    async def refresh(entity, command_id):
        await original_refresh(entity, command_id)
        finished.set()

    monkeypatch.setattr(lock_module.ToyotaLock, "_background_refresh", refresh)
    started = dt_util.utcnow()
    for index, service in enumerate(services):
        if index:
            freezer.tick(timedelta(seconds=3))
            push_lock_state(account, services[index - 1] == "lock")
        await hass.services.async_call("lock", service, {"entity_id": lock_id}, blocking=True)

    freezer.move_to(started + timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    # The first follow-up finishes while the newer one is still sleeping.
    await asyncio.wait_for(finished.wait(), 1)
    assert hass.states.get(lock_id).state == ("locking" if services[-1] == "lock" else "unlocking")
    freezer.move_to(started + timedelta(seconds=30))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_failed_followup_clears_command_progress(hass, lock_control, freezer, monkeypatch):
    _, account, lock_id = lock_control
    monkeypatch.setattr(
        account.coordinator, "async_request_refresh", AsyncMock(side_effect=RuntimeError("offline"))
    )
    await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    assert hass.states.get(lock_id).state == "locking"

    freezer.tick(timedelta(seconds=11))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()

    assert hass.states.get(lock_id).state == "unlocked"
    assert get_entity(hass, lock_id)._requested_command is None


async def test_unload_cancels_pending_lock_progress(hass, lock_control):
    _, account, lock_id = lock_control
    entity = get_entity(hass, lock_id)
    await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    assert hass.states.get(lock_id).state == "locking"
    polls = account.get_vehicles.await_count

    assert await hass.config_entries.async_unload(account.entry.entry_id)
    await hass.async_block_till_done()

    assert account.get_vehicles.await_count == polls
    assert entity._requested_command is None
