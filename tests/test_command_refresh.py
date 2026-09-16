"""Engine follow-up polls only the requested vehicle and stops promptly."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_button as ha
import test_vehicle_behavior as behavior
from custom_components.toyota_na import command_refresh
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


class CommandRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_engine_button_polls_until_started_without_account_polls_or_wakes(self):
        client = types.SimpleNamespace(
            remote_request_21mm=AsyncMock(),
            get_engine_status_21mm=AsyncMock(side_effect=[{"status": "OFF"}, {"status": "ON"}]),
        )
        vehicle = behavior.make_vehicle(client)
        other = ha.FakeVehicle(set(), vin="OTHER")
        other.poll_engine_status = AsyncMock()
        coordinator = ha.DataUpdateCoordinator([vehicle, other])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        entity = ha.button.ToyotaCommandButton(
            RemoteRequestCommand.EngineStart, "mdi:engine", entry,
            coordinator, "Remote Start", vehicle.vin,
        )
        entity.hass = hass
        with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
            await entity.async_press()
            await asyncio.gather(*hass.tasks)
        client.remote_request_21mm.assert_awaited_once()
        self.assertEqual(2, client.get_engine_status_21mm.await_count)
        self.assertTrue(vehicle.features[VehicleFeatures.RemoteStartStatus].on)
        self.assertEqual(0, coordinator.refreshes)
        other.poll_engine_status.assert_not_awaited()

    async def test_stop_followup_is_limited_even_if_status_never_changes(self):
        for factory, generation, getter in (
            (behavior.make_17cy_vehicle, ApiVehicleGeneration.CY17, "get_engine_status_17cy"),
            (behavior.make_vehicle, ApiVehicleGeneration.CY17PLUS, "get_engine_status_17cyplus"),
            (behavior.make_vehicle, ApiVehicleGeneration.MM21, "get_engine_status_21mm"),
        ):
            for status, count in (("ON", 4), ("OFF", 1)):
                with self.subTest(generation=generation, status=status):
                    read = AsyncMock(return_value={"status": status})
                    vehicle = factory(types.SimpleNamespace(**{getter: read}))
                    vehicle._generation = generation
                    coordinator = ha.DataUpdateCoordinator([vehicle])
                    with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
                        await ha.integration_runtime._refresh_coordinator_after_command(
                            coordinator, vehicle.vin, RemoteRequestCommand.EngineStop,
                        )
                    self.assertEqual(count, read.await_count)
                    self.assertEqual(0, coordinator.refreshes)

    async def test_followup_does_not_treat_cached_engine_state_as_confirmation(self):
        client = types.SimpleNamespace(get_engine_status_21mm=AsyncMock(return_value={}))
        vehicle = behavior.make_vehicle(client)
        vehicle._parse_engine_status({"status": "ON"})
        coordinator = ha.DataUpdateCoordinator([vehicle])
        with patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0):
            await command_refresh.refresh_after_command(coordinator, vehicle.vin, RemoteRequestCommand.EngineStart)
        self.assertEqual(4, client.get_engine_status_21mm.await_count)

    async def test_missing_vehicle_or_elapsed_deadline_stops_followup(self):
        vehicle = behavior.make_vehicle()
        vehicle.poll_engine_status = AsyncMock()
        coordinator = ha.DataUpdateCoordinator([vehicle])
        with patch.object(command_refresh, "ENGINE_STATUS_TIMEOUT", 0):
            await command_refresh.refresh_after_command(coordinator, vehicle.vin, RemoteRequestCommand.EngineStart)
        coordinator.data = []
        await command_refresh.refresh_after_command(coordinator, vehicle.vin, RemoteRequestCommand.EngineStart)
        vehicle.poll_engine_status.assert_not_awaited()

    async def test_unload_cancels_engine_followup(self):
        vehicle = behavior.make_vehicle(types.SimpleNamespace(remote_request_21mm=AsyncMock()))
        vehicle.poll_engine_status = AsyncMock()
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        entity = ha.button.ToyotaCommandButton(RemoteRequestCommand.EngineStart, "mdi:engine", entry, coordinator, "Start", vehicle.vin)
        entity.hass = hass
        await entity.async_press()
        await asyncio.sleep(0)
        for callback in entry.unload_callbacks:
            callback()
        await asyncio.gather(*hass.tasks, return_exceptions=True)
        self.assertTrue(hass.tasks[0].cancelled())
        vehicle.poll_engine_status.assert_not_awaited()

    async def test_stalled_engine_read_is_cancelled_at_the_followup_deadline(self):
        cancelled = asyncio.Event()

        async def stall():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        vehicle = behavior.make_vehicle()
        vehicle.poll_engine_status = AsyncMock(side_effect=stall)
        coordinator = ha.DataUpdateCoordinator([vehicle])
        with (
            patch.object(command_refresh, "ENGINE_STATUS_INTERVAL", 0),
            patch.object(command_refresh, "ENGINE_STATUS_TIMEOUT", 0.02),
        ):
            await command_refresh.refresh_after_command(coordinator, vehicle.vin, RemoteRequestCommand.EngineStop)
        self.assertTrue(cancelled.is_set())
        vehicle.poll_engine_status.assert_awaited_once()
