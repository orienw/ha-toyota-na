"""Remote controls advertised for each vehicle and transport."""

import types
from unittest.mock import AsyncMock

import pytest
from common import entity_id, make_17cy_vehicle, make_vehicle

from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
)


async def test_advertised_controls_use_generation_transport(subtests):
    for generation in (
        ApiVehicleGeneration.CY17,
        ApiVehicleGeneration.CY17PLUS,
        ApiVehicleGeneration.MM21,
        ApiVehicleGeneration.MM24,
        ApiVehicleGeneration.BEV26,
    ):
        client = types.SimpleNamespace(
            remote_request_route=AsyncMock(), remote_request_24mm=AsyncMock()
        )
        vehicle = (
            make_17cy_vehicle(client)
            if generation == ApiVehicleGeneration.CY17
            else make_vehicle(client)
        )
        vehicle._generation = generation
        vehicle._extended_capabilities = {
            "hornCapable": True,
            "lightsCapable": True,
            "buzzerCapable": True,
            "powerWindowsOpenCapable": True,
            "powerWindowsCloseCapable": True,
            "moonroofCloseCapable": True,
            "trunkLockUnlockCapable": False,
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
            with subtests.test(generation=generation, command=command):
                if vehicle.uses_appsync and command == RemoteRequestCommand.TrunkLock:
                    with pytest.raises(ValueError):
                        await vehicle.send_command(command)
                    continue
                await vehicle.send_command(command)
                if vehicle.uses_appsync:
                    client.remote_request_24mm.assert_awaited_with(
                        vehicle.vin, wire_command, vehicle.region
                    )
                    client.remote_request_route.assert_not_awaited()
                else:
                    client.remote_request_route.assert_awaited_with(
                        vehicle.vin,
                        generation.value,
                        wire_command,
                        vehicle.region,
                        vehicle.brand,
                    )
                    client.remote_request_24mm.assert_not_awaited()


async def test_window_directions_and_sunroof_follow_explicit_capabilities(hass, setup_vehicles):
    vehicle = make_vehicle()
    vehicle._extended_capabilities = {
        "powerWindowsCapable": True,
        "powerWindowsOpenCapable": True,
        "powerWindowsCloseCapable": False,
        "moonroof": True,
    }
    account = await setup_vehicles([vehicle])

    def button(name):
        return entity_id(hass, "button", f"TESTVIN.{name}")

    assert button("Open Windows")
    assert button("Close Windows") is None
    assert button("Close Sunroof") is None
    assert button("Sound Horn") is None

    vehicle._extended_capabilities["powerWindowsCloseCapable"] = True
    vehicle._extended_capabilities["moonroofCloseCapable"] = True
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()
    assert button("Close Windows")
    assert button("Close Sunroof")

    vehicle._feature_flags = {"remoteCommands": 2}
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()
    for name in ("Open Windows", "Close Windows", "Close Sunroof"):
        assert hass.states.get(button(name)).state == "unavailable"


async def test_unknown_capability_or_inactive_subscription_cannot_send_new_command(subtests):
    client = types.SimpleNamespace(remote_request_route=AsyncMock())
    vehicle = make_vehicle(client)
    for capabilities, subscribed in (
        ({}, True),
        ({"hornCapable": False}, True),
        ({"hornCapable": "true"}, True),
        ({"hornCapable": True}, False),
    ):
        vehicle._extended_capabilities = capabilities
        vehicle._has_remote_subscription = subscribed
        with (
            subtests.test(capabilities=capabilities, subscribed=subscribed),
            pytest.raises(ValueError),
        ):
            await vehicle.send_command(RemoteRequestCommand.SoundHorn)
    client.remote_request_route.assert_not_awaited()
