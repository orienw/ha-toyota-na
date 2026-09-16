"""REST routing through the integration and upstream client wrappers."""

import asyncio
import importlib.util
import json
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from toyota_na.client import ToyotaOneClient
from toyota_na.exceptions import LoginError


SPEC = importlib.util.spec_from_file_location(
    "rest_patch_client",
    Path(__file__).resolve().parents[1] / "custom_components/toyota_na/patch_client.py",
)
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)


class Client:
    api_get = ToyotaOneClient.api_get
    api_post = ToyotaOneClient.api_post
    api_request = client_module.api_request
    get_electric_status = client_module.get_electric_status
    auth = types.SimpleNamespace(get_device_id=lambda: "device")

    async def _auth_headers(self):
        return {"AUTHORIZATION": "Bearer test-token"}


class RequestTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_stalled_http_command_times_out_without_replaying(self):
        calls = []

        async def stall(request):
            calls.append(request.method)
            await asyncio.sleep(0.1)
            return web.json_response({})

        app = web.Application()
        app.router.add_post("/command", stall)
        async with TestServer(app) as server:
            with patch.object(client_module, "HTTP_TIMEOUT", aiohttp.ClientTimeout(total=0.02)):
                with self.assertRaises(TimeoutError):
                    await client_module.api_request(Client(), "POST", str(server.make_url("/command")), json={})
        self.assertEqual(["POST"], calls)


class RestTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = AsyncMock()
        self.response.status = 200
        self.response.raise_for_status = MagicMock()
        self.response.json.return_value = {"payload": {"status": "started"}}
        self.response.__aenter__.return_value = self.response
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        self.session.request.return_value = self.response
        self.patch = patch.object(client_module.aiohttp, "ClientSession", return_value=self.session)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_legacy_schedule_body_uses_hour_minute_objects(self):
        self.response.json.return_value = {"payload": {"returnCode": "ONE-RES-10000", "appRequestNo": "123"}}
        body = {"settingId": 1, "enabled": False, "startTime": "23:00", "endTime": "07:00", "daysOfTheWeek": ["Monday"]}
        await client_module.save_charge_schedule(Client(), "TESTVIN", "21MM", body, "CA", "L")
        args, kwargs = self.session.request.call_args
        self.assertEqual(("PUT", "https://onecdn.telematicsct.com/oneapi/v1/electric/charging"), args)
        self.assertEqual({"hour": 23, "minute": 0}, kwargs["json"]["startTime"])
        self.assertEqual({"hour": 7, "minute": 0}, kwargs["json"]["endTime"])
        self.assertEqual("L", kwargs["headers"]["X-BRAND"])
        self.assertEqual("21MM", kwargs["headers"]["X-GENERATION"])
        self.assertEqual("23:00", body["startTime"])

    async def test_schedule_delete_uses_id_path_without_body(self):
        self.response.json.return_value = {"payload": {"returnCode": "ONE-RES-10000", "appRequestNo": "123"}}
        await client_module.save_charge_schedule(Client(), "TESTVIN", "21MM", {"settingId": 2}, delete=True)
        args, kwargs = self.session.request.call_args
        self.assertEqual(("DELETE", "https://onecdn.telematicsct.com/oneapi/v1/electric/charging/2"), args)
        self.assertNotIn("json", kwargs)


    async def test_optional_read_helpers_propagate_expired_credentials(self):
        client = Client()
        client._auth_headers = AsyncMock(side_effect=LoginError())
        for getter in (
            client_module.get_telemetry, client_module.get_vehicle_status_17cy,
            client_module.get_vehicle_status_17cyplus, client_module.get_engine_status_17cyplus,
        ):
            with self.subTest(getter=getter.__name__), self.assertRaises(LoginError):
                await getter(client, "TESTVIN")
        self.session.request.assert_not_called()

    async def test_climate_preferences_use_route_with_actual_vehicle_context(self):
        settings = {"temperature": 72, "temperatureUnit": "F", "settingsOn": True}
        self.response.json.return_value = {"payload": settings}
        for generation in ("17CY", "17CYPLUS", "21MM", "24MM", "26BEV"):
            with self.subTest(generation=generation):
                self.assertEqual(await client_module.get_climate_settings(Client(), "TESTVIN", generation, "CA", "L"), settings)
                call = self.session.request.call_args
                self.assertEqual(call.args, ("GET", "https://onecdn.telematicsct.com/v1/remote/route/climate-settings"))
                self.assertEqual(call.kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(call.kwargs["headers"]["X-BRAND"], "L")
                self.assertEqual(call.kwargs["headers"]["x-region"], "CA")
                await client_module.update_climate_settings(Client(), "TESTVIN", generation, settings, "CA", "L")
                call = self.session.request.call_args
                self.assertEqual(call.args, ("PUT", "https://onecdn.telematicsct.com/v1/remote/route/climate-settings"))
                self.assertEqual(call.kwargs["json"], settings)

    async def test_extended_commands_keep_generation_brand_and_buzzer_parameters(self):
        for generation in ("17CY", "17CYPLUS", "21MM"):
            for command in ("sound-horn", "buzzer-warning"):
                with self.subTest(generation=generation, command=command):
                    await client_module.remote_request_route(Client(), "TESTVIN", generation, command, "CA", "L")
                    call = self.session.request.call_args
                    self.assertEqual(call.args, ("POST", "https://onecdn.telematicsct.com/v1/remote/route/command"))
                    self.assertEqual(call.kwargs["headers"]["X-GENERATION"], generation)
                    self.assertEqual(call.kwargs["headers"]["X-BRAND"], "L")
                    self.assertEqual(call.kwargs["headers"]["x-region"], "CA")
                    body = {"command": command, "autoFixPopup": False}
                    if command == "buzzer-warning":
                        body["beepCount"] = 10
                    self.assertEqual(call.kwargs["json"], body)

    async def test_charge_now_uses_electric_command_and_acceptance_response(self):
        completed = {"remoteControlResult": {"status": 0, "result": 0}, "vehicleInfo": {"chargeInfo": {"plugStatus": 40}}}
        self.response.json.side_effect = [
            {"payload": {"appRequestNo": "charge-123", "returnCode": "ONE-RES-10000"}},
            {"payload": {"remoteControlResult": {"status": 1, "result": None}}},
            {"payload": completed},
        ]
        with patch.object(client_module.asyncio, "sleep", AsyncMock()):
            result = await client_module.electric_command(Client(), "TESTVIN", "21MM", "immediate-charge", "CA", "L")
        call, pending, followup = self.session.request.call_args_list
        self.assertEqual(call.args, ("POST", "https://onecdn.telematicsct.com/oneapi/v2/electric/command"))
        self.assertEqual(call.kwargs["json"], {"command": "immediate-charge"})
        self.assertEqual(call.kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(call.kwargs["headers"]["X-BRAND"], "L")
        self.assertEqual(call.kwargs["headers"]["device-id"], "device")
        self.assertEqual(pending.args, ("GET", "https://onecdn.telematicsct.com/oneapi/v3/electric/status?remote-control=charge-123"))
        self.assertEqual(followup.args, pending.args)
        self.assertEqual(followup.kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(result, completed)

    async def test_17cy_charging_completion_uses_v2_and_encoded_request_number(self):
        self.response.json.side_effect = [
            {"payload": {"appRequestNo": "charge/123", "returnCode": "ONE-RES-10000"}},
            {"payload": {"remoteControlResult": {"status": 0, "result": 0}}},
        ]
        await client_module.electric_command(Client(), "TESTVIN", "17CY", "immediate-charge")
        self.assertEqual(self.session.request.call_args.args, (
            "GET", "https://onecdn.telematicsct.com/oneapi/v2/electric/status?remote-control=charge%2F123",
        ))

    async def test_charging_acceptance_without_completion_times_out(self):
        self.response.json.return_value = {"payload": {"appRequestNo": "123", "returnCode": "ONE-RES-10000"}}
        with patch.object(client_module, "ELECTRIC_COMMAND_TIMEOUT", 0):
            with self.assertRaisesRegex(RuntimeError, "did not confirm completion"):
                await client_module.electric_command(Client(), "TESTVIN", "21MM", "immediate-charge")

    async def test_charge_now_rejects_failed_or_missing_acceptance(self):
        for payload in ({}, {"returnCode": "ONE-RES-10000"}, {"returnCode": "REJECTED", "appRequestNo": "123"}):
            with self.subTest(payload=payload), self.assertRaisesRegex(RuntimeError, "did not accept"):
                self.response.json.return_value = {"payload": payload}
                await client_module.electric_command(Client(), "TESTVIN", "17CY", "immediate-charge")

    async def test_electric_status_uses_generation_specific_version_and_headers(self):
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}
        self.response.json.return_value = {"payload": status}
        for generation, version in (("17CY", "v2"), ("17CYPLUS", "v3"), ("21MM", "v3")):
            with self.subTest(generation=generation):
                result = await Client().get_electric_status(
                    "TESTVIN", region="CA", generation=generation,
                )
                args, kwargs = self.session.request.call_args
                self.assertEqual(args, ("GET", f"https://onecdn.telematicsct.com/oneapi/{version}/electric/status"))
                self.assertEqual(kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(kwargs["headers"]["x-region"], "CA")
                self.assertEqual(result, status)

    async def test_electric_refresh_followup_keeps_generation_and_request_number(self):
        status = {"vehicleInfo": {"chargeInfo": {"plugStatus": 40}}}
        for generation, version, query in (
            ("17CY", "v2", "?realtime-status=request%2F123"),
            ("17CYPLUS", "v3", "?realtime-status=request%2F123"),
            ("21MM", "v3", "?realtime-status=request%2F123"),
        ):
            with self.subTest(generation=generation):
                self.session.request.reset_mock()
                self.response.json.side_effect = [
                    {"payload": {"appRequestNo": "request/123", "returnCode": "ONE-RES-10000"}},
                    {"payload": status},
                ]
                result = await client_module.get_electric_realtime_status(
                    Client(), "TESTVIN", generation, "CA",
                )
                refresh, followup = self.session.request.call_args_list
                self.assertEqual(refresh.args, ("POST", "https://onecdn.telematicsct.com/oneapi/v2/electric/realtime-status"))
                self.assertEqual(refresh.kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(refresh.kwargs["headers"]["device-id"], "device")
                UUID(refresh.kwargs["headers"]["X-CORRELATIONID"])
                UUID(refresh.kwargs["headers"]["x-correlation-id"])
                self.assertEqual(followup.args, ("GET", f"https://onecdn.telematicsct.com/oneapi/{version}/electric/status{query}"))
                self.assertEqual(followup.kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(result, status)

    async def test_21mm_command_reaches_cdn_root_with_vehicle_headers(self):
        await client_module.remote_request_21mm(Client(), "TESTVIN", "engine-start", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://onecdn.telematicsct.com/v1/remote/route/command"))
        self.assertEqual(kwargs["json"], {"command": "engine-start", "autoFixPopup": False})
        headers = kwargs["headers"]
        self.assertEqual(headers["VIN"], "TESTVIN")
        self.assertEqual(headers["X-GENERATION"], "21MM")
        self.assertEqual(headers["X-BRAND"], "T")
        self.assertEqual(headers["x-region"], "CA")
        self.assertEqual(headers["Content-Type"], "application/json")
        UUID(headers["X-CORRELATIONID"])

    async def test_ng86_routed_reads_and_refresh_keep_generation(self):
        for method, path, verb in (
            (client_module.get_vehicle_status_route, "status", "GET"),
            (client_module.get_engine_status_route, "engine-status", "GET"),
            (client_module.send_refresh_request_route, "refresh-status", "POST"),
        ):
            with self.subTest(path=path):
                await method(Client(), "TESTNG86", "NG86", "CA", "T")
                args, kwargs = self.session.request.call_args
                self.assertEqual((verb, f"https://onecdn.telematicsct.com/v1/remote/route/{path}"), args)
                self.assertEqual("NG86", kwargs["headers"]["X-GENERATION"])
                self.assertEqual("CA", kwargs["headers"]["x-region"])
                self.assertEqual("T", kwargs["headers"]["X-BRAND"])

    async def test_21mm_engine_status_reaches_cdn_root(self):
        result = await client_module.get_engine_status_21mm(Client(), "TESTVIN", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", "https://onecdn.telematicsct.com/v1/remote/route/engine-status"))
        self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(kwargs["headers"]["x-region"], "CA")
        self.assertEqual(result, {"status": "started"})

    async def test_21mm_vehicle_status_reaches_cdn_root(self):
        status = {"vehicleStatus": [], "latitude": 34.05, "longitude": -118.25}
        for payload in ({"status": status}, status):
            with self.subTest(payload=payload):
                self.response.json.return_value = {"payload": payload}

                result = await client_module.get_vehicle_status_21mm(Client(), "TESTVIN", "CA")

                args, kwargs = self.session.request.call_args
                self.assertEqual(args, ("GET", "https://onecdn.telematicsct.com/v1/remote/route/status"))
                self.assertEqual(kwargs["headers"]["VIN"], "TESTVIN")
                self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
                self.assertEqual(kwargs["headers"]["x-region"], "CA")
                self.assertEqual(result, status)

    async def test_21mm_refresh_uses_route_request_body(self):
        await client_module.send_refresh_request_21mm(Client(), "TESTVIN", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://onecdn.telematicsct.com/v1/remote/route/refresh-status"))
        self.assertEqual(kwargs["json"], {"autoFixPopup": False})
        self.assertEqual(kwargs["headers"]["VIN"], "TESTVIN")
        self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(kwargs["headers"]["x-region"], "CA")

    async def test_rejected_21mm_command_preserves_toyota_error_details(self):
        self.response.status = 400
        for message, expected in (
            (
                {
                    "detailedDescription": "Remote command invocation(Spec-B) failed",
                    "description": "Command failed",
                    "responseCode": "ONE-GLOBAL-RS-40009",
                },
                "Remote command invocation(Spec-B) failed [ONE-GLOBAL-RS-40009]",
            ),
            (
                {"description": "Command failed", "responseCode": "ONE-GLOBAL-RS-40009"},
                "Command failed [ONE-GLOBAL-RS-40009]",
            ),
            ({"description": "Command failed"}, "Command failed"),
            ({"responseCode": "ONE-GLOBAL-RS-40009"}, "Bad Request [ONE-GLOBAL-RS-40009]"),
        ):
            with self.subTest(message=message):
                self.response.text.return_value = json.dumps({"status": {"messages": [message]}})
                error = aiohttp.ClientResponseError(
                    MagicMock(), (), status=400, message="Bad Request",
                )
                self.response.raise_for_status.side_effect = error

                with self.assertRaises(aiohttp.ClientResponseError) as caught:
                    await client_module.remote_request_21mm(Client(), "TESTVIN", "engine-start")

                self.assertIs(caught.exception, error)
                self.assertEqual(caught.exception.message, expected)

    async def test_rejected_21mm_command_preserves_http_error_without_toyota_details(self):
        self.response.status = 400
        for body in (
            "rejected",
            '<html>Bad Request</html>',
            '{"status":',
            'null',
            '[]',
            '{}',
            '{"status": null}',
            '{"status": {"messages": []}}',
            '{"status": {"messages": [null]}}',
            '{"status": {"messages": [{"description": 123, "responseCode": null}]}}',
        ):
            with self.subTest(body=body):
                self.response.text.return_value = body
                error = aiohttp.ClientResponseError(
                    MagicMock(), (), status=400, message="Bad Request",
                )
                self.response.raise_for_status.side_effect = error

                with self.assertRaises(aiohttp.ClientResponseError) as caught:
                    await client_module.remote_request_21mm(Client(), "TESTVIN", "engine-start")

                self.assertIs(caught.exception, error)
                self.assertEqual(caught.exception.message, "Bad Request")
