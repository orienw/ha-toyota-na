"""Generation, feature availability, and electric vehicle controls."""

import asyncio
from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import number, switch
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures,
)
from custom_components.toyota_na.patch_vehicle import get_vehicles
from toyota_na.exceptions import LoginError


CLIMATE_SETTINGS = {
    "temperature": 22.0,
    "temperatureUnit": "C",
    "minTemp": 18.0,
    "maxTemp": 30.0,
    "tempInterval": 0.5,
    "settingsOn": True,
    "isCustomerSettings": True,
    "acOperations": [{"type": "frontDefogger", "value": "off"}],
    "extendedRuntime": {"enabled": False},
}


class FeatureTests(unittest.IsolatedAsyncioTestCase):
    def test_feature_states_and_legacy_fallback(self):
        vehicle = behavior.make_vehicle()
        for flags, available in (
            (None, True), ({}, False), ({"remoteCommands": 1}, True),
            ({"remoteCommands": 0}, False), ({"remoteCommands": 2}, False),
            ({"remoteCommands": None}, False), ({"remoteCommands": True}, False),
        ):
            with self.subTest(flags=flags):
                vehicle._feature_flags = flags
                self.assertEqual(vehicle.supports_command(RemoteRequestCommand.EngineStart), available)
        vehicle._feature_flags = None
        vehicle._has_remote_subscription = False
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.EngineStart))

    def test_climate_start_uses_capabilities_and_generation(self):
        for generation in ApiVehicleGeneration:
            if generation in (ApiVehicleGeneration.GR86, ApiVehicleGeneration.PRE17CY):
                continue
            vehicle = behavior.make_17cy_vehicle() if generation == ApiVehicleGeneration.CY17 else behavior.make_vehicle()
            vehicle._generation = generation
            vehicle._remote_capabilities = {"estartStopCapable": False}
            vehicle._extended_capabilities = {"remoteEConnectCapable": True}
            self.assertTrue(vehicle.supports_command(RemoteRequestCommand.EngineStart))
            vehicle._extended_capabilities = {}
            vehicle._legacy_capabilities = [{"name": "evremoteservice"}]
            self.assertEqual(vehicle.supports_command(RemoteRequestCommand.EngineStart), generation == ApiVehicleGeneration.CY17)

    async def test_26bev_discovery_routes_poll_commands_refresh_and_push(self):
        metadata = {**behavior.TWENTY_FOUR_MM_PHEV, "vin": "SYNTHETIC26BEV", "generation": "26BEV"}
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[metadata]),
            get_telemetry=AsyncMock(return_value={}),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            remote_request_24mm=AsyncMock(),
            graphql_pre_wake=AsyncMock(), graphql_confirm_subscription=AsyncMock(),
            graphql_refresh_status=AsyncMock(),
            auth=types.SimpleNamespace(get_guid=AsyncMock(return_value="guid")),
        )
        vehicle, = await get_vehicles(client)
        self.assertEqual(vehicle.api_generation, "26BEV")
        self.assertEqual(vehicle.endpoint_generation, "17CYPLUS")
        client.graphql_get_vehicle_status.assert_awaited_once_with("SYNTHETIC26BEV", "hatch", "CA")
        await vehicle.send_command(RemoteRequestCommand.EngineStart)
        client.remote_request_24mm.assert_awaited_once_with("SYNTHETIC26BEV", "engine-start", "CA")
        await vehicle.poll_vehicle_refresh()
        client.graphql_refresh_status.assert_awaited_once_with("SYNTHETIC26BEV", "CA")
        self.assertEqual(ha.integration_runtime._websocket_contexts([vehicle]), {
            "SYNTHETIC26BEV": {"region": "CA", "backdoor_type": "hatch"},
        })

    async def test_unsubscribed_appsync_ev_can_read_but_cannot_command_or_wake(self):
        for generation in (ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            client = types.SimpleNamespace(
                get_telemetry=AsyncMock(return_value={}),
                graphql_get_vehicle_status=AsyncMock(return_value={"electric": {
                    "battery": {"chargeRemainingAmount": {"value": 63, "unit": "%"}},
                }}),
            )
            vehicle = behavior.make_24mm_vehicle(client)
            vehicle._generation = generation
            vehicle._has_remote_subscription = False
            vehicle._feature_flags = {"evBattery": 1, "remoteCommands": 0, "vehicleState": 0}
            await vehicle.update()
            self.assertEqual(vehicle.features[VehicleFeatures.ChargeLevel].value, 63)
            self.assertFalse(vehicle.supports_command(RemoteRequestCommand.EngineStart))
            self.assertFalse(vehicle.supports_command(RemoteRequestCommand.Refresh))
            self.assertIn(vehicle.vin, ha.integration_runtime._websocket_contexts([vehicle]))
            vehicle._feature_flags["evBattery"] = 2
            client.graphql_get_vehicle_status.reset_mock()
            client.graphql_get_vehicle_status.return_value["electric"]["battery"]["chargeRemainingAmount"]["value"] = 64
            await vehicle.update()
            client.graphql_get_vehicle_status.assert_awaited_once()
            self.assertEqual(vehicle.features[VehicleFeatures.ChargeLevel].value, 64)
            self.assertIn(vehicle.vin, ha.integration_runtime._websocket_contexts([vehicle]))

    def test_app_feature_flags_do_not_hide_observed_sensor_values(self):
        vehicle = behavior.make_vehicle()
        vehicle.features[VehicleFeatures.ChargeLevel] = ha.ToyotaNumeric(80, "%")
        coordinator = ha.DataUpdateCoordinator([vehicle])
        sensor = ha.sensor_platform.ToyotaSensor(
            VehicleFeatures.ChargeLevel, "mdi:battery", "%", "measurement",
            coordinator, "Charge Level", vehicle.vin,
        )
        self.assertEqual(sensor.state, 80)
        for flags in ({}, {"evBattery": 0}, {"evBattery": 2}):
            vehicle._feature_flags = flags
            self.assertTrue(sensor.available)
            self.assertEqual(sensor.state, 80)

    def test_app_maintenance_does_not_hide_observed_lock_state(self):
        vehicle = behavior.make_vehicle()
        vehicle.features[VehicleFeatures.FrontDriverDoor] = ha.ToyotaLockableOpening(
            closed=True, locked=True,
        )
        entity = ha.lock_platform.ToyotaLock(
            ha.ConfigEntry(), ha.DataUpdateCoordinator([vehicle]), "", vehicle.vin,
        )
        self.assertTrue(entity.is_locked)
        vehicle._feature_flags = {"vehicleState": 2, "remoteCommands": 1}
        self.assertTrue(entity.available)
        self.assertTrue(entity.is_locked)
        vehicle._feature_flags["vehicleState"] = 1
        self.assertTrue(entity.is_locked)

    async def test_21mm_zero_status_locked_values_reach_lock_controls(self):
        class Client:
            get_vehicle_status_21mm = behavior.get_vehicle_status_21mm
            api_get = AsyncMock()
            get_telemetry = AsyncMock(return_value=None)
            get_engine_status_21mm = AsyncMock(return_value=None)
            remote_request_21mm = AsyncMock()

        client = Client()
        vehicle = behavior.make_vehicle(client)
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        entities = []
        await ha.lock_platform.async_setup_entry(hass, entry, lambda added, update: entities.extend(added))
        entity, = entities
        entity.hass = hass

        for value, flag, expected in (("locked", 0, True), ("unlocked", 1, False), ("locked", 0, True)):
            with self.subTest(value=value, flag=flag):
                client.api_get.return_value = {"status": {"vehicleStatus": [
                    {"category": category, "sections": [{"section": "Door", "values": [
                        {"value": "closed", "status": 0}, {"value": value, "status": flag},
                    ]}]}
                    for category in ("Driver Side", "Passenger Side")
                ]}}
                await vehicle.update()

                self.assertTrue(entity.available)
                self.assertIs(entity.is_locked, expected)
                self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].closed)
                self.assertTrue(vehicle.features[VehicleFeatures.FrontPassengerDoor].closed)
                with patch.object(ha.lock_platform, "COMMAND_REFRESH_DELAY", 0):
                    if expected:
                        await entity.async_unlock()
                    else:
                        await entity.async_lock()
                    await asyncio.gather(*hass.tasks)
                client.remote_request_21mm.assert_awaited_with(
                    vehicle.vin, "door-unlock" if expected else "door-lock", "US",
                )
                self.assertFalse(entity._state_changing)

    async def test_legacy_reads_and_entities_do_not_require_remote_subscription(self):
        client = types.SimpleNamespace(
            get_telemetry=AsyncMock(return_value={}),
            get_engine_status_17cy=AsyncMock(return_value={"status": "off"}),
            get_electric_status=AsyncMock(return_value={"vehicleInfo": {
                "chargeInfo": {"chargeRemainingAmount": 71, "plugStatus": 40},
            }}),
        )
        vehicle = behavior.make_17cy_vehicle(client)
        vehicle._has_remote_subscription = False
        vehicle._feature_flags = {"evBattery": 0, "evVehicleStatus": 2}
        await vehicle.update()
        client.get_engine_status_17cy.assert_awaited_once()
        self.assertEqual(vehicle.features[VehicleFeatures.ChargeLevel].value, 71)
        vehicle.features[VehicleFeatures.FrontDriverDoor] = ha.ToyotaLockableOpening(
            closed=True, locked=True,
        )
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        for platform in (ha.binary_sensor_platform, ha.sensor_platform):
            await platform.async_setup_entry(
                ha.FakeHass(coordinator), ha.ConfigEntry(),
                lambda added, update: entities.extend(added),
            )
        battery = next(entity for entity in entities if entity.sensor_name == "EV Battery Level")
        self.assertEqual(battery.state, 71)
        self.assertTrue(battery.available)
        self.assertTrue(any(entity.sensor_name == "Front Driver Door" for entity in entities))
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.ChargeStop))

    def test_subscription_changes_preserve_last_known_vehicle_observations(self):
        previous = behavior.make_24mm_vehicle()
        previous.features[VehicleFeatures.ChargeLevel] = ha.ToyotaNumeric(62, "%")
        vehicle = behavior.make_24mm_vehicle()
        vehicle._has_remote_subscription = False
        self.assertTrue(vehicle.inherit_state(previous))
        self.assertEqual(vehicle.features[VehicleFeatures.ChargeLevel].value, 62)
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.EngineStart))

    async def test_expired_login_during_optional_vehicle_reads_reaches_reauthentication(self):
        for factory in (behavior.make_17cy_vehicle, behavior.make_24mm_vehicle):
            client = types.SimpleNamespace(get_telemetry=AsyncMock(side_effect=LoginError()))
            vehicle = factory(client)
            vehicle._has_remote_subscription = False
            with self.subTest(factory=factory.__name__), self.assertRaises(LoginError):
                await vehicle.update()

    async def test_services_enforce_live_feature_and_charging_state(self):
        client = types.SimpleNamespace(remote_request_24mm=AsyncMock())
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle._feature_flags = {"remoteCommands": 1, "evVehicleStatus": 1, "vehicleState": 2}
        vehicle.features[VehicleFeatures.ChargingState] = ha.ToyotaNumeric("charging", "")
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        handlers = {}
        hass.services = types.SimpleNamespace(
            async_register=lambda domain, name, handler: handlers.update({name: handler}),
        )
        hass.async_get_entry = lambda entry_id: entry
        hass.device_registry = types.SimpleNamespace(async_get=lambda device_id: types.SimpleNamespace(
            config_entries={entry.entry_id}, identifiers={(ha.DOMAIN, vehicle.vin)},
        ))
        await ha.integration_runtime.async_setup(hass, {})
        for service in ("charge_start", "refresh"):
            with self.subTest(service=service), self.assertRaises(ha.exceptions.HomeAssistantError):
                await handlers[service](types.SimpleNamespace(service=service, data={"vehicle": "device"}))
        client.remote_request_24mm.assert_not_awaited()
        self.assertEqual(hass.tasks, [])

        call = types.SimpleNamespace(service="charge_stop", data={"vehicle": "device"})
        with patch.object(ha.integration_runtime, "COMMAND_REFRESH_DELAY", 0):
            await handlers[call.service](call)
            await asyncio.gather(*hass.tasks)
        client.remote_request_24mm.assert_awaited_once_with(vehicle.vin, "charge-stop", "CA")
        self.assertEqual(coordinator.refreshes, 1)
        vehicle._feature_flags["remoteCommands"] = 2
        with self.assertRaises(ha.exceptions.HomeAssistantError):
            await handlers[call.service](call)
        self.assertEqual(client.remote_request_24mm.await_count, 1)


class ChargingTests(unittest.IsolatedAsyncioTestCase):
    async def test_charging_buttons_follow_state_without_duplicate_entities(self):
        vehicle = behavior.make_24mm_vehicle()
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await ha.button.async_setup_entry(
            ha.FakeHass(coordinator), ha.ConfigEntry(),
            lambda added, update: entities.extend(added),
        )
        charge_names = {"Charge Now", "Resume Charging", "Stop Charging"}
        for state, expected in (
            ("charge_now", "Charge Now"), ("charging", "Stop Charging"),
            ("resume_charging", "Resume Charging"), ("unavailable", None),
            ("charge_now", "Charge Now"),
        ):
            with self.subTest(state=state):
                vehicle._parse_graphql_electric_status({"charging": {"chargingState": state}})
                coordinator.notify_listeners()
                self.assertEqual(
                    [entity.sensor_name for entity in entities
                     if entity.sensor_name in charge_names and entity.available],
                    [expected] if expected else [],
                )
        self.assertEqual(len(entities), len({entity.unique_id for entity in entities}))

    async def test_charging_state_selects_only_applicable_commands(self):
        for generation in (ApiVehicleGeneration.CY17, ApiVehicleGeneration.CY17PLUS,
                           ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            client = types.SimpleNamespace(electric_command=AsyncMock(return_value={}), remote_request_24mm=AsyncMock())
            vehicle = behavior.make_17cy_vehicle(client) if generation == ApiVehicleGeneration.CY17 else behavior.make_24mm_vehicle(client)
            vehicle._generation = generation
            for state, command, wire_command in (
                ("36", RemoteRequestCommand.ChargeStart, "immediate-charge"),
                ("charge_now", RemoteRequestCommand.ChargeStart, "immediate-charge"),
                ("resume_charging", RemoteRequestCommand.ChargeResume, "resume-charge"),
                ("charging", RemoteRequestCommand.ChargeStop, "charge-stop"),
            ):
                with self.subTest(generation=generation, state=state):
                    if vehicle.uses_appsync:
                        vehicle._parse_graphql_electric_status({"charging": {"chargingState": state}})
                    else:
                        vehicle._parse_electric_status({"vehicleInfo": {"chargeInfo": {"plugStatus": state}}})
                    expected = command == RemoteRequestCommand.ChargeStart or vehicle.uses_appsync
                    self.assertEqual(vehicle.supports_command(command), expected)
                    if expected:
                        await vehicle.send_command(command)
                        if vehicle.uses_appsync:
                            client.remote_request_24mm.assert_awaited_with(vehicle.vin, wire_command, vehicle.region)
                        else:
                            client.electric_command.assert_awaited_with(vehicle.vin, generation.value, wire_command, vehicle.region, vehicle.brand)
                    else:
                        with self.assertRaises(ValueError):
                            await vehicle.send_command(command)
            for state in ("40", "56", "45", "60", "no_controls", "unavailable", "external_power_active", "unexpected"):
                vehicle.features[VehicleFeatures.ChargingState] = ha.ToyotaNumeric(state, "")
                for command in (RemoteRequestCommand.ChargeStart, RemoteRequestCommand.ChargeResume, RemoteRequestCommand.ChargeStop):
                    self.assertFalse(vehicle.supports_command(command), (generation, state, command))

    def test_older_charging_observations_cannot_reenable_commands(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle._parse_graphql_electric_status({"charging": {
            "chargingState": "no_controls", "lastUpdateDateTime": "2026-09-14T19:01:00Z",
        }})
        vehicle._parse_graphql_electric_status({"charging": {
            "chargingState": "charge_now", "lastUpdateDateTime": "2026-09-14T19:00:00Z",
        }})
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.ChargeStart))


class ClimateSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = types.SimpleNamespace(
            get_climate_settings=AsyncMock(return_value=deepcopy(CLIMATE_SETTINGS)),
            update_climate_settings=AsyncMock(),
        )
        self.vehicle = behavior.make_vehicle(self.client)
        self.vehicle._extended_capabilities = {
            **self.vehicle._extended_capabilities, "climateCapable": True,
        }

    async def test_partial_update_preserves_latest_unrelated_preferences(self):
        self.vehicle._climate_settings = {**CLIMATE_SETTINGS, "acOperations": []}
        await self.vehicle.update_climate_settings(temperature=23.5)
        expected = {**CLIMATE_SETTINGS, "temperature": 23.5}
        self.client.update_climate_settings.assert_awaited_once_with("TESTVIN", "21MM", expected, "US", "L")
        self.assertEqual(self.vehicle.climate_settings, expected)
        self.assertEqual(self.client.get_climate_settings.return_value, CLIMATE_SETTINGS)

    async def test_rejected_settings_do_not_replace_last_known_preferences(self):
        self.vehicle._climate_settings = deepcopy(CLIMATE_SETTINGS)
        self.client.update_climate_settings.side_effect = RuntimeError("Toyota rejected settings")
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            await self.vehicle.update_climate_settings(temperature=23)
        self.assertEqual(self.vehicle.climate_settings, CLIMATE_SETTINGS)

    async def test_temperature_range_and_step_are_checked_before_writing(self):
        for temperature in (17, 31, 22.25, float("nan"), float("inf")):
            with self.subTest(temperature=temperature), self.assertRaises(ValueError):
                await self.vehicle.update_climate_settings(temperature=temperature)
        self.client.update_climate_settings.assert_not_awaited()

    async def test_unavailable_climate_does_not_read_or_write(self):
        self.vehicle._feature_flags = {"remoteClimate": 2}
        await self.vehicle.update_climate()
        with self.assertRaises(ValueError):
            await self.vehicle.update_climate_settings(temperature=23)
        self.client.get_climate_settings.assert_not_awaited()
        self.client.update_climate_settings.assert_not_awaited()

    async def test_native_controls_follow_vehicle_units_bounds_and_availability(self):
        await self.vehicle.update_climate()
        coordinator = ha.DataUpdateCoordinator([self.vehicle])
        hass = ha.FakeHass(coordinator)
        entities = []
        for platform in (number, switch):
            await platform.async_setup_entry(hass, ha.ConfigEntry(), lambda added, update: entities.extend(added))
        temperature, enabled = entities
        self.assertEqual((temperature.native_value, temperature.native_min_value,
                          temperature.native_max_value, temperature.native_step,
                          temperature.native_unit_of_measurement), (22, 18, 30, 0.5, "°C"))
        await temperature.async_set_native_value(23)
        self.assertEqual(temperature.native_value, 23)
        await enabled.async_turn_off()
        self.assertFalse(enabled.is_on)
        self.vehicle._feature_flags = {"remoteClimate": 2}
        self.assertFalse(temperature.available)
        self.assertFalse(enabled.available)
        with self.assertRaises(ha.exceptions.ServiceValidationError):
            await enabled.async_turn_on()

    async def test_simultaneous_changes_preserve_both_settings(self):
        settings = deepcopy(CLIMATE_SETTINGS)

        async def read(*args):
            await asyncio.sleep(0)
            return deepcopy(settings)

        async def write(vin, generation, body, region, brand):
            await asyncio.sleep(0)
            settings.update(body)

        self.client.get_climate_settings.side_effect = read
        self.client.update_climate_settings.side_effect = write
        await asyncio.gather(self.vehicle.update_climate_settings(temperature=23),
                             self.vehicle.update_climate_settings(settingsOn=False))
        self.assertEqual(settings["temperature"], 23)
        self.assertFalse(settings["settingsOn"])

    async def test_save_remains_visible_when_poll_replaces_vehicle(self):
        await self.vehicle.update_climate()
        replacement = behavior.make_vehicle(self.client)
        replacement._extended_capabilities = dict(self.vehicle._extended_capabilities)
        replacement.inherit_state(self.vehicle)
        temperature = number.ToyotaClimateTemperature(
            ha.DataUpdateCoordinator([replacement]), "Climate Temperature", replacement.vin,
        )
        await self.vehicle.update_climate_settings(temperature=24)
        self.assertTrue(temperature.available)
        self.assertEqual(temperature.native_value, 24)
