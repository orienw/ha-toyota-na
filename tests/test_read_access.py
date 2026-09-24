"""Cached readings and shutdown independently of command access."""

import types
import unittest
from unittest.mock import AsyncMock

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


class ReadAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_supported_generations_read_cached_status_without_subscription(self):
        rest_status = {"vehicleStatus": [{
            "category": "Driver Side", "sections": [{
                "section": "Door", "values": [{"value": "closed"}, {"value": "locked"}],
            }],
        }]}
        graphql_status = {"vehicleState": {"doors": {
            "driverSide": {"position": {"status": "closed"}, "lock": {"status": "locked"}},
        }}}
        for generation in (
            ApiVehicleGeneration.CY17, ApiVehicleGeneration.CY17PLUS,
            ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26,
        ):
            with self.subTest(generation=generation):
                client = types.SimpleNamespace(
                    get_telemetry=AsyncMock(return_value={}),
                    get_vehicle_status_17cy=AsyncMock(return_value=rest_status),
                    get_vehicle_status_17cyplus=AsyncMock(return_value=rest_status),
                    get_vehicle_status_21mm=AsyncMock(return_value=rest_status),
                    get_engine_status_17cy=AsyncMock(return_value={}),
                    get_engine_status_17cyplus=AsyncMock(return_value={}),
                    get_engine_status_21mm=AsyncMock(return_value={}),
                    graphql_get_vehicle_status=AsyncMock(return_value=graphql_status),
                    graphql_pre_wake=AsyncMock(),
                )
                vehicle = behavior.make_17cy_vehicle(client) if generation == ApiVehicleGeneration.CY17 else behavior.make_vehicle(client)
                vehicle._generation = generation
                vehicle._has_electric = False
                vehicle._has_remote_subscription = False
                vehicle._feature_flags = {"vehicleState": 2, "remoteCommands": 2}
                await vehicle.update()
                self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].locked)
                self.assertFalse(vehicle.supports_command(RemoteRequestCommand.Refresh))
                self.assertFalse(vehicle.supports_command(RemoteRequestCommand.DoorUnlock))
                client.graphql_pre_wake.assert_not_awaited()

    async def test_failed_unload_keeps_live_subscription_running(self):
        coordinator = ha.DataUpdateCoordinator([])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        handler = types.SimpleNamespace(stop=AsyncMock())
        hass.data[ha.DOMAIN][entry.entry_id]["ws_handler"] = handler
        hass.async_unload_platforms = AsyncMock(return_value=False)
        self.assertFalse(await ha.integration_runtime.async_unload_entry(hass, entry))
        handler.stop.assert_not_awaited()
        hass.async_unload_platforms.return_value = True
        self.assertTrue(await ha.integration_runtime.async_unload_entry(hass, entry))
        handler.stop.assert_awaited_once()
        self.assertNotIn(entry.entry_id, hass.data[ha.DOMAIN])
