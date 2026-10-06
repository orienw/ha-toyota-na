"""Ensure diagnostic payloads use exact account identifier redaction keys."""

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

import test_button as ha

ha.ha_const.CONF_ACCESS_TOKEN = "access_token"
ha.ha_const.CONF_EMAIL = "email"
ha.ha_const.CONF_PASSWORD = "password"
ha.module("homeassistant.components.diagnostics").async_redact_data = MagicMock()

from custom_components.toyota_na import diagnostics


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_routed_generations_keep_their_context_in_diagnostics(self):
        vehicles = [
            {"vin": f"TEST{generation}", "generation": generation, "brand": "T", "region": "CA"}
            for generation in ("NG86", "GR86")
        ]
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=vehicles),
            get_vehicle_status_route=AsyncMock(return_value={"vehicleStatus": []}),
            get_engine_status_route=AsyncMock(return_value={"status": "stopped"}),
            get_telemetry=AsyncMock(return_value={}),
            get_electric_status=AsyncMock(return_value={}),
        )
        entry = ha.ConfigEntry()
        hass = ha.FakeHass(None)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data):
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual([{"vehicleStatus": []}] * 2, result["vehicle_status"]["data"])
        self.assertEqual([{"status": "stopped"}] * 2, result["engine_status"]["data"])
        for generation in ("NG86", "GR86"):
            client.get_vehicle_status_route.assert_any_await(f"TEST{generation}", generation, "CA", "T")
            client.get_engine_status_route.assert_any_await(f"TEST{generation}", generation, "CA", "T")
            client.get_telemetry.assert_any_await(f"TEST{generation}", "CA", generation)

    async def test_raw_vehicle_and_config_data_use_account_identifier_redaction(self):
        vehicle = {
            "vin": "vin", "generation": "24MM",
            "remoteUserGuid": "remote-user", "subscriberGuid": "subscriber",
            "accountInfoId": "account", "modelName": "RAV4",
        }
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[vehicle]),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            get_telemetry=AsyncMock(return_value={}),
        )
        entry = ha.ConfigEntry()
        entry.data = {"email": "owner@example.com", "tokens": {"guid": "owner"}}
        hass = ha.FakeHass(None)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data) as redact:
            await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        config_data, config_keys = redact.call_args_list[0].args
        payload, payload_keys = redact.call_args_list[1].args
        self.assertEqual(entry.data, config_data)
        self.assertEqual([vehicle], payload["vehicle_list"]["data"])
        for keys in (config_keys, payload_keys):
            self.assertTrue({"remoteUserGuid", "subscriberGuid", "accountInfoId", "guid", "vin", "email", "password"} <= keys)

    async def test_climate_schedules_come_from_the_last_poll(self):
        schedules = {
            "returnCode": "ONE-RES-10000",
            "airConditioningReservation": [{"reservationNo": 1, "date": "09-22-2026", "time": "06:30"}],
        }
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[{"vin": vin, "generation": "24MM"} for vin in ("SCHEDULED", "OTHER")]),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            get_telemetry=AsyncMock(return_value={}),
        )
        coordinator = types.SimpleNamespace(data=[
            types.SimpleNamespace(vin="SCHEDULED", climate_schedules=schedules),
            types.SimpleNamespace(vin="OTHER", climate_schedules={}),
        ])
        entry = ha.ConfigEntry()
        hass = ha.FakeHass(coordinator)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data):
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual([schedules, None], result["climate_schedules"]["data"])

    async def test_vehicle_health_comes_from_the_last_poll(self):
        health = {"read_at": 1000.0, "report": {"vehicleStatus": {}}, "campaigns": []}
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[{"vin": vin, "generation": "24MM"} for vin in ("READ", "OTHER")]),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            get_telemetry=AsyncMock(return_value={}),
        )
        coordinator = types.SimpleNamespace(data=[
            types.SimpleNamespace(vin="READ", health=health),
            types.SimpleNamespace(vin="OTHER", health={"read_at": 1000.0}),
        ])
        entry = ha.ConfigEntry()
        hass = ha.FakeHass(coordinator)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data):
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual(
            [{"report": {"vehicleStatus": {}}, "campaigns": []}, None],
            result["vehicle_health"]["data"],
        )

    async def test_notification_history_keeps_only_structured_fields(self):
        item = {
            "messageId": "1", "category": "RemoteCommand", "displayCategory": "Remote", "subcategory": None,
            "type": "alert", "status": "completed", "notificationDate": "2026-10-06T07:00:00Z", "isRead": False,
            "title": "Doors locked", "message": "Locked at 1 Main St", "iconUrl": "https://example.com",
            "lat": 37.1, "lon": -122.1, "vin": "READ", "readTimestamp": None,
        }
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[{"vin": vin, "generation": "24MM"} for vin in ("READ", "UNREAD")]),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            get_telemetry=AsyncMock(return_value={}),
        )
        coordinator = types.SimpleNamespace(data=[
            types.SimpleNamespace(vin="READ", notifications=[item]),
            types.SimpleNamespace(vin="UNREAD", notifications=None),
        ])
        entry = ha.ConfigEntry()
        hass = ha.FakeHass(coordinator)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data) as redact:
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual(result["notification_history"]["data"], [[{
            "messageId": "1", "category": "RemoteCommand", "displayCategory": "Remote", "subcategory": None,
            "type": "alert", "status": "completed", "notificationDate": "2026-10-06T07:00:00Z", "isRead": False,
        }], None])
        self.assertTrue({"lat", "lon"} <= redact.call_args_list[-1].args[1])

    async def test_report_survives_a_failed_or_unusable_vehicle_list(self):
        def failure(error, code=None):
            if code is not None:
                error.response_code = code
            return AsyncMock(side_effect=error)

        def forbidden(message):
            return aiohttp.ClientResponseError(
                MagicMock(real_url="https://example.invalid/oneapi/v2/vehicle/guid"), (),
                status=403, message=message,
            )

        for lookup, vehicle_list in (
            (failure(RuntimeError("No vehicles for JTHTESTVIN0000001 owner@example.com [ONE-VL-10002]"), "ONE-VL-10002"),
             {"data": None, "error": "RuntimeError [ONE-VL-10002]"}),
            (failure(forbidden("Denied for ABCDEF12-3456-7890-ABCD-EF1234567890 [APIGW-403]"), "APIGW-403"),
             {"data": None, "error": "ClientResponseError 403 [APIGW-403]"}),
            (failure(TimeoutError()), {"data": None, "error": "TimeoutError"}),
            # Code-shaped text in Toyota's message is never exported.
            (failure(forbidden("Account [ONE-ACCOUNT-12345]")), {"data": None, "error": "ClientResponseError 403"}),
            (failure(RuntimeError("Account [ACCOUNT-123456789012] [ONE-VL-10002]")),
             {"data": None, "error": "RuntimeError"}),
            (failure(RuntimeError("Call us"), "PHONE-15555550123"), {"data": None, "error": "RuntimeError"}),
            (AsyncMock(return_value=None), {"data": None}),
            (AsyncMock(return_value={"status": {"messages": {"description": "Unavailable for owner@example.com"}}}),
             {"data": None, "unexpected": "dict"}),
        ):
            client = types.SimpleNamespace(get_user_vehicle_list=lookup)
            entry = ha.ConfigEntry()
            hass = ha.FakeHass(None)
            hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
            with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data):
                result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
            self.assertEqual(vehicle_list, result["vehicle_list"])
            self.assertEqual([], result["vehicle_status"]["data"])

    async def test_vehicle_entries_without_a_vin_are_skipped(self):
        vehicles = ["unexpected", {"generation": "21MM"}, {"vin": "TESTVIN"}]
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=vehicles),
            get_telemetry=AsyncMock(return_value={}),
            get_electric_status=AsyncMock(return_value={}),
        )
        entry = ha.ConfigEntry()
        hass = ha.FakeHass(None)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data):
            result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        self.assertEqual(vehicles, result["vehicle_list"]["data"])
        self.assertEqual([{}], result["telemetry"]["data"])
