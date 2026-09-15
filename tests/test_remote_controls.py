"""Remote controls advertised for each vehicle and transport."""

import types
import unittest
from unittest.mock import AsyncMock

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand


class RemoteControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_advertised_controls_use_generation_transport(self):
        for generation in (
            ApiVehicleGeneration.CY17, ApiVehicleGeneration.CY17PLUS,
            ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26,
        ):
            client = types.SimpleNamespace(remote_request_route=AsyncMock(), remote_request_24mm=AsyncMock())
            vehicle = behavior.make_17cy_vehicle(client) if generation == ApiVehicleGeneration.CY17 else behavior.make_vehicle(client)
            vehicle._generation = generation
            vehicle._extended_capabilities = {
                "hornCapable": True, "lightsCapable": True, "buzzerCapable": True,
                "powerWindowsOpenCapable": True, "powerWindowsCloseCapable": True,
                "moonroofCloseCapable": True, "trunkLockUnlockCapable": False,
                "powerTailgateCapable": True,
            }
            for command, wire_command in (
                (RemoteRequestCommand.SoundHorn, "sound-horn"),
                (RemoteRequestCommand.HeadlightsOn, "headlight-on"),
                (RemoteRequestCommand.SoundBuzzer, "buzzer-warning"),
                (RemoteRequestCommand.WindowsOpen, "power-window-open"),
                (RemoteRequestCommand.WindowsClose, "power-window-close"),
                (RemoteRequestCommand.MoonroofClose, "sunroof-close"),
                (RemoteRequestCommand.TrunkUnlock, "trunk-unlock"),
                (RemoteRequestCommand.TrunkLock, "trunk-lock"),
            ):
                with self.subTest(generation=generation, command=command):
                    if vehicle.uses_appsync and command == RemoteRequestCommand.TrunkLock:
                        with self.assertRaises(ValueError):
                            await vehicle.send_command(command)
                        continue
                    await vehicle.send_command(command)
                    if vehicle.uses_appsync:
                        client.remote_request_24mm.assert_awaited_with(vehicle.vin, wire_command, vehicle.region)
                        client.remote_request_route.assert_not_awaited()
                    else:
                        client.remote_request_route.assert_awaited_with(
                            vehicle.vin, generation.value, wire_command, vehicle.region, vehicle.brand,
                        )
                        client.remote_request_24mm.assert_not_awaited()

    async def test_window_directions_and_sunroof_follow_explicit_capabilities(self):
        vehicle = behavior.make_vehicle()
        vehicle._extended_capabilities = {
            "powerWindowsCapable": True, "powerWindowsOpenCapable": True,
            "powerWindowsCloseCapable": False, "moonroof": True,
        }
        entities = []
        coordinator = ha.DataUpdateCoordinator([vehicle])
        await ha.button.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        names = {entity.sensor_name for entity in entities}
        self.assertIn("Open Windows", names)
        self.assertNotIn("Close Windows", names)
        self.assertNotIn("Close Sunroof", names)
        self.assertNotIn("Sound Horn", names)
        vehicle._extended_capabilities["powerWindowsCloseCapable"] = True
        vehicle._extended_capabilities["moonroofCloseCapable"] = True
        coordinator.notify_listeners()
        names = {entity.sensor_name for entity in entities}
        self.assertIn("Close Windows", names)
        self.assertIn("Close Sunroof", names)
        vehicle._feature_flags = {"remoteCommands": 2}
        self.assertFalse(any(entity.available for entity in entities if entity.sensor_name in {
            "Open Windows", "Close Windows", "Close Sunroof",
        }))

    async def test_unknown_capability_or_inactive_subscription_cannot_send_new_command(self):
        client = types.SimpleNamespace(remote_request_route=AsyncMock())
        vehicle = behavior.make_vehicle(client)
        for capabilities, subscribed in (
            ({}, True), ({"hornCapable": False}, True),
            ({"hornCapable": "true"}, True), ({"hornCapable": True}, False),
        ):
            vehicle._extended_capabilities = capabilities
            vehicle._has_remote_subscription = subscribed
            with self.subTest(capabilities=capabilities, subscribed=subscribed), self.assertRaises(ValueError):
                await vehicle.send_command(RemoteRequestCommand.SoundHorn)
        client.remote_request_route.assert_not_awaited()
