"""Protocol tests for AppSync status and 24MM remote commands."""

import base64
import importlib.util
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components/toyota_na/patch_client.py"
SPEC = importlib.util.spec_from_file_location("appsync_patch_client", MODULE_PATH)
patch_client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(patch_client)


class _Auth:
    async def get_access_token(self):
        return "token"

    async def get_guid(self):
        return "guid"

    def get_device_id(self):
        return "device"


class _Message:
    type = aiohttp.WSMsgType.TEXT

    def __init__(self, body):
        self.data = json.dumps(body)


class _WebSocket:
    def __init__(self):
        self.sent = []
        self.subscription_id = None
        self.stage = 0

    async def send_json(self, value):
        self.sent.append(value)
        if value.get("type") == "start":
            self.subscription_id = value["id"]

    async def receive(self):
        if self.stage == 0:
            body = {"type": "connection_ack"}
        elif self.stage == 1:
            body = {"type": "start_ack", "id": self.subscription_id}
        else:
            request_no = 41 if self.stage == 2 else 42
            body = {
                "type": "data",
                "id": self.subscription_id,
                "payload": {
                    "data": {
                        "onPostRemoteCallback": {
                            "vin": "TESTVIN24",
                            "appRequestNo": request_no,
                            "status": "COMPLETED",
                            "commandEnded": True,
                        }
                    }
                },
            }
        self.stage += 1
        return _Message(body)


class _SocketContext:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _CallbackWebSocket(_WebSocket):
    def __init__(self, callbacks):
        super().__init__()
        self.callbacks = list(callbacks)

    async def receive(self):
        if self.stage < 2:
            return await super().receive()
        return _Message({
            "type": "data", "id": self.subscription_id,
            "payload": {"data": {"onPostRemoteCallback": self.callbacks.pop(0)}},
        })


class _StatusWebSocket(_WebSocket):
    def __init__(self, statuses):
        super().__init__()
        self.statuses = list(statuses)

    async def receive(self):
        if self.stage < 2:
            return await super().receive()
        return _Message({"type": "data", "id": self.subscription_id, "payload": {"data": {
            "onPostRemoteCallback": {
                "vin": "TESTVIN24", "appRequestNo": 42,
                "status": self.statuses.pop(0), "message": "Vehicle rejected the operation",
            },
        }}})


class _WebSocketSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.url = None
        self.protocols = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def ws_connect(self, url, protocols, heartbeat):
        self.url = url
        self.protocols = protocols
        return _SocketContext(self.websocket)


class _CommandClient:
    def __init__(self):
        self.auth = _Auth()
        self.command_calls = []

    async def graphql_send_remote_command(self, vin, command, region):
        self.command_calls.append((vin, command, region))
        return {
            "payload": {
                "correlationId": "correlation",
                "requestNo": 42,
            }
        }


class _Response:
    def __init__(self, status=200, body=None):
        self.status = status
        self.body = {"data": {"getVehicleStatus": {"vin": "TESTVIN24"}}} if body is None else body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def text(self):
        return json.dumps(self.body)


class _HttpSession:
    def __init__(self):
        self.headers = None
        self.payload = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def post(self, url, headers, data):
        self.headers = headers
        self.payload = json.loads(data)
        return _Response()


class _HttpClient:
    auth = _Auth()
    graphql_request = patch_client.graphql_request


class AppSyncTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_remote_commands_accept_callbacks_without_a_request_number(self):
        for command in ("door-lock", "engine-start", "immediate-charge", "resume-charge", "charge-stop", "power-supply-stop"):
            for fields in ({}, {"appRequestNo": None}):
                with self.subTest(command=command, fields=fields):
                    callback = {"vin": "TESTVIN24", "status": "COMPLETED", **fields}
                    websocket = _CallbackWebSocket([
                        {"vin": "OTHER", "status": "COMPLETED", **fields},
                        {"vin": "TESTVIN24", "appRequestNo": 41, "status": "ERROR"},
                        {"vin": "TESTVIN24", "status": "IN_PROGRESS", **fields},
                        callback,
                    ])
                    client = _CommandClient()
                    with patch.object(patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)):
                        result = await patch_client.remote_request_24mm(client, "TESTVIN24", command)
                    self.assertEqual(callback, result)
                    self.assertEqual([], websocket.callbacks)
                    self.assertEqual([("TESTVIN24", command, "US")], client.command_calls)

    async def test_remote_failures_without_a_request_number_report_the_callback(self):
        for command in ("door-lock", "immediate-charge"):
            for fields in ({}, {"appRequestNo": None}):
                with self.subTest(command=command, fields=fields):
                    websocket = _CallbackWebSocket([{
                        "vin": "TESTVIN24", "status": "ERROR", "message": "Vehicle rejected the operation", **fields,
                    }])
                    with patch.object(patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)):
                        with self.assertRaisesRegex(RuntimeError, "Vehicle rejected the operation"):
                            await patch_client.remote_request_24mm(_CommandClient(), "TESTVIN24", command)
                    self.assertEqual([], websocket.callbacks)

    async def test_callbacks_without_an_id_work_when_no_request_number_was_returned(self):
        callback = {"vin": "TESTVIN24", "status": "COMPLETED"}
        with patch.object(patch_client, "_receive_remote_socket_message", AsyncMock(return_value={
            "type": "data", "id": "subscription",
            "payload": {"data": {"onPostRemoteCallback": callback}},
        })):
            result = await patch_client._wait_for_remote_command_result(
                object(), "TESTVIN24", "subscription",
            )
        self.assertEqual(callback, result)

    async def test_remote_failures_report_the_callback_without_waiting_for_timeout(self):
        for status in ("terminated", "interrupted", "popup_required", "RES1", None, "unexpected"):
            with self.subTest(status=status):
                websocket = _StatusWebSocket([status])
                with patch.object(patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)):
                    with self.assertRaisesRegex(RuntimeError, "Vehicle rejected"):
                        await patch_client.remote_request_24mm(_CommandClient(), "TESTVIN24", "engine-start")
                self.assertEqual([], websocket.statuses)

    async def test_remote_progress_and_unknown_charging_status_keep_waiting(self):
        for command, statuses in (
            ("engine-start", ["in_progress", "completed"]),
            ("immediate-charge", ["interrupted", None, "in_progress", "completed"]),
            ("resume-charge", ["interrupted", "completed"]),
            ("charge-stop", ["interrupted", "completed"]),
            ("power-supply-stop", ["interrupted", "completed"]),
        ):
            with self.subTest(command=command):
                websocket = _StatusWebSocket(statuses)
                with patch.object(patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)):
                    result = await patch_client.remote_request_24mm(_CommandClient(), "TESTVIN24", command)
                self.assertEqual("completed", result["status"])
                self.assertEqual([], websocket.statuses)

    async def test_remote_command_subscribes_before_sending_and_uses_region(self):
        websocket = _WebSocket()
        session = _WebSocketSession(websocket)
        client = _CommandClient()

        with patch.object(
            patch_client.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            result = await patch_client.remote_request_24mm(
                client,
                "TESTVIN24",
                "door-lock",
                "CA",
            )

        self.assertEqual("completed", result["status"].lower())
        self.assertEqual(
            [("TESTVIN24", "door-lock", "CA")],
            client.command_calls,
        )
        self.assertEqual("connection_init", websocket.sent[0]["type"])
        subscription = websocket.sent[1]
        self.assertEqual("start", subscription["type"])
        document = json.loads(subscription["payload"]["data"])
        self.assertIn("onPostRemoteCallback", document["query"])
        authorization = subscription["payload"]["extensions"][
            "authorization"
        ]
        self.assertEqual("CA", authorization["x-region"])
        self.assertEqual("T", authorization["X-BRAND"])
        self.assertEqual("device", authorization["x-deviceid"])
        self.assertEqual(["graphql-ws"], session.protocols)

        query = parse_qs(urlparse(session.url).query)
        connection_headers = json.loads(
            base64.b64decode(query["header"][0])
        )
        self.assertEqual("CA", connection_headers["x-region"])
        self.assertEqual("TESTVIN24", connection_headers["vin"])

    async def test_status_query_sends_vehicle_context_headers(self):
        session = _HttpSession()

        with patch.object(
            patch_client.aiohttp,
            "ClientSession",
            return_value=session,
        ):
            result = await patch_client.graphql_get_vehicle_status(
                _HttpClient(),
                "TESTVIN24",
                "hatch",
                "CA",
            )

        self.assertEqual({"vin": "TESTVIN24"}, result)
        self.assertEqual("CA", session.headers["x-region"])
        self.assertEqual("T", session.headers["X-BRAND"])
        self.assertEqual("T", session.headers["X-APPBRAND"])
        self.assertEqual("hatch", session.headers["backdoorType"])
        self.assertEqual("TESTVIN24", session.headers["vin"])
        self.assertEqual(patch_client.APP_VERSION, session.headers["X-APPVERSION"])
        self.assertEqual(patch_client.USER_AGENT, session.headers["User-Agent"])
        self.assertEqual(4, UUID(session.headers["X-CORRELATIONID"]).version)
        self.assertIn("X-DEVICE-TIMEZONE", session.headers)
        self.assertEqual("GetVehicleStatus", session.payload["operationName"])


class GraphQLRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.auth = SimpleNamespace(
            get_access_token=AsyncMock(return_value="old-token"),
            get_guid=AsyncMock(return_value="guid"),
            get_device_id=lambda: "device",
            check_tokens=AsyncMock(),
        )
        self.client = _HttpClient()
        self.client.auth = self.auth
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        session_patch = patch.object(patch_client.aiohttp, "ClientSession", return_value=self.session)
        session_patch.start()
        self.addCleanup(session_patch.stop)

    async def test_malformed_status_responses_do_not_retry(self):
        for body in (
            [], "invalid", {"data": ["invalid"]},
            {"data": ["invalid"], "errors": [{"errorType": "ValidationError"}]},
            {"data": {"getVehicleStatus": ["invalid"]}},
        ):
            with self.subTest(body=body):
                self.session.post.reset_mock()
                self.session.post.return_value = _Response(200, body)
                self.assertIsNone(await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
                self.session.post.assert_called_once()

    async def test_malformed_command_response_raises_without_replay(self):
        for body in ([], {"data": ["invalid"]}):
            with self.subTest(body=body):
                self.session.post.reset_mock()
                self.session.post.return_value = _Response(200, body)
                with self.assertRaisesRegex(RuntimeError, "invalid response"):
                    await patch_client.graphql_refresh_status(self.client, "TESTVIN24")
                self.session.post.assert_called_once()

    async def test_partial_status_recovers_failed_sections_without_replacing_valid_data(self):
        original = {"vin": "TESTVIN24", "electric": None, "telemetry": {"odo": {"value": 0}}}
        electric = {"battery": {"stateOfChargeDisplay": {"value": 85}}, "charging": {"chargingStatus": "charging"}}
        for path in (
            ["electric", "charging", "actualChargingRate"],
            ["electric", "charging", "chargeSettings", "schedules", 0, "enabled"],
            ["electric", "charging", "chargeSettings", "acCurrentSelections", 0, "enabled"],
            ["electric", "charging", "chargeSettings", "dcPowerSelections"],
            ["electric", "charging", "chargeSettings", "electricSupplyModeLimit", "value"],
            ["electric", "charging", "chargeSettings", "electricSupplyLimitFunction"],
            ["electric", "charging", "chargeSettings", "electricSupplyLimitSelections"],
            ["electric", "charging", "limitSelectionValues"],
            ["vehicleState", "glassHatch", "position", "status"],
            ["vehicleState", "engine", "startTime"],
            ["vehicleState", "engine", "stopTime"],
            ["vehicleState", "engine", "lastUpdateBy"],
        ):
            with self.subTest(path=path):
                state = {**original, path[0]: None}
                restored = electric if path[0] == "electric" else {"engine": {"running": True}}
                self.session.post.reset_mock()
                self.session.post.side_effect = [
                    _Response(200, {"data": {"getVehicleStatus": state}, "errors": [{
                        "path": ["getVehicleStatus", *path], "message": "Optional field failed",
                    }]}),
                    _Response(200, {"data": {"getVehicleStatus": {
                        **state, path[0]: restored, "telemetry": {"odo": {"value": 999}},
                    }}}),
                ]
                result = await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24")
                self.assertEqual({**state, path[0]: restored}, result)
                self.assertEqual(2, self.session.post.call_count)

    async def test_optional_errors_do_not_retry_healthy_sections_or_unrelated_paths(self):
        optional_path = ["getVehicleStatus", "electric", "charging", "chargeSettings", "schedules"]
        for electric, path in (
            ({"battery": {"stateOfChargeDisplay": {"value": 0}}, "charging": {"chargeSettings": None}}, optional_path),
            (None, ["getVehicleStatus", "electric", "battery", "stateOfChargeDisplay"]),
            (None, ["getVehicleStatus", "electric", "schedules"]),
            (None, ["anotherOperation", *optional_path[1:]]),
            (None, None), (None, "electric.charging.chargeSettings.schedules"),
        ):
            with self.subTest(electric=electric, path=path):
                state = {"vin": "TESTVIN24", "electric": electric}
                self.session.post.reset_mock()
                self.session.post.return_value = _Response(200, {
                    "data": {"getVehicleStatus": state},
                    "errors": [{"path": path, "message": "Field failed"}],
                })
                self.assertEqual(state, await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
                self.session.post.assert_called_once()

    async def test_failed_partial_recovery_preserves_first_response(self):
        state = {"vin": "TESTVIN24", "electric": None, "telemetry": {"odo": {"value": 1234}}}
        original = _Response(200, {"data": {"getVehicleStatus": state}, "errors": [{
            "path": ["getVehicleStatus", "electric", "charging", "actualChargingRate"],
            "message": "Optional field failed",
        }]})
        malformed = _Response()
        malformed.text = AsyncMock(return_value="not json")
        for failure, attempts in (
            (_Response(503, {}), 4), (_Response(403, {}), 2),
            (_Response(200, []), 2), (_Response(200, {"data": ["invalid"]}), 2),
            (_Response(200, {"data": {"getVehicleStatus": None}}), 2),
            (original, 2), (malformed, 2),
            (aiohttp.ClientConnectionError("Disconnected"), 2), (TimeoutError(), 2),
        ):
            with self.subTest(failure=failure):
                self.session.post.reset_mock()
                self.session.post.side_effect = [original, *([failure] * 3)]
                with patch.object(patch_client.asyncio, "sleep", AsyncMock()):
                    self.assertEqual(state, await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
                self.assertEqual(attempts, self.session.post.call_count)
        self.auth.check_tokens.assert_not_awaited()

    async def test_partial_recovery_does_not_hide_authentication_failure_or_cancellation(self):
        original = _Response(200, {"data": {"getVehicleStatus": {"vin": "TESTVIN24", "electric": None}}, "errors": [{
            "path": ["getVehicleStatus", "electric", "charging", "actualChargingRate"],
        }]})
        for body in ({}, []):
            with self.subTest(body=body):
                self.session.post.reset_mock()
                self.auth.check_tokens.reset_mock()
                self.session.post.side_effect = [original, _Response(401, body), _Response(401, body)]
                with self.assertRaises(patch_client.TokenExpired):
                    await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24")
                self.assertEqual(3, self.session.post.call_count)
                self.auth.check_tokens.assert_awaited_once()
        self.session.post.side_effect = [original, patch_client.asyncio.CancelledError()]
        with self.assertRaises(patch_client.asyncio.CancelledError):
            await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24")

    async def test_optional_error_can_recover_a_null_vehicle_result(self):
        self.session.post.side_effect = [
            _Response(200, {"data": {"getVehicleStatus": None}, "errors": [{
                "path": ["getVehicleStatus", "electric", "charging", "actualChargingRate"],
            }]}),
            _Response(),
        ]
        self.assertEqual({"vin": "TESTVIN24"}, await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
        self.assertEqual(2, self.session.post.call_count)

    async def test_transient_retry_budget_is_shared_with_fallback(self):
        self.session.post.side_effect = [
            _Response(503, {}),
            _Response(200, {"errors": [{"errorType": "ValidationError"}]}),
            _Response(503, {}), _Response(503, {}),
        ]
        with patch.object(patch_client.asyncio, "sleep", AsyncMock()) as sleep:
            self.assertIsNone(await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
        self.assertEqual([1, 2], [call.args[0] for call in sleep.await_args_list])
        self.assertEqual(4, self.session.post.call_count)

    async def test_status_schema_rejection_retries_with_compatible_fields(self):
        state = {"vin": "TESTVIN24", "electric": {
            "battery": {"stateOfChargeDisplay": {"value": 85, "unit": "%"}},
            "charging": {"chargingStatus": "charging"},
        }}
        for status, error, data in (
            (200, {"message": "Validation error of type FieldUndefined: actualChargingRate"}, None),
            (400, {"errorType": "ValidationError"}, {"getVehicleStatus": None}),
            (200, {"extensions": {"code": "GRAPHQL_VALIDATION_FAILED"}}, None),
        ):
            with self.subTest(status=status, error=error):
                self.session.post.reset_mock()
                self.session.post.side_effect = [
                    _Response(status, {"errors": [error], "data": data}),
                    _Response(200, {"data": {"getVehicleStatus": state}}),
                ]
                result = await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24", "hatch", "CA")
                self.assertEqual(state, result)
                requests = [json.loads(call.kwargs["data"]) for call in self.session.post.call_args_list]
                self.assertEqual(2, len(requests))
                self.assertIn("actualChargingRate", requests[0]["query"])
                for field in ("actualChargingRate", "glassHatch", "acCurrentSelections", "schedules"):
                    self.assertNotIn(field, requests[1]["query"])
                for field in ("battery {", "chargingStatus", "stateOfChargeDisplay", "tires {", "doors {"):
                    self.assertIn(field, requests[1]["query"])
                self.assertEqual({"vin": "TESTVIN24"}, requests[1]["variables"])
                self.assertEqual("GetVehicleStatus", requests[1]["operationName"])
                self.assertEqual("CA", self.session.post.call_args.kwargs["headers"]["x-region"])
        self.auth.check_tokens.assert_not_awaited()

    async def test_status_fallback_is_bounded_and_does_not_replay_mutations(self):
        self.session.post.return_value = _Response(200, {"errors": [{"errorType": "ValidationError"}]})
        self.assertIsNone(await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
        self.assertEqual(2, self.session.post.call_count)
        self.session.post.reset_mock()
        with self.assertRaises(RuntimeError):
            await patch_client.graphql_request(
                self.client, "Write", "mutation Write { command }", {},
                raise_errors=True, fallback_query="mutation Write { otherCommand }",
            )
        self.session.post.assert_called_once()

    async def test_status_fallback_retains_token_recovery(self):
        self.auth.get_access_token.side_effect = ["old-token", "old-token", "fresh-token"]
        self.session.post.side_effect = [
            _Response(200, {"errors": [{"errorType": "ValidationError"}]}),
            _Response(401, {}),
            _Response(),
        ]
        self.assertEqual({"vin": "TESTVIN24"}, await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24"))
        self.assertEqual(3, self.session.post.call_count)
        for call in self.session.post.call_args_list[1:]:
            self.assertNotIn("actualChargingRate", json.loads(call.kwargs["data"])["query"])
        self.auth.check_tokens.assert_awaited_once_with(rejected_token="old-token")
        self.assertEqual("Bearer fresh-token", self.session.post.call_args.kwargs["headers"]["Authorization"])

    async def test_status_fallback_preserves_partial_data_and_ignores_permission_errors(self):
        state = {"vin": "TESTVIN24", "electric": {"battery": {"stateOfChargeDisplay": {"value": 0}}}}
        for status, data, errors, expected_calls in (
            (200, {"getVehicleStatus": state}, [{"errorType": "ValidationError"}], 1),
            (200, {"getVehicleStatus": state}, [{"message": "Feature unavailable"}], 1),
            (200, {"getVehicleStatus": {"vin": "TESTVIN24", "electric": None}}, [], 1),
            (403, None, [{"message": "Feature unavailable"}], 1),
            (429, None, [{"errorType": "ValidationError"}], 3),
            (503, None, [{"errorType": "ValidationError"}], 3),
        ):
            with self.subTest(status=status, data=data):
                self.session.post.reset_mock()
                self.session.post.return_value = _Response(status, {"data": data, "errors": errors})
                with patch.object(patch_client.asyncio, "sleep", AsyncMock()):
                    result = await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24")
                self.assertEqual(data["getVehicleStatus"] if data else None, result)
                self.assertEqual(expected_calls, self.session.post.call_count)
                for call in self.session.post.call_args_list:
                    self.assertIn("actualChargingRate", json.loads(call.kwargs["data"])["query"])
        self.auth.check_tokens.assert_not_awaited()

    async def test_read_refreshes_rejected_tokens_once(self):
        for response in (
            _Response(401, {}),
            _Response(403, {"errorType": "APPSYNC-AUTH-403"}),
            _Response(200, {"errors": [{"errorType": "APIGW-403"}]}),
            _Response(200, {"errors": [{"message": "jwt expired"}]}),
        ):
            with self.subTest(status=response.status, body=response.body):
                self.auth.get_access_token.side_effect = ["old-token", "new-token"]
                self.auth.check_tokens.reset_mock()
                self.session.post.reset_mock()
                self.session.post.side_effect = [response, _Response()]
                result = await patch_client.graphql_request(self.client, "Read", "query Read { vin }", {}, read_only=True)
                self.assertEqual({"getVehicleStatus": {"vin": "TESTVIN24"}}, result)
                self.auth.check_tokens.assert_awaited_once_with(rejected_token="old-token")
                self.assertEqual(2, self.session.post.call_count)
                self.assertEqual("Bearer new-token", self.session.post.call_args.kwargs["headers"]["Authorization"])

    async def test_rejected_refreshed_token_requests_reauthentication(self):
        self.session.post.return_value = _Response(401, {})
        with self.assertRaises(patch_client.TokenExpired):
            await patch_client.graphql_request(self.client, "Read", "query Read { vin }", {}, read_only=True)
        self.assertEqual(2, self.session.post.call_count)
        self.auth.check_tokens.assert_awaited_once()

    async def test_permission_errors_do_not_refresh_and_partial_data_survives(self):
        for response, expected in (
            (_Response(403, {"message": "Feature unavailable"}), None),
            (_Response(200, {"errors": [{"message": "Feature unavailable"}], "data": {"vin": "TESTVIN"}}), {"vin": "TESTVIN"}),
        ):
            with self.subTest(status=response.status):
                self.session.post.return_value = response
                self.assertEqual(expected, await patch_client.graphql_request(self.client, "Read", "query Read { vin }", {}, read_only=True))
        self.auth.check_tokens.assert_not_awaited()

    async def test_mutation_auth_failure_refreshes_without_replaying(self):
        self.session.post.return_value = _Response(401, {})
        with self.assertRaisesRegex(RuntimeError, "Credentials were refreshed"):
            await patch_client.graphql_request(self.client, "Write", "mutation Write { command }", {}, raise_errors=True)
        self.session.post.assert_called_once()
        self.auth.check_tokens.assert_awaited_once_with(rejected_token="old-token")

    async def test_transient_read_retries_are_bounded_and_mutations_are_not_replayed(self):
        for status in (429, 503):
            self.session.post.return_value = _Response(status, {})
            self.session.post.reset_mock()
            with patch.object(patch_client.asyncio, "sleep", AsyncMock()) as sleep:
                self.assertIsNone(await patch_client.graphql_request(self.client, "Read", "query Read { vin }", {}, read_only=True))
                self.assertEqual([1, 2], [call.args[0] for call in sleep.await_args_list])
            self.assertEqual(3, self.session.post.call_count)
            self.session.post.reset_mock()
            with self.assertRaises(RuntimeError):
                await patch_client.graphql_request(self.client, "Write", "mutation Write { command }", {}, raise_errors=True)
            self.session.post.assert_called_once()

    async def test_read_retries_do_not_depend_on_document_formatting(self):
        for operation, document in (
            (None, "query{ vin }"),
            (None, "{ vin }"),
            ("Read", "# cached vehicle status\nquery Read { vin }"),
            ("Read", "query\nRead { vin }"),
        ):
            for status in (401, 503):
                with self.subTest(document=document, status=status):
                    self.session.post.reset_mock()
                    self.session.post.side_effect = [_Response(status, {}), _Response()]
                    with patch.object(patch_client.asyncio, "sleep", AsyncMock()):
                        result = await patch_client.graphql_request(
                            self.client, operation, document, {}, read_only=True,
                        )
                    self.assertEqual({"getVehicleStatus": {"vin": "TESTVIN24"}}, result)
                    self.assertEqual(2, self.session.post.call_count)

    async def test_document_text_does_not_enable_retries(self):
        for operation, document in (
            ("Read", "query Read { vin }"),
            ("Write", "query Read { vin }\nmutation Write { command }"),
        ):
            for status in (401, 429, 503):
                with self.subTest(operation=operation, status=status):
                    self.auth.check_tokens.reset_mock()
                    self.session.post.reset_mock()
                    self.session.post.return_value = _Response(status, {})
                    with patch.object(patch_client.asyncio, "sleep", AsyncMock()) as sleep:
                        with self.assertRaises(RuntimeError):
                            await patch_client.graphql_request(
                                self.client, operation, document, {}, raise_errors=True,
                            )
                    self.session.post.assert_called_once()
                    sleep.assert_not_awaited()
                    if status == 401:
                        self.auth.check_tokens.assert_awaited_once_with(rejected_token="old-token")
                    else:
                        self.auth.check_tokens.assert_not_awaited()

    async def test_vehicle_status_read_enables_recovery(self):
        for status in (401, 503):
            with self.subTest(status=status):
                self.session.post.reset_mock()
                self.session.post.side_effect = [_Response(status, {}), _Response()]
                with patch.object(patch_client.asyncio, "sleep", AsyncMock()):
                    result = await patch_client.graphql_get_vehicle_status(self.client, "TESTVIN24")
                self.assertEqual({"vin": "TESTVIN24"}, result)
                self.assertEqual(2, self.session.post.call_count)


if __name__ == "__main__":
    unittest.main()
