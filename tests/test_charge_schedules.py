"""Schedule writes preserve unrelated data and confirm reported state."""

from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_appsync_transport as transport
import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import patch_base_vehicle, sensor, switch
from custom_components.toyota_na.charging_helpers import build_charge_schedule
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration


SCHEDULE = {
    "settingId": 1, "enabled": True, "startTime": "23:00", "endTime": "07:00",
    "daysOfTheWeek": ["Monday", "Wednesday"], "status": "Active", "nextChargeSettingId": 2,
}


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def make_vehicle(self, generation=ApiVehicleGeneration.MM24):
        self.schedules = [deepcopy(SCHEDULE)]

        async def graphql(*args):
            return {"electric": {"charging": {"chargeSettings": {"schedules": deepcopy(self.schedules)}}}}

        async def electric(*args, **kwargs):
            return {"vehicleInfo": {"timerChargeInfo": deepcopy(self.schedules), "maxNoOfChargeSchedules": 3}}

        async def save(vin, generation, body, region, brand, *, delete):
            identifier = body.get("settingId")
            if delete:
                self.schedules = [item for item in self.schedules if item["settingId"] != identifier]
            elif identifier is None:
                self.schedules.append({**deepcopy(body), "settingId": 2})
            else:
                next(item for item in self.schedules if item["settingId"] == identifier).update(body)

        async def disable(*args):
            for item in self.schedules:
                item["enabled"] = False

        self.client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(side_effect=graphql),
            get_electric_status=AsyncMock(side_effect=electric),
            save_charge_schedule=AsyncMock(side_effect=save),
            disable_charge_schedules=AsyncMock(side_effect=disable),
        )
        vehicle = behavior.make_17cy_vehicle(self.client) if generation == ApiVehicleGeneration.CY17 else behavior.make_24mm_vehicle(self.client)
        vehicle._generation = generation
        vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": 1}
        vehicle._store_charge_schedules(deepcopy(self.schedules), None)
        return vehicle

    async def test_switch_preserves_times_days_and_other_schedules(self):
        vehicle = self.make_vehicle()
        self.schedules.append({**deepcopy(SCHEDULE), "settingId": 2, "enabled": False})
        before = deepcopy(self.schedules[1])
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entity = switch.ToyotaChargeScheduleSwitch("1", ha.ConfigEntry(), coordinator, "Charge Schedule 1", vehicle.vin)
        entity.hass = hass
        await entity.async_turn_off()
        self.assertFalse(entity.is_on)
        self.assertEqual(before, self.schedules[1])
        body = self.client.save_charge_schedule.call_args.args[2]
        self.assertEqual({"settingId": 1, "enabled": False, "startTime": "23:00", "endTime": "07:00", "daysOfTheWeek": ["Monday", "Wednesday"]}, body)
        self.assertEqual("23:00", entity.extra_state_attributes["startTime"])

    async def test_bulk_disable_preserves_schedules_on_each_transport(self):
        for generation in (ApiVehicleGeneration.CY17, ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            with self.subTest(generation=generation):
                vehicle = self.make_vehicle(generation)
                self.schedules.append({**deepcopy(SCHEDULE), "settingId": 2, "enabled": False})
                expected = [{**item, "enabled": False} for item in deepcopy(self.schedules)]
                await vehicle.disable_charge_schedules()
                self.assertEqual(expected, vehicle.charge_settings["schedules"])
                self.client.disable_charge_schedules.assert_awaited_once_with(
                    vehicle.vin, vehicle.api_generation, vehicle.region, vehicle.brand,
                )
                self.client.save_charge_schedule.assert_not_awaited()

    async def test_bulk_disable_reads_current_state_and_skips_already_disabled_schedules(self):
        vehicle = self.make_vehicle()
        self.schedules[0]["enabled"] = False
        await vehicle.disable_charge_schedules()
        self.assertFalse(vehicle.charge_settings["schedules"][0]["enabled"])
        self.schedules.clear()
        await vehicle.disable_charge_schedules()
        self.client.disable_charge_schedules.assert_not_awaited()

    async def test_bulk_disable_requires_schedule_access(self):
        vehicle = self.make_vehicle()
        vehicle._feature_flags["multiDayCharging"] = 2
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await vehicle.disable_charge_schedules()
        self.client.disable_charge_schedules.assert_not_awaited()

    async def test_bulk_disable_waits_for_every_schedule_to_be_reported_disabled(self):
        vehicle = self.make_vehicle()
        self.schedules.append({**deepcopy(SCHEDULE), "settingId": 2})

        async def disable_one(*args):
            self.schedules[0]["enabled"] = False

        async def reported_after_delay(delay):
            self.schedules[1]["enabled"] = False

        self.client.disable_charge_schedules.side_effect = disable_one
        with patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=reported_after_delay) as sleep:
            await vehicle.disable_charge_schedules()
        sleep.assert_awaited_once_with(5)
        self.assertTrue(all(item["enabled"] is False for item in vehicle.charge_settings["schedules"]))

    async def test_bulk_disable_does_not_report_unconfirmed_state(self):
        vehicle = self.make_vehicle()
        self.client.disable_charge_schedules.side_effect = None
        with patch.object(patch_base_vehicle, "SCHEDULE_UPDATE_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "accepted.*did not return"):
                await vehicle.disable_charge_schedules()
        self.assertTrue(vehicle.charge_settings["schedules"][0]["enabled"])

    async def test_create_and_delete_confirm_state_on_each_transport(self):
        for generation in (ApiVehicleGeneration.CY17, ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            with self.subTest(generation=generation):
                vehicle = self.make_vehicle(generation)
                await vehicle.update_charge_schedule(startTime="10:00:00", endTime="12:00", daysOfTheWeek=["Sunday"])
                self.assertEqual(2, len(vehicle.charge_settings["schedules"]))
                body = self.client.save_charge_schedule.call_args.args[2]
                self.assertNotIn("settingId", body)
                self.assertEqual("10:00", body["startTime"])
                await vehicle.update_charge_schedule(2, delete=True)
                self.assertEqual([1], [item["settingId"] for item in vehicle.charge_settings["schedules"]])

    async def test_legacy_requires_explicit_multiday_support(self):
        vehicle = self.make_vehicle(ApiVehicleGeneration.MM21)
        vehicle._feature_flags = None
        self.assertFalse(vehicle.supports_charge_schedules)
        vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": 1}
        self.assertTrue(vehicle.supports_charge_schedules)
        vehicle._has_remote_subscription = False
        self.assertFalse(vehicle.supports_charge_schedules)
        coordinator = ha.DataUpdateCoordinator([vehicle])
        entities = []
        await sensor.async_setup_entry(ha.FakeHass(coordinator), ha.ConfigEntry(), lambda added, update: entities.extend(added))
        schedule_sensor = next(entity for entity in entities if entity.sensor_name == "Charge Schedules")
        self.assertEqual(1, schedule_sensor.state)
        self.assertEqual([SCHEDULE], schedule_sensor.extra_state_attributes["schedules"])

    async def test_invalid_or_deleted_schedule_cannot_be_sent(self):
        vehicle = self.make_vehicle()
        for identifier, changes in (
            (1, {"enabled": "false"}), (1.5, {"enabled": False}),
            (1, {"startTime": "25:00"}), (1, {"startTime": "10:00:30"}),
            (1, {"daysOfTheWeek": []}), (1, {"daysOfTheWeek": ["Unknown"]}),
            (None, {"enabled": True}), (99, {"enabled": True}),
        ):
            with self.subTest(identifier=identifier, changes=changes), self.assertRaises(ValueError):
                await vehicle.update_charge_schedule(identifier, **changes)
        self.client.save_charge_schedule.assert_not_awaited()

    async def test_schedule_access_uses_multiday_instead_of_remote_command_flag(self):
        for generation in (ApiVehicleGeneration.CY17, ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26):
            with self.subTest(generation=generation):
                vehicle = self.make_vehicle(generation)
                vehicle._feature_flags = {"remoteCommands": 2, "multiDayCharging": 1}
                self.assertTrue(vehicle.supports_charge_schedules)
                await vehicle.update_charge_schedule(1, enabled=False)
                self.assertFalse(vehicle.charge_settings["schedules"][0]["enabled"])
                self.client.save_charge_schedule.reset_mock()
                for value in (0, 2, None, True):
                    vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": value}
                    self.assertFalse(vehicle.supports_charge_schedules)
                    with self.assertRaisesRegex(ValueError, "unavailable"):
                        await vehicle.update_charge_schedule(1, enabled=True)
                self.client.save_charge_schedule.assert_not_awaited()

    async def test_deleted_schedule_becomes_unavailable(self):
        vehicle = self.make_vehicle()
        entity = switch.ToyotaChargeScheduleSwitch("1", ha.ConfigEntry(), ha.DataUpdateCoordinator([vehicle]), "Charge Schedule 1", vehicle.vin)
        await vehicle.update_charge_schedule(1, delete=True)
        self.assertFalse(entity.available)
        self.assertIsNone(entity.is_on)

    async def test_schedule_switch_reports_validation_and_operation_failures(self):
        vehicle = self.make_vehicle()
        entry = ha.ConfigEntry()
        entity = switch.ToyotaChargeScheduleSwitch("1", entry, ha.DataUpdateCoordinator([vehicle]), "Charge Schedule 1", vehicle.vin)
        entity.hass = ha.FakeHass(entity.coordinator)
        self.client.save_charge_schedule.side_effect = RuntimeError("Toyota rejected the schedule change.")
        with self.assertRaisesRegex(ha.exceptions.HomeAssistantError, "Toyota rejected the schedule change"):
            await entity.async_turn_off()
        self.assertTrue(entity.is_on)
        self.assertEqual({}, entry.data)
        self.schedules.clear()
        with self.assertRaisesRegex(ha.exceptions.ServiceValidationError, "no longer exists"):
            await entity.async_turn_off()
        with self.assertRaisesRegex(ha.exceptions.ServiceValidationError, "unavailable"):
            await entity.async_turn_off()

    async def test_missing_schedule_response_is_an_operational_error(self):
        vehicle = self.make_vehicle()
        self.client.graphql_get_vehicle_status.side_effect = None
        self.client.graphql_get_vehicle_status.return_value = {}
        with self.assertRaisesRegex(RuntimeError, "Toyota did not return current charge schedules"):
            await vehicle.update_charge_schedule(1, enabled=False)
        self.client.save_charge_schedule.assert_not_awaited()

    async def test_unconfirmed_change_does_not_set_state_optimistically(self):
        vehicle = self.make_vehicle()
        self.client.save_charge_schedule.side_effect = None
        with patch.object(patch_base_vehicle, "SCHEDULE_UPDATE_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "accepted.*did not return"):
                await vehicle.update_charge_schedule(1, enabled=False)
        self.assertTrue(vehicle.charge_settings["schedules"][0]["enabled"])

    async def test_lagging_schedule_readback_backs_off_within_ninety_seconds(self):
        vehicle = self.make_vehicle()
        self.client.save_charge_schedule.side_effect = None
        elapsed = 0
        sleeps = []

        async def sleep(delay):
            nonlocal elapsed
            sleeps.append(delay)
            elapsed += delay

        with (
            patch.object(patch_base_vehicle.asyncio, "get_running_loop", return_value=types.SimpleNamespace(time=lambda: elapsed)),
            patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=sleep),
            self.assertRaisesRegex(RuntimeError, "accepted.*did not return"),
        ):
            await vehicle.update_charge_schedule(1, enabled=False)
        self.assertEqual([5, 10, 20, 30, 25], sleeps)
        self.assertEqual(90, elapsed)
        self.assertEqual(6, self.client.graphql_get_vehicle_status.await_count)
        self.client.save_charge_schedule.assert_awaited_once()
        self.assertTrue(vehicle.charge_settings["schedules"][0]["enabled"])

    async def test_readback_stops_as_soon_as_toyota_reports_the_saved_change(self):
        vehicle = self.make_vehicle()
        self.client.save_charge_schedule.side_effect = None

        async def saved_after_delay(delay):
            self.schedules[0]["enabled"] = False

        with patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=saved_after_delay) as sleep:
            await vehicle.update_charge_schedule(1, enabled=False)
        sleep.assert_awaited_once_with(5)
        self.assertEqual(3, self.client.graphql_get_vehicle_status.await_count)
        self.assertFalse(vehicle.charge_settings["schedules"][0]["enabled"])

    async def test_reported_capacity_blocks_create_without_blocking_edits(self):
        vehicle = self.make_vehicle(ApiVehicleGeneration.MM21)
        self.schedules.extend({**deepcopy(SCHEDULE), "settingId": identifier} for identifier in (2, 3))
        with self.assertRaisesRegex(ValueError, "schedule limit"):
            await vehicle.update_charge_schedule(startTime="10:00", endTime="12:00", daysOfTheWeek=["Sunday"])
        self.client.save_charge_schedule.assert_not_awaited()
        await vehicle.update_charge_schedule(1, enabled=False)
        self.assertFalse(vehicle.charge_settings["schedules"][0]["enabled"])

    async def test_failed_write_preserves_reported_schedule(self):
        vehicle = self.make_vehicle()
        self.client.save_charge_schedule.side_effect = RuntimeError("Vehicle is unavailable")
        with self.assertRaisesRegex(RuntimeError, "Vehicle is unavailable"):
            await vehicle.update_charge_schedule(1, enabled=False)
        self.assertEqual([SCHEDULE], vehicle.charge_settings["schedules"])

    def test_older_or_missing_schedules_preserve_newer_readings(self):
        vehicle = self.make_vehicle()
        for timestamp, schedules in (
            ("2026-09-14T15:00:00Z", [SCHEDULE]),
            ("2026-09-14T14:00:00Z", []),
            ("2026-09-14T16:00:00Z", None),
        ):
            vehicle.apply_graphql_status({
                "electric": {"charging": {"chargeSettings": {
                    "lastUpdateDateTime": timestamp, "schedules": schedules,
                }}},
            })
        self.assertEqual([SCHEDULE], vehicle.charge_settings["schedules"])


class ScheduleDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = behavior.make_24mm_vehicle(types.SimpleNamespace())
        self.vehicle._feature_flags = {"multiDayCharging": 1}
        self.vehicle._store_charge_schedules([deepcopy(SCHEDULE)], None)
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle])
        self.hass = ha.FakeHass(self.coordinator)
        self.config_entry = ha.ConfigEntry()
        self.registry = self.hass.entity_registry
        self.added = []

    def register(self, unique_id, *, domain="switch", platform=ha.DOMAIN, entry_id="entry", disabled=False):
        entity_id = f"{domain}.entity_{len(self.registry.entries) + len(self.registry.removed)}"
        self.registry.entries[entity_id] = types.SimpleNamespace(
            entity_id=entity_id, unique_id=unique_id, domain=domain, platform=platform,
            config_entry_id=entry_id, disabled_by="user" if disabled else None,
        )
        return entity_id

    async def setup_switches(self):
        def add_entities(entities, update):
            self.added.extend(entities)
            for entity in entities:
                if not any(entry.unique_id == entity.unique_id for entry in self.registry.entries.values()):
                    self.register(entity.unique_id)

        await switch.async_setup_entry(self.hass, self.config_entry, add_entities)

    async def test_deleted_schedule_is_removed_and_reused_id_is_discovered(self):
        await self.setup_switches()
        entity_id = next(iter(self.registry.entries))
        self.vehicle._store_charge_schedules([], None)
        self.coordinator.notify_listeners()
        self.assertEqual([entity_id], self.registry.removed)
        self.assertEqual({}, self.registry.entries)

        self.vehicle._store_charge_schedules([deepcopy(SCHEDULE)], None)
        self.coordinator.notify_listeners()
        self.coordinator.notify_listeners()
        self.assertEqual(2, len(self.added))
        self.assertEqual(self.added[0].unique_id, self.added[1].unique_id)
        self.assertEqual(1, len(self.registry.entries))

    async def test_setup_removes_disabled_deleted_schedules_within_this_vehicle_and_entry(self):
        unique_id = f"{self.vehicle.vin}.Charge Schedule 2"
        stale = self.register(unique_id, disabled=True)
        retained = [
            self.register(unique_id, entry_id="other-account"),
            self.register(unique_id, platform="other-integration"),
            self.register(unique_id, domain="sensor"),
            self.register("OTHER-VIN.Charge Schedule 2"),
            self.register(f"{self.vehicle.vin}.Use Climate Settings"),
        ]
        await self.setup_switches()
        self.assertEqual([stale], self.registry.removed)
        self.assertTrue(all(entity_id in self.registry.entries for entity_id in retained))

    async def test_missing_or_incomplete_schedule_lists_do_not_remove_entities(self):
        await self.setup_switches()
        for schedules in (None, {}, [None], [{}], [{"settingId": None}]):
            with self.subTest(schedules=schedules):
                self.vehicle.charge_settings["schedules"] = schedules
                self.coordinator.notify_listeners()
                self.assertEqual([], self.registry.removed)

    async def test_failed_refresh_or_missing_vehicle_does_not_remove_entities(self):
        await self.setup_switches()
        self.vehicle._store_charge_schedules([], None)
        self.coordinator.last_update_success = False
        self.coordinator.notify_listeners()
        self.assertEqual([], self.registry.removed)
        self.coordinator.last_update_success = True
        self.coordinator.data = []
        self.coordinator.notify_listeners()
        self.assertEqual([], self.registry.removed)
        self.coordinator.data = [self.vehicle]
        self.coordinator.notify_listeners()
        self.assertEqual(1, len(self.registry.removed))

    async def test_older_or_missing_schedule_payload_and_subscription_loss_preserve_switches(self):
        self.vehicle._store_charge_schedules([deepcopy(SCHEDULE)], "2026-09-21T15:00:00Z")
        await self.setup_switches()
        self.vehicle._store_charge_schedules([], "2026-09-21T14:00:00Z")
        self.vehicle._store_charge_schedules(None, "2026-09-21T16:00:00Z")
        self.vehicle._has_remote_subscription = False
        self.coordinator.notify_listeners()
        self.assertEqual([], self.registry.removed)
        self.assertEqual(1, len(self.added))


class AppSyncScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_bulk_disable_subscribes_before_write_and_accepts_numberless_callback(self):
        for generation in ("24MM", "26BEV"):
            with self.subTest(generation=generation):
                callback = {"vin": "TESTVIN24", "status": "COMPLETED"}
                websocket = transport._CallbackWebSocket([
                    {"vin": "TESTVIN24", "appRequestNo": 41, "status": "ERROR"}, callback,
                ])

                async def request(method, endpoint, headers):
                    self.assertEqual(2, websocket.stage)
                    self.assertEqual("PUT", method)
                    self.assertTrue(endpoint.endswith("/charging/disable-all"))
                    return {"returnCode": "ONE-RES-10000", "correlationId": "42"}

                client = types.SimpleNamespace(auth=transport._Auth(), api_request=AsyncMock(side_effect=request))
                with patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
                    result = await transport.patch_client.disable_charge_schedules(client, "TESTVIN24", generation)
                self.assertEqual(callback, result)
                self.assertEqual([], websocket.callbacks)
                client.api_request.assert_awaited_once()

    async def test_schedule_writes_match_response_ids_when_callbacks_include_them(self):
        for generation in ("24MM", "26BEV"):
            for method in ("POST", "PUT", "DELETE"):
                for identifiers in (
                    {"appRequestNo": 42}, {"correlationId": "42"},
                    {"appRequestNo": 42, "correlationId": "other"},
                ):
                    for fields in ({}, {"appRequestNo": None}, {"appRequestNo": "42"}):
                        with self.subTest(generation=generation, method=method, identifiers=identifiers, fields=fields):
                            callback = {"vin": "TESTVIN24", "status": "COMPLETED", **fields}
                            websocket = transport._CallbackWebSocket([
                                {"vin": "OTHER", "status": "COMPLETED", **fields},
                                {"vin": "TESTVIN24", "appRequestNo": 41, "status": "COMPLETED"},
                                {"vin": "TESTVIN24", "appRequestNo": 41, "status": "ERROR"},
                                callback,
                            ])
                            client = types.SimpleNamespace(
                                auth=transport._Auth(), api_request=AsyncMock(return_value={
                                    "returnCode": "ONE-RES-10000", **identifiers,
                                }),
                            )
                            body = {key: value for key, value in SCHEDULE.items() if method != "POST" or key != "settingId"}
                            with patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
                                result = await transport.patch_client.save_charge_schedule(
                                    client, "TESTVIN24", generation, body, delete=method == "DELETE",
                                )
                            self.assertEqual(callback, result)
                            self.assertEqual([], websocket.callbacks)
                            client.api_request.assert_awaited_once()
                            self.assertEqual(method, client.api_request.call_args.args[0])

    async def test_routed_schedule_subscribes_before_write_and_waits_for_callback(self):
        websocket = transport._WebSocket()
        async def request(method, endpoint, headers, **kwargs):
            self.assertEqual(2, websocket.stage)
            self.assertEqual("POST", method)
            self.assertEqual("https://onecdn.telematicsct.com/v1/remote/route/charging", endpoint)
            self.assertEqual("26BEV", headers["X-GENERATION"])
            self.assertEqual("23:00", kwargs["json"]["startTime"])
            return {"returnCode": "ONE-RES-10000", "appRequestNo": 42}
        client = types.SimpleNamespace(auth=transport._Auth(), api_request=AsyncMock(side_effect=request))
        with patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)):
            result = await transport.patch_client.save_charge_schedule(
                client, "TESTVIN24", "26BEV",
                build_charge_schedule([], startTime="23:00", endTime="07:00", daysOfTheWeek=["Monday"]),
            )
        self.assertEqual("COMPLETED", result["status"])
        self.assertEqual(4, websocket.stage)


class ScheduleServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = behavior.make_24mm_vehicle()
        self.vehicle.update_charge_schedule = AsyncMock()
        self.vehicle.disable_charge_schedules = AsyncMock(return_value=True)
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle])
        self.hass = ha.FakeHass(self.coordinator)
        self.entry = ha.ConfigEntry()
        self.handlers = {}
        self.hass.services = types.SimpleNamespace(async_register=lambda domain, name, handler: self.handlers.update({name: handler}))
        self.hass.async_get_entry = lambda entry_id: self.entry
        device = types.SimpleNamespace(config_entries={self.entry.entry_id}, identifiers={(ha.DOMAIN, self.vehicle.vin)})
        self.hass.device_registry = types.SimpleNamespace(async_get=lambda device_id: device)
        await ha.integration_runtime.async_setup(self.hass, {})

    async def test_services_resolve_device_and_map_schedule_fields(self):
        await self.handlers["set_charge_schedule"](types.SimpleNamespace(
            service="set_charge_schedule",
            data={"vehicle": "device", "schedule_id": 1, "enabled": False, "start_time": "23:00", "days": ["Monday"]},
        ))
        self.vehicle.update_charge_schedule.assert_awaited_once_with(1, delete=False, enabled=False, startTime="23:00", daysOfTheWeek=["Monday"])
        await self.handlers["delete_charge_schedule"](types.SimpleNamespace(
            service="delete_charge_schedule", data={"vehicle": "device", "schedule_id": 1},
        ))
        self.vehicle.update_charge_schedule.assert_awaited_with(1, delete=True)
        self.assertFalse(self.hass.tasks)

    async def test_bulk_disable_service_updates_entities_and_reports_failures(self):
        call = types.SimpleNamespace(service="disable_charge_schedules", data={"vehicle": "device"})
        notify = self.coordinator.async_set_updated_data = Mock()
        await self.handlers[call.service](call)
        self.vehicle.disable_charge_schedules.assert_awaited_once_with()
        notify.assert_called_once_with(self.coordinator.data)
        self.assertFalse(self.hass.tasks)
        self.vehicle.disable_charge_schedules.side_effect = RuntimeError("Vehicle is unavailable")
        with self.assertRaisesRegex(ha.exceptions.HomeAssistantError, "Vehicle is unavailable"):
            await self.handlers[call.service](call)
        notify.assert_called_once()

    async def test_bulk_disable_noop_does_not_record_a_wake(self):
        self.vehicle.disable_charge_schedules.return_value = False
        await self.handlers["disable_charge_schedules"](types.SimpleNamespace(
            service="disable_charge_schedules", data={"vehicle": "device"},
        ))
        self.assertEqual({}, self.entry.data)

    async def test_schedule_services_preserve_failure_messages_with_ha_exception_types(self):
        for service in ("set_charge_schedule", "delete_charge_schedule"):
            for error, expected_type in (
                (ValueError("This charge schedule no longer exists."), ha.exceptions.ServiceValidationError),
                (RuntimeError("Toyota accepted the schedule change but did not return the updated schedule."), ha.exceptions.HomeAssistantError),
            ):
                self.vehicle.update_charge_schedule.side_effect = error
                with self.subTest(service=service, error=type(error)), self.assertRaises(expected_type) as raised:
                    await self.handlers[service](types.SimpleNamespace(
                        service=service, data={"vehicle": "device", "schedule_id": 1},
                    ))
                self.assertEqual(str(error), str(raised.exception))
                self.assertIs(error, raised.exception.__cause__)
        self.assertEqual({}, self.entry.data)
