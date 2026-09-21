"""Reported charging choices and confirmed settings changes."""

import asyncio
from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_appsync_transport as transport
import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import select
from custom_components.toyota_na.charging_helpers import charge_options, current_charge_option
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


CHARGING = {
    "lastUpdateDateTime": "2026-09-14T12:00:00Z",
    "limitSelectionValues": ["Full", "90%", "80%"],
    "chargeSettings": {
        "targetLimit": {"value": "80", "unit": "%"},
        "maxACCurrent": {"value": "Max", "setting": "127"},
        "maxDCPower": {"value": "50kW", "setting": "50"},
        "electricSupplyModeLimit": {"value": "30%", "setting": "30"},
        "acCurrentSelections": [
            {"key": "Max", "enabled": True}, {"key": "8A", "enabled": True},
            {"key": "16A", "enabled": False}, {"key": "Broken", "enabled": True},
        ],
        "dcPowerSelections": [{"key": "Max", "enabled": True}, {"key": "50kW", "enabled": True}],
        "electricSupplyLimitSelections": [{"key": "30%", "enabled": True}],
    },
}


def status(charging=CHARGING):
    return {"electric": {"charging": deepcopy(charging)}}


class ChargingSettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_choices_follow_enabled_metadata_and_special_wire_values(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status(status())
        settings = vehicle.charge_settings
        self.assertEqual({"100%": 100, "90%": 90, "80%": 80}, charge_options(settings, "targetLimit"))
        self.assertEqual({"Max": 127, "8 A": 8}, charge_options(settings, "maxACCurrent"))
        self.assertEqual({"Max": 1, "50 kW": 50}, charge_options(settings, "maxDCPower"))
        self.assertEqual({"30%": 30}, charge_options(settings, "electricSupplyModeLimit"))
        self.assertEqual("Max", current_charge_option(settings, "maxACCurrent"))
        self.assertEqual("50 kW", current_charge_option(settings, "maxDCPower"))
        self.assertEqual("80%", current_charge_option(settings, "targetLimit"))
        self.assertEqual("30%", current_charge_option(settings, "electricSupplyModeLimit"))

    async def test_default_target_choices_require_reported_support(self):
        self.assertEqual({}, charge_options({}, "targetLimit"))
        self.assertEqual(
            {f"{value}%": value for value in range(100, 39, -10)},
            charge_options({"targetLimit": {"value": 80}}, "targetLimit"),
        )

    async def test_selects_appear_only_on_supported_vehicles(self):
        vehicle = behavior.make_24mm_vehicle()
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await select.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        self.assertFalse(entities)
        vehicle.apply_graphql_status(status())
        coordinator.notify_listeners()
        self.assertEqual(4, len(entities))
        for entity in entities:
            self.assertTrue(entity.available)
        vehicle._generation = ApiVehicleGeneration.MM21
        self.assertFalse(any(entity.available for entity in entities))
        vehicle._generation = ApiVehicleGeneration.BEV26
        self.assertTrue(all(entity.available for entity in entities))
        vehicle._has_remote_subscription = False
        self.assertFalse(any(entity.available for entity in entities))

    async def test_update_validates_fresh_choices_and_reads_back_confirmed_state(self):
        updated = status()
        updated["electric"]["charging"]["chargeSettings"]["targetLimit"]["value"] = "90"
        client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(side_effect=[status(), updated]),
            update_charge_settings=AsyncMock(),
        )
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle.apply_graphql_status(status())
        await vehicle.set_charge_setting("targetLimit", "90%")
        client.update_charge_settings.assert_awaited_once_with(vehicle.vin, "chargingTargetLimit", 90, vehicle.region)
        self.assertEqual("90%", current_charge_option(vehicle.charge_settings, "targetLimit"))

        fresh = status()
        fresh["electric"]["charging"]["chargeSettings"]["acCurrentSelections"][0]["enabled"] = False
        client.graphql_get_vehicle_status.side_effect = [fresh]
        client.update_charge_settings.reset_mock()
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await vehicle.set_charge_setting("maxACCurrent", "Max")
        client.update_charge_settings.assert_not_awaited()

    async def test_charge_and_power_supply_controls_follow_their_own_feature_flags(self):
        client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(return_value=status()),
            update_charge_settings=AsyncMock(),
        )
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle.apply_graphql_status(status())
        coordinator = ha.DataUpdateCoordinator([vehicle])
        for field, feature, option in (
            ("targetLimit", "chargeSetting", "90%"),
            ("maxACCurrent", "chargeSetting", "Max"),
            ("maxDCPower", "chargeSetting", "Max"),
            ("electricSupplyModeLimit", "powerSupply", "30%"),
        ):
            entity = select.ToyotaChargeSelect(field, ha.ConfigEntry(), coordinator, field, vehicle.vin)
            for value in (0, 2, None, True, "1"):
                with self.subTest(field=field, value=value):
                    vehicle._feature_flags = {"remoteCommands": 1, "chargeSetting": 1, "powerSupply": 1, feature: value}
                    self.assertFalse(entity.available)
                    with self.assertRaisesRegex(ValueError, "unavailable"):
                        await vehicle.set_charge_setting(field, option)
            vehicle._feature_flags = {"remoteCommands": 2, feature: 1}
            self.assertTrue(entity.available)
            await vehicle.set_charge_setting(field, option)
        self.assertEqual(4, client.update_charge_settings.await_count)
        self.assertEqual(8, client.graphql_get_vehicle_status.await_count)

    async def test_disabled_charging_controls_preserve_returned_readings(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle._feature_flags = {"remoteCommands": 1, "chargeSetting": 2, "powerSupply": 2}
        vehicle.apply_graphql_status(status())
        entities = []
        await select.async_setup_entry(ha.FakeHass(ha.DataUpdateCoordinator([vehicle])), ha.ConfigEntry(), lambda added, update: entities.extend(added))
        self.assertEqual([], entities)
        self.assertEqual("80%", current_charge_option(vehicle.charge_settings, "targetLimit"))
        self.assertEqual("80", vehicle.features[VehicleFeatures.ChargeTargetLimit].value)

    async def test_failure_does_not_optimistically_change_setting(self):
        client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(return_value=status()),
            update_charge_settings=AsyncMock(side_effect=RuntimeError("Vehicle rejected setting")),
        )
        vehicle = behavior.make_24mm_vehicle(client)
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await vehicle.set_charge_setting("targetLimit", "90%")
        self.assertEqual("80%", current_charge_option(vehicle.charge_settings, "targetLimit"))

    async def test_partial_read_without_fresh_settings_cannot_change_cached_choices(self):
        client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(return_value={"telemetry": {"odo": {"value": 100}}}),
            update_charge_settings=AsyncMock(),
        )
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle.apply_graphql_status(status())
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await vehicle.set_charge_setting("targetLimit", "90%")
        client.update_charge_settings.assert_not_awaited()

    async def test_older_or_partial_settings_cannot_erase_newer_choices(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.apply_graphql_status(status())
        old = status()
        old["electric"]["charging"]["lastUpdateDateTime"] = "2026-09-14T11:00:00Z"
        old["electric"]["charging"]["limitSelectionValues"] = ["100"]
        vehicle.apply_graphql_status(old)
        vehicle.apply_graphql_status({"electric": {"charging": {"chargeSettings": {"targetLimit": None}}}})
        self.assertEqual(3, len(charge_options(vehicle.charge_settings, "targetLimit")))
        replacement = behavior.make_24mm_vehicle()
        replacement.inherit_state(vehicle)
        self.assertIs(replacement.charge_settings, vehicle.charge_settings)

    async def test_power_supply_stop_is_available_only_while_active(self):
        client = types.SimpleNamespace(remote_request_24mm=AsyncMock())
        vehicle = behavior.make_24mm_vehicle(client)
        for state in ("external_power_active", "external_power_active_hybrid"):
            vehicle.apply_graphql_status({"electric": {"charging": {"chargingState": state}}})
            await vehicle.send_command(RemoteRequestCommand.PowerSupplyStop)
            client.remote_request_24mm.assert_awaited_with(vehicle.vin, "power-supply-stop", vehicle.region)
        vehicle.apply_graphql_status({"electric": {"charging": {"chargingState": "charging"}}})
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.PowerSupplyStop))


class ChargingTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_setting_mutations_wait_for_callback_after_subscription(self):
        for variable, value, operation, key, variables in (
            ("currentCharge", 127, "PostChargeSettings", "postChargeSettings", {"currentCharge": 127}),
            ("minimumElectricSupply", 30, "PostPowerSupplyModeLimit", "executeRemoteCommand",
             {"command": "set-power-supply", "minimumElectricSupply": "30"}),
        ):
            with self.subTest(variable=variable):
                websocket = transport._WebSocket()
                async def mutate(*args, **kwargs):
                    self.assertEqual(2, websocket.stage)
                    self.assertEqual(operation, args[0])
                    self.assertEqual(variables, args[2])
                    self.assertEqual("CA", kwargs["region"])
                    return {key: {"payload": {"correlationId": "correlation", "requestNo": 42}}}
                client = types.SimpleNamespace(auth=transport._Auth(), graphql_request=AsyncMock(side_effect=mutate))
                with patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
                    result = await transport.patch_client.update_charge_settings(client, "TESTVIN24", variable, value, "CA")
                self.assertEqual("COMPLETED", result["status"])
                self.assertEqual(4, websocket.stage)

    async def test_commands_for_one_vehicle_are_serialized(self):
        client = types.SimpleNamespace()
        active = 0
        peak = 0
        async def execute(*args, **kwargs):
            nonlocal active, peak
            active += 1
            peak = max(active, peak)
            await asyncio.sleep(0)
            active -= 1
        with patch.object(transport.patch_client, "_execute_appsync_operation", side_effect=execute):
            await asyncio.gather(*(
                transport.patch_client._run_appsync_operation(client, "TESTVIN", None, "CA")
                for _ in range(3)
            ))
        self.assertEqual(1, peak)
