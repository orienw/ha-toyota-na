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
    get_user_vehicle_list = client_module.get_user_vehicle_list
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


class ElectricStatusHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_app_headers_reach_every_electric_status_request(self):
        requests = []
        reject_v3 = False
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}

        async def handle(request):
            requests.append((request.path, dict(request.query), {
                key.lower(): value for key, value in request.headers.items()
            }, await request.read()))
            if reject_v3 and request.match_info["version"] == "v3":
                return web.json_response({}, status=403)
            return web.json_response({"payload": status})

        app = web.Application()
        app.router.add_get("/oneapi/{version}/electric/status", handle)
        client = Client()
        client.auth = types.SimpleNamespace(
            get_access_token=AsyncMock(return_value="test-token"),
            get_guid=AsyncMock(return_value="test-guid"),
        )
        client._auth_headers = types.MethodType(client_module._auth_headers, client)
        async with TestServer(app) as server:
            with patch.object(client_module, "API_GATEWAY", str(server.make_url("/oneapi/"))), patch("time.tzname", ("PST", "PDT")):
                for generation, request_no, reject_v3 in (
                    ("17CY", None, False), ("17CYPLUS", None, False),
                    ("21MM", None, False), ("21MM", "refresh/123", False),
                    ("21MM", "refresh/123", True),
                ):
                    with self.subTest(generation=generation, request_no=request_no, reject_v3=reject_v3):
                        requests.clear()
                        self.assertEqual(await client.get_electric_status(
                            "TESTVIN", request_no, "CA", generation,
                        ), status)
                        versions = ["v2"] if generation == "17CY" else ["v3", "v2"] if reject_v3 else ["v3"]
                        self.assertEqual([item[0] for item in requests], [f"/oneapi/{version}/electric/status" for version in versions])
                        for version, (_, query, headers, body) in zip(versions, requests):
                            self.assertEqual(query, {"realtime-status": request_no} if request_no else {})
                            self.assertEqual(body, b"")
                            expected = {
                                "authorization": "Bearer test-token", "x-guid": "test-guid",
                                "x-api-key": client_module.RESOLVER_API_KEY, "x-channel": "ONEAPP",
                                "vin": "TESTVIN", "x-brand": "T", "x-region": "CA", "x-locale": "en-US",
                            }
                            expected.update({
                                "content-type": "application/json", "x-appbrand": "T",
                                "x-appversion": "3.5.0", "user-agent": "okhttp/5.3.2",
                                "x-osname": "Android", "x-osversion": "14",
                                "x-device-timezone": "PST",
                                "x-generation": generation if version == "v3" or generation == "17CY" else None,
                            })
                            self.assertEqual(UUID(headers["x-correlationid"]).version, 4)
                            self.assertEqual({key: headers.get(key) for key in expected}, expected)


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

    async def test_endpoint_headers_override_shared_app_headers(self):
        client = Client()
        client.auth = types.SimpleNamespace(
            get_access_token=AsyncMock(return_value="test-token"),
            get_guid=AsyncMock(return_value="test-guid"),
        )
        client._auth_headers = types.MethodType(client_module._auth_headers, client)
        await client.api_get("v1/global/remote/status", {
            "VIN": "TESTVIN", "X-GENERATION": "21MM", "X-BRAND": "L",
            "x-region": "CA", "X-CORRELATIONID": "existing-request",
        })
        headers = self.session.request.call_args.kwargs["headers"]
        self.assertEqual(headers["X-CORRELATIONID"], "existing-request")
        self.assertEqual(headers["X-BRAND"], "L")
        self.assertEqual(headers["X-APPBRAND"], "T")
        self.assertEqual(headers["X-GENERATION"], "21MM")
        self.assertEqual(headers["x-region"], "CA")
        self.assertEqual(headers["VIN"], "TESTVIN")
        self.assertEqual(headers["X-APPVERSION"], "3.5.0")

    async def test_vehicle_list_failure_code_raises_instead_of_listing_no_vehicles(self):
        for messages, payload, error in (
            ([{"responseCode": "ONE-VL-10002", "description": "Try again later."}], None,
             r"^Try again later\. \[ONE-VL-10002\]$"),
            ([{"description": "no code"}, {"responseCode": "ONE-VL-10002"}], [],
             r"^Toyota could not return the vehicle list\. \[ONE-VL-10002\]$"),
        ):
            self.response.json.return_value = {"status": {"messages": messages}, "payload": payload}
            with self.assertRaisesRegex(RuntimeError, error) as caught:
                await Client().get_user_vehicle_list()
            self.assertEqual("ONE-VL-10002", caught.exception.response_code)
        self.assertTrue(str(self.session.request.call_args.args[1]).endswith("/oneapi/v2/vehicle/guid"))

    async def test_vehicle_list_success_bodies_return_payload_as_before(self):
        vehicles = [{"vin": "TESTVIN"}]
        for body in (
            {"payload": vehicles},
            {"status": {"messages": [{"responseCode": "ONE-VL-10001", "description": "Note"}]}, "payload": vehicles},
            {"status": {"messages": "unexpected"}, "payload": vehicles},
            {"status": {"messages": True}, "payload": vehicles},
            {"status": {"messages": 5}, "payload": vehicles},
            {"status": "unexpected", "payload": vehicles},
        ):
            self.response.json.return_value = body
            self.assertEqual(await Client().get_user_vehicle_list(), vehicles)
        self.response.json.return_value = {"vehicles": vehicles}
        self.assertEqual(await Client().get_user_vehicle_list(), {"vehicles": vehicles})

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

    async def test_tire_pressure_uses_actual_generation_brand_and_region(self):
        payload = {"flTirePressure": {"displayLowTirePressureWarning": True}}
        self.response.json.return_value = {"payload": payload}
        for generation in ("17CY", "17CYPLUS", "21MM", "NG86", "GR86"):
            with self.subTest(generation=generation):
                self.assertEqual(payload, await client_module.get_tire_pressure(Client(), "TESTVIN", generation, "CA", "L"))
                call = self.session.request.call_args
                self.assertEqual(("GET", "https://onecdn.telematicsct.com/oneapi/v1/telemetry/tires/pressure"), call.args)
                self.assertEqual(generation, call.kwargs["headers"]["GENERATION"])
                self.assertEqual("L", call.kwargs["headers"]["X-BRAND"])
                self.assertEqual("CA", call.kwargs["headers"]["x-region"])

    async def test_climate_reservations_use_route_headers_and_saved_device_id(self):
        self.response.json.return_value = {"payload": {"airConditioningReservation": []}}
        self.assertEqual({"airConditioningReservation": []}, await client_module.get_climate_schedules(Client(), "TESTVIN", "21MM", "CA", "L"))
        self.assertEqual(("GET", "https://onecdn.telematicsct.com/v1/remote/route/ac-reservation"), self.session.request.call_args.args)
        for method, identifier, delete in (("POST", None, False), ("PUT", 1, False), ("DELETE", 1, True)):
            for result in ({"returnCode": "ONE-RES-10000", "reservationNo": 1}, {"payload": {"returnCode": "ONE-RES-10000", "reservationNo": 1}}):
                with self.subTest(method=method, result=result):
                    self.response.json.return_value = {"payload": result}
                    self.assertEqual(1, (await client_module.save_climate_schedule(
                        Client(), "TESTVIN", "21MM", {"temperature": "22.5"}, "CA", "L", identifier=identifier, delete=delete,
                    ))["reservationNo"])
                    call = self.session.request.call_args
                    self.assertEqual((method, "https://onecdn.telematicsct.com/v1/remote/route/ac-reservation"), call.args)
                    self.assertEqual("device", call.kwargs["headers"]["device-id"])
                    self.assertEqual("21MM", call.kwargs["headers"]["X-GENERATION"])
                    self.assertEqual("L", call.kwargs["headers"]["X-BRAND"])
                    self.assertEqual("CA", call.kwargs["headers"]["x-region"])
                    if identifier is None:
                        self.assertNotIn("ReservationNo", call.kwargs["headers"])
                    else:
                        self.assertEqual("1", call.kwargs["headers"]["ReservationNo"])
                    if delete:
                        self.assertNotIn("json", call.kwargs)
                    else:
                        self.assertEqual({"temperature": "22.5"}, call.kwargs["json"])

    async def test_climate_schedule_response_keeps_toyotas_message_for_read_back(self):
        for response in (
            {"returnCode": "FAILED", "message": "Request failed"},
            {"message": "Request failed", "payload": {"returnCode": "FAILED"}},
        ):
            with self.subTest(response=response):
                self.response.json.return_value = {"payload": response}
                result = await client_module.save_climate_schedule(Client(), "TESTVIN", "21MM", {})
                self.assertEqual({"returnCode": "FAILED", "message": "Request failed"}, result)

    async def test_extended_commands_keep_generation_brand_and_buzzer_parameters(self):
        for generation in ("17CY", "17CYPLUS", "21MM", "NG86", "GR86"):
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
                self.session.request.reset_mock()
                result = await Client().get_electric_status(
                    "TESTVIN", region="CA", generation=generation,
                )
                self.session.request.assert_called_once()
                args, kwargs = self.session.request.call_args
                self.assertEqual(args, ("GET", f"https://onecdn.telematicsct.com/oneapi/{version}/electric/status"))
                self.assertEqual(kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(kwargs["headers"]["x-region"], "CA")
                self.assertEqual(result, status)

    async def test_electric_status_recovers_from_unusable_v3_response(self):
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}
        for generation in ("17CYPLUS", "21MM"):
            for payload in (
                None, [], "invalid", {}, {"vehicleInfo": None}, {"vehicleInfo": []},
                {"vehicleInfo": {}},
                *({"vehicleInfo": {"chargeInfo": charge}} for charge in (
                    None, [], "invalid", {}, {"evDistanceUnit": "km"},
                    {"chargeRemainingAmount": None, "plugStatus": None, "connectorStatus": None},
                    {"gasolineTravelableDistance": 0}, {"gasolineTravelableDistance": 20},
                )),
            ):
                with self.subTest(generation=generation, payload=payload):
                    self.session.request.reset_mock()
                    self.response.json.side_effect = [{"payload": payload}, {"payload": status}]
                    with self.assertLogs(client_module._LOGGER, level="DEBUG") as logs:
                        result = await Client().get_electric_status("TESTVIN", region="CA", generation=generation)
                    self.assertTrue(any("v3 returned no" in line for line in logs.output), logs.output)
                    current, legacy = self.session.request.call_args_list
                    self.assertEqual(current.args, ("GET", "https://onecdn.telematicsct.com/oneapi/v3/electric/status"))
                    self.assertEqual(current.kwargs["headers"]["X-GENERATION"], generation)
                    self.assertEqual(legacy.args, ("GET", "https://onecdn.telematicsct.com/oneapi/v2/electric/status"))
                    self.assertNotIn("X-GENERATION", legacy.kwargs["headers"])
                    self.assertEqual(legacy.kwargs["headers"]["VIN"], "TESTVIN")
                    self.assertEqual(legacy.kwargs["headers"]["x-region"], "CA")
                    self.assertEqual(result, status)

    async def test_electric_status_recovers_from_v3_errors(self):
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}
        for error in (
            aiohttp.ClientResponseError(MagicMock(), (), status=404, message="Not Found"),
            aiohttp.ClientConnectionError("Disconnected"),
            TimeoutError(),
            json.JSONDecodeError("Not JSON", "<html>", 0),
        ):
            with self.subTest(error=type(error).__name__):
                self.session.request.reset_mock()
                self.response.json.side_effect = [error, {"payload": status}]
                self.assertEqual(await Client().get_electric_status("TESTVIN"), status)
                self.assertEqual(self.session.request.call_count, 2)

    async def test_electric_status_stops_after_both_endpoints_reject_authorization(self):
        self.response.status = 401
        self.response.text.return_value = "{}"
        self.response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=401, message="Unauthorized",
        )
        self.assertIsNone(await Client().get_electric_status("TESTVIN"))
        self.assertEqual(self.session.request.call_count, 2)

    async def test_electric_status_falls_back_when_v3_resource_is_forbidden(self):
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}
        rejected = AsyncMock()
        rejected.__aenter__.return_value = rejected
        rejected.status = 403
        rejected.text.return_value = json.dumps({"status": {"messages": [{
            "responseCode": "RESOURCE-DENIED", "description": "Resource is forbidden",
        }]}})
        rejected.raise_for_status = MagicMock(side_effect=aiohttp.ClientResponseError(
            MagicMock(), (), status=403, message="Forbidden",
        ))
        self.session.request.side_effect = [rejected, self.response]
        self.response.json.return_value = {"payload": status}
        self.assertEqual(await Client().get_electric_status("TESTVIN"), status)
        self.assertEqual(self.session.request.call_count, 2)
        self.assertTrue(self.session.request.call_args.args[1].endswith("/v2/electric/status"))

    async def test_electric_reads_propagate_auth_and_cancellation_at_either_attempt(self):
        for error in (LoginError(), asyncio.CancelledError()):
            for attempt in (0, 1):
                with self.subTest(error=type(error).__name__, attempt=attempt):
                    self.session.request.reset_mock()
                    client = Client()
                    client._auth_headers = AsyncMock(side_effect=[{}] * attempt + [error])
                    self.response.json.return_value = {"payload": {}}
                    with self.assertRaises(type(error)) as caught:
                        await client.get_electric_status("TESTVIN")
                    self.assertIs(caught.exception, error)
                    self.assertEqual(client._auth_headers.await_count, attempt + 1)
                    self.assertEqual(self.session.request.call_count, attempt)

    async def test_electric_status_exhausts_fallback_without_retrying_17cy(self):
        for generation, count in (("17CY", 1), ("17CYPLUS", 2), ("21MM", 2)):
            for response in (
                {"payload": {}}, TimeoutError(),
                aiohttp.ClientResponseError(MagicMock(), (), status=403),
            ):
                with self.subTest(generation=generation, response=type(response).__name__):
                    self.session.request.reset_mock()
                    self.response.json.side_effect = [response] * count
                    self.assertIsNone(await Client().get_electric_status("TESTVIN", generation=generation))
                    self.assertEqual(self.session.request.call_count, count)

    async def test_electric_status_keeps_successful_partial_v3_data(self):
        for info in (
            {"chargeInfo": {"chargeRemainingAmount": 80}},
            {"chargeInfo": {"chargeRemainingAmount": 0, "evDistance": None}},
            {"chargeInfo": {"plugStatus": 0}},
        ):
            with self.subTest(info=info):
                self.session.request.reset_mock()
                status = {"vehicleInfo": info}
                self.response.json.return_value = {"payload": status}
                self.assertEqual(await Client().get_electric_status("TESTVIN"), status)
                self.session.request.assert_called_once()

    async def test_electric_refresh_falls_back_without_repeating_wake(self):
        status = {"vehicleInfo": {"chargeInfo": {"plugStatus": 40}}}
        for generation in ("17CYPLUS", "21MM"):
            with self.subTest(generation=generation):
                self.session.request.reset_mock()
                self.response.json.side_effect = [
                    {"payload": {"appRequestNo": "request/123", "returnCode": "ONE-RES-10000"}},
                    {"payload": {}},
                    {"payload": status},
                ]
                result = await client_module.get_electric_realtime_status(Client(), "TESTVIN", generation, "CA")
                wake, current, legacy = self.session.request.call_args_list
                self.assertEqual(wake.args, ("POST", "https://onecdn.telematicsct.com/oneapi/v2/electric/realtime-status"))
                self.assertEqual(current.args, ("GET", "https://onecdn.telematicsct.com/oneapi/v3/electric/status?realtime-status=request%2F123"))
                self.assertEqual(legacy.args, ("GET", "https://onecdn.telematicsct.com/oneapi/v2/electric/status?realtime-status=request%2F123"))
                self.assertEqual(wake.kwargs["headers"]["X-GENERATION"], generation)
                self.assertNotIn("X-GENERATION", legacy.kwargs["headers"])
                self.assertEqual(legacy.kwargs["headers"]["VIN"], "TESTVIN")
                self.assertEqual(legacy.kwargs["headers"]["x-region"], "CA")
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

    async def test_routed_reads_and_refresh_keep_generation(self):
        for generation in ("NG86", "GR86"):
            for method, path, verb in (
                (client_module.get_vehicle_status_route, "status", "GET"),
                (client_module.get_engine_status_route, "engine-status", "GET"),
                (client_module.send_refresh_request_route, "refresh-status", "POST"),
            ):
                with self.subTest(generation=generation, path=path):
                    await method(Client(), "TESTVIN", generation, "CA", "T")
                    args, kwargs = self.session.request.call_args
                    self.assertEqual((verb, f"https://onecdn.telematicsct.com/v1/remote/route/{path}"), args)
                    self.assertEqual(generation, kwargs["headers"]["X-GENERATION"])
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
                self.assertEqual(getattr(caught.exception, "response_code", None), message.get("responseCode"))

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
