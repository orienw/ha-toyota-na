"""Cached readings and shutdown independently of command access."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import binary_sensor, patch_client, sensor
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


class ReadAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_21mm_legacy_fallback_restores_all_nine_ev_entities(self):
        status = {"vehicleInfo": {"acquisitionDatetime": "2026-09-27T12:00:00Z", "chargeInfo": {
            "evDistance": 200, "evDistanceAC": 180, "evDistanceUnit": "km",
            "chargeRemainingAmount": 85, "plugStatus": 40, "remainingChargeTime": 90,
            "evTravelableDistance": 200, "chargeType": 2, "connectorStatus": 5,
        }}}
        expected = {
            "EV Range": 200, "EV Range AC": 180, "EV Battery Level": 85,
            "Plug Status": "charging", "Remaining Charge Time": 90,
            "EV Travelable Distance": 200, "Charge Type": 2, "Connector Status": "locked",
        }
        for response in (
            {"vehicleInfo": {"chargeInfo": None}},
            {"vehicleInfo": {"chargeInfo": {"gasolineTravelableDistance": 0}}},
            patch_client.aiohttp.ClientResponseError(MagicMock(), (), status=401),
        ):
            with self.subTest(response=response):
                client = types.SimpleNamespace(
                    api_get=AsyncMock(side_effect=[response, status]),
                    get_telemetry=AsyncMock(return_value={}),
                    get_vehicle_status_21mm=AsyncMock(return_value={}),
                    get_engine_status_21mm=AsyncMock(return_value={}),
                    get_climate_settings=AsyncMock(return_value=None),
                )
                client.get_electric_status = types.MethodType(patch_client.get_electric_status, client)
                vehicle = behavior.make_vehicle(client)
                vehicle._has_electric = True
                await vehicle.update()
                coordinator = ha.DataUpdateCoordinator([vehicle])
                hass, entry, entities = ha.FakeHass(coordinator), ha.ConfigEntry(), []
                for platform in (sensor, binary_sensor):
                    await platform.async_setup_entry(hass, entry, lambda added, update: entities.extend(added))
                by_name = {entity.sensor_name: entity for entity in entities}
                self.assertEqual(set(expected) | {"Charging Status"}, set(by_name))
                for name, value in expected.items():
                    self.assertTrue(by_name[name].available, name)
                    self.assertEqual(value, by_name[name].native_value, name)
                self.assertTrue(by_name["Charging Status"].available)
                self.assertTrue(by_name["Charging Status"].is_on)
                self.assertEqual(["v3/electric/status", "v2/electric/status"], [
                    call.args[0] for call in client.api_get.call_args_list
                ])

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

    async def test_unload_cancels_pending_post_command_poll(self):
        vehicle = ha.FakeVehicle({RemoteRequestCommand.EngineStart})
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        entity = ha.button.ToyotaCommandButton(
            RemoteRequestCommand.EngineStart, "mdi:engine", entry,
            coordinator, "Remote Start", vehicle.vin,
        )
        entity.hass = hass
        with patch.object(ha.button, "COMMAND_REFRESH_DELAY", 60):
            await entity.async_press()
            await asyncio.sleep(0)
            for callback in entry.unload_callbacks:
                callback()
            await asyncio.gather(*hass.tasks, return_exceptions=True)
        self.assertEqual(0, coordinator.refreshes)
        self.assertTrue(hass.tasks[0].cancelled())
