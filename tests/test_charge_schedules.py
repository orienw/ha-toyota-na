"""Schedule writes preserve unrelated data and confirm reported state."""

from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock, patch

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

        self.client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(side_effect=graphql),
            get_electric_status=AsyncMock(side_effect=electric),
            save_charge_schedule=AsyncMock(side_effect=save),
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


class AppSyncScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_schedule_writes_match_both_response_id_fields(self):
        class CallbackSocket(transport._WebSocket):
            def __init__(self):
                super().__init__()
                self.callbacks = [
                    {"vin": "OTHER", "appRequestNo": 42, "status": "COMPLETED"},
                    {"vin": "TESTVIN24", "appRequestNo": 41, "status": "COMPLETED"},
                    {"vin": "TESTVIN24", "appRequestNo": 41, "status": "ERROR"},
                    {"vin": "TESTVIN24", "status": "COMPLETED"},
                    {"vin": "TESTVIN24", "status": "ERROR"},
                    {"vin": "TESTVIN24", "appRequestNo": "42", "status": "COMPLETED"},
                ]

            async def receive(self):
                if self.stage < 2:
                    return await super().receive()
                return transport._Message({
                    "type": "data", "id": self.subscription_id,
                    "payload": {"data": {"onPostRemoteCallback": self.callbacks.pop(0)}},
                })

        for generation in ("24MM", "26BEV"):
            for method in ("POST", "PUT", "DELETE"):
                for identifiers in (
                    {"appRequestNo": 42}, {"correlationId": "42"},
                    {"appRequestNo": 42, "correlationId": "other"},
                ):
                    with self.subTest(generation=generation, method=method, identifiers=identifiers):
                        websocket = CallbackSocket()
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
                        self.assertEqual("42", result["appRequestNo"])
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
    async def test_services_resolve_device_and_map_schedule_fields(self):
        vehicle = behavior.make_24mm_vehicle()
        vehicle.update_charge_schedule = AsyncMock()
        coordinator = ha.DataUpdateCoordinator([vehicle])
        hass = ha.FakeHass(coordinator)
        entry = ha.ConfigEntry()
        handlers = {}
        hass.services = types.SimpleNamespace(async_register=lambda domain, name, handler: handlers.update({name: handler}))
        hass.async_get_entry = lambda entry_id: entry
        device = types.SimpleNamespace(config_entries={entry.entry_id}, identifiers={(ha.DOMAIN, vehicle.vin)})
        registry = types.SimpleNamespace(async_get=lambda device_id: device)
        with patch.object(ha.integration_runtime.dr, "async_get", return_value=registry, create=True):
            await ha.integration_runtime.async_setup(hass, {})
            await handlers["set_charge_schedule"](types.SimpleNamespace(
                service="set_charge_schedule",
                data={"vehicle": "device", "schedule_id": 1, "enabled": False, "start_time": "23:00", "days": ["Monday"]},
            ))
            vehicle.update_charge_schedule.assert_awaited_once_with(1, delete=False, enabled=False, startTime="23:00", daysOfTheWeek=["Monday"])
            await handlers["delete_charge_schedule"](types.SimpleNamespace(
                service="delete_charge_schedule", data={"vehicle": "device", "schedule_id": 1},
            ))
            vehicle.update_charge_schedule.assert_awaited_with(1, delete=True)
        self.assertFalse(hass.tasks)
