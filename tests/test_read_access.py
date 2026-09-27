"""Cached readings and shutdown independently of command access."""

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_appsync_transport as http
import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import binary_sensor, patch_client, sensor
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


class ReadAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_status_capability_does_not_suppress_scheduled_wakes(self):
        for flags, enabled in (
            (None, True), ({}, True),
            ({"evBattery": 1, "evVehicleStatus": 1}, True),
            ({"vehicleState": None}, True), ({"vehicleState": 1}, True),
            ({"vehicleState": 0}, False), ({"vehicleState": 2}, False),
            ({"vehicleState": True}, False), ({"vehicleState": "1"}, False),
        ):
            for subscribed in (True, False):
                with self.subTest(flags=flags, subscribed=subscribed):
                    vehicle = behavior.make_24mm_vehicle()
                    vehicle._feature_flags = flags
                    vehicle._has_remote_subscription = subscribed
                    vehicle.poll_vehicle_refresh = AsyncMock()
                    coordinator = ha.DataUpdateCoordinator([vehicle])
                    hass, entry = ha.FakeHass(coordinator), ha.ConfigEntry()
                    with (
                        patch.object(ha.integration_runtime, "get_vehicles", AsyncMock(return_value=[vehicle])),
                        patch.object(ha.integration_runtime, "automatic_wake_due", return_value=True),
                        patch.object(ha.integration_runtime, "_refresh_coordinator_after_command", AsyncMock()),
                    ):
                        await ha.integration_runtime.update_vehicles_status(
                            hass, types.SimpleNamespace(), entry, coordinator,
                        )
                    await asyncio.gather(*hass.tasks)
                    self.assertEqual(vehicle.poll_vehicle_refresh.await_count, int(enabled and subscribed))
                    self.assertEqual(vehicle.supports_command(RemoteRequestCommand.Refresh), enabled and subscribed)


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


class StatusRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = types.SimpleNamespace(
            auth=types.SimpleNamespace(
                get_guid=AsyncMock(return_value="guid"), get_device_id=lambda: "device",
                get_access_token=AsyncMock(return_value="token"),
            ),
            get_telemetry=AsyncMock(return_value={}),
        )
        self.client.graphql_request = types.MethodType(patch_client.graphql_request, self.client)
        self.client.graphql_get_vehicle_status = types.MethodType(patch_client.graphql_get_vehicle_status, self.client)
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        session_patch = patch.object(patch_client.aiohttp, "ClientSession", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.status = {"vin": "TESTVIN24", "telemetry": {"odo": {"value": 1234, "unit": "mi"}}, "electric": None}
        self.electric = {"battery": {"stateOfChargeDisplay": {"value": 0, "unit": "%"}}, "charging": {"chargingState": "charging"}}
        self.error = {"path": ["getVehicleStatus", "electric", "charging", "chargeSettings", "schedules"], "message": "Field failed"}

    async def test_recovery_creates_battery_and_charging_entities_after_vehicle_update(self):
        healthy = {**self.status, "electric": self.electric}
        for response, calls in (
            ({"errors": [{"errorType": "ValidationError"}]}, 2),
            ({"data": {"getVehicleStatus": self.status}, "errors": [self.error]}, 2),
            ({"data": {"getVehicleStatus": healthy}, "errors": [self.error]}, 1),
        ):
            with self.subTest(response=response):
                vehicle = behavior.make_24mm_vehicle(self.client)
                self.session.post.reset_mock()
                self.session.post.side_effect = [
                    http._Response(200, response),
                    http._Response(200, {"data": {"getVehicleStatus": healthy}}),
                ]
                await vehicle.update()
                coordinator = ha.DataUpdateCoordinator([vehicle])
                hass, entry, entities = ha.FakeHass(coordinator), ha.ConfigEntry(), []
                for platform in (sensor, binary_sensor):
                    await platform.async_setup_entry(hass, entry, lambda added, update: entities.extend(added))
                by_name = {entity.sensor_name: entity for entity in entities}
                self.assertEqual(0, by_name["EV Battery Level"].native_value)
                self.assertTrue(by_name["Charging Status"].is_on)
                self.assertEqual(1234, by_name["Odometer"].native_value)
                self.assertEqual("TESTVIN24.EV Battery Level", by_name["EV Battery Level"].unique_id)
                self.assertEqual("TESTVIN24.Charging Status", by_name["Charging Status"].unique_id)
                self.assertEqual(calls, self.session.post.call_count)

    async def test_recovered_sections_do_not_inherit_a_newer_primary_timestamp(self):
        for fallback_time in ("2026-09-27T11:00:00Z", None):
            with self.subTest(fallback_time=fallback_time):
                vehicle = behavior.make_24mm_vehicle(self.client)
                vehicle.apply_graphql_status({"lastUpdateDateTime": "2026-09-27T11:30:00Z", "electric": {
                    "battery": {"stateOfChargeDisplay": {"value": 50}},
                    "charging": {"chargingState": "45"},
                }})
                primary = {**self.status, "lastUpdateDateTime": "2026-09-27T12:00:00Z"}
                fallback = {"lastUpdateDateTime": fallback_time, "electric": self.electric, "telemetry": {"odo": {"value": 999}}}
                self.session.post.side_effect = [
                    http._Response(200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}),
                    http._Response(200, {"data": {"getVehicleStatus": fallback}}),
                ]
                await vehicle.update()
                self.assertEqual(50, vehicle.features[VehicleFeatures.ChargeLevel].value)
                self.assertTrue(vehicle.features[VehicleFeatures.ChargingStatus].closed)
                self.assertEqual(1234, vehicle.features[VehicleFeatures.Odometer].value)
                vehicle.apply_graphql_status({"lastUpdateDateTime": "2026-09-27T11:45:00Z", "telemetry": {"odo": {"value": 999}}})
                self.assertEqual(1234, vehicle.features[VehicleFeatures.Odometer].value)

    async def test_charging_recovery_preserves_primary_readings_and_source_timestamps(self):
        for cached in (False, True):
            for fallback_time, preserve_cached in (("2026-09-27T11:00:00Z", True), (None, True), ("2026-09-27T13:00:00Z", False)):
                with self.subTest(cached=cached, fallback_time=fallback_time):
                    vehicle = behavior.make_24mm_vehicle(self.client)
                    if cached:
                        vehicle.apply_graphql_status({"lastUpdateDateTime": "2026-09-27T11:30:00Z", "electric": {
                            "battery": {"stateOfChargeDisplay": {"value": 50}},
                            "charging": {"chargingState": "45"},
                        }})
                    primary = {"vin": "TESTVIN24", "lastUpdateDateTime": "2026-09-27T12:00:00Z", "electric": {
                        "battery": {"stateOfChargeDisplay": {"value": 85}, "travelableDistance": {"value": 200}},
                        "gasoline": {"travelableDistance": {"value": 300}}, "charging": None,
                    }}
                    fallback = {"lastUpdateDateTime": fallback_time, "electric": self.electric}
                    self.session.post.reset_mock()
                    self.session.post.side_effect = [
                        http._Response(200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}),
                        http._Response(200, {"data": {"getVehicleStatus": fallback}}),
                    ]
                    await vehicle.update()
                    self.assertEqual(2, self.session.post.call_count)
                    self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                    self.assertEqual(200, vehicle.features[VehicleFeatures.ChargeDistance].value)
                    self.assertEqual(300, vehicle.features[VehicleFeatures.GasolineRange].value)
                    self.assertEqual(cached and preserve_cached, vehicle.features[VehicleFeatures.ChargingStatus].closed)
                    vehicle.apply_graphql_status({"lastUpdateDateTime": "2026-09-27T11:45:00Z", "electric": {
                        "battery": {"stateOfChargeDisplay": {"value": 10}},
                    }})
                    self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                    latest = "2026-09-27T12:00:00+00:00" if preserve_cached else "2026-09-27T13:00:00+00:00"
                    self.assertEqual(latest, vehicle._feature_timestamps[(VehicleFeatures.LastTimeStamp, "value")].isoformat())

    async def test_failed_charging_recovery_keeps_valid_battery_data(self):
        primary = {"electric": {"battery": {"stateOfChargeDisplay": {"value": 85}}, "charging": None}}
        for fallback in ({"electric": None}, {"electric": {"charging": None}}, {"electric": {"battery": {"stateOfChargeDisplay": {"value": 10}}}}):
            with self.subTest(fallback=fallback):
                vehicle = behavior.make_24mm_vehicle(self.client)
                self.session.post.reset_mock()
                self.session.post.side_effect = [
                    http._Response(200, {"data": {"getVehicleStatus": primary}, "errors": [self.error]}),
                    http._Response(200, {"data": {"getVehicleStatus": fallback}}),
                ]
                await vehicle.update()
                self.assertEqual(2, self.session.post.call_count)
                self.assertEqual(85, vehicle.features[VehicleFeatures.ChargeLevel].value)
                self.assertNotIn(VehicleFeatures.ChargingStatus, vehicle.features)
