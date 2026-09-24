"""Runtime extension only for an eligible remote-start session."""

from datetime import datetime, timedelta, timezone
import json
import types
import unittest
from unittest.mock import AsyncMock, patch

import test_appsync_transport as transport
import test_vehicle_behavior as behavior

from custom_components.toyota_na.patch_base_vehicle import RemoteRequestCommand


def remote_status(**changes):
    engine = {
        "running": True, "lastUpdateBy": "Remote", "status": "Running",
        "stopTime": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
    }
    return {"vehicleState": {"engine": {**engine, **changes}}}


class RemoteRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_extend_refreshes_session_before_sending(self):
        client = types.SimpleNamespace(graphql_get_vehicle_status=AsyncMock(return_value=remote_status()))
        client.remote_request_24mm = AsyncMock(
            side_effect=lambda *args: client.graphql_get_vehicle_status.assert_awaited_once()
        )
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle.apply_graphql_status(remote_status())
        await vehicle.send_command(RemoteRequestCommand.ExtendRuntime)
        client.graphql_get_vehicle_status.assert_awaited_once_with(vehicle.vin, vehicle.backdoor_type, vehicle.region)
        client.remote_request_24mm.assert_awaited_once_with(vehicle.vin, "add-runtime", vehicle.region)

    async def test_local_start_pending_extension_and_expired_session_cannot_extend(self):
        for changes in (
            {"lastUpdateBy": "Vehicle"}, {"status": "Pending"}, {"status": "ExtendedRunning"},
            {"status": None}, {"running": False}, {"stopTime": None},
            {"stopTime": "2020-01-01T00:00:00Z"},
        ):
            with self.subTest(changes=changes):
                vehicle = behavior.make_24mm_vehicle()
                vehicle.apply_graphql_status(remote_status(**changes))
                self.assertFalse(vehicle.supports_command(RemoteRequestCommand.ExtendRuntime))

    async def test_session_that_ends_before_dispatch_is_not_extended(self):
        client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(return_value=remote_status(running=False)),
            remote_request_24mm=AsyncMock(),
        )
        vehicle = behavior.make_24mm_vehicle(client)
        vehicle.apply_graphql_status(remote_status())
        with self.assertRaisesRegex(ValueError, "unavailable for this session"):
            await vehicle.send_command(RemoteRequestCommand.ExtendRuntime)
        client.remote_request_24mm.assert_not_awaited()

    async def test_timed_out_callback_reports_acceptance_without_claiming_completion(self):
        websocket = transport._WebSocket()
        with (
            patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._WebSocketSession(websocket)),
            patch.object(transport.patch_client, "_wait_for_remote_command_result", side_effect=TimeoutError),
        ):
            with self.assertRaisesRegex(RuntimeError, "accepted.*did not report completion"):
                await transport.patch_client.remote_request_24mm(transport._CommandClient(), "TESTVIN24", "add-runtime")

    async def test_partial_read_data_survives_an_error_on_another_field(self):
        payload = {
            "data": {"getVehicleStatus": {"telemetry": {"odo": {"value": 1000, "unit": "mi"}}}},
            "errors": [{"message": "No access to vehicle state", "path": ["getVehicleStatus", "vehicleState"]}],
        }
        with (
            patch.object(transport._Response, "text", AsyncMock(return_value=json.dumps(payload))),
            patch.object(transport.patch_client.aiohttp, "ClientSession", return_value=transport._HttpSession()),
        ):
            result = await transport.patch_client.graphql_get_vehicle_status(transport._HttpClient(), "TESTVIN24")
            self.assertEqual(payload["data"]["getVehicleStatus"], result)
            with self.assertRaisesRegex(RuntimeError, "No access"):
                await transport.patch_client.graphql_request(
                    transport._HttpClient(), "SendRemoteCommand", "mutation {}", {}, raise_errors=True,
                )
