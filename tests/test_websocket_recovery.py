"""Subscription recovery with multiple vehicles on one connection."""

import asyncio
import json
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from test_vehicle_behavior import (
    ROOT,
    ToyotaWebSocketHandler,
    VehicleFeatures,
    make_24mm_vehicle,
    websocket_module,
)


class SubscriptionRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.auth = types.SimpleNamespace(
            get_access_token=AsyncMock(return_value="fresh-token"),
            get_guid=AsyncMock(return_value="guid"),
            get_device_id=lambda: "device",
        )
        self.client = types.SimpleNamespace(
            auth=self.auth,
            graphql_confirm_subscription=AsyncMock(return_value={"vin": "FIRSTVIN"}),
        )
        self.socket = AsyncMock()
        self.socket.closed = False
        self.received = []
        self.handler = ToyotaWebSocketHandler(
            self.client, lambda vin, status: self.received.append((vin, status)),
        )
        self.handler._ws = self.socket
        self.handler._running = True
        self.handler._ready = True
        self.handler._vehicle_contexts = {"FIRSTVIN": {"region": "US"}, "SECONDVIN": {"region": "CA"}}
        self.handler._subscriptions = {"FIRSTVIN": "first", "SECONDVIN": "second"}
        self.handler._task = asyncio.create_task(asyncio.Event().wait())
        self.sleeps = asyncio.Queue()
        sleep_patch = patch.object(websocket_module.asyncio, "sleep", side_effect=self.sleep)
        sleep_patch.start()
        self.addCleanup(sleep_patch.stop)
        self.addAsyncCleanup(self.handler.stop)

    async def sleep(self, delay):
        resume = asyncio.Event()
        self.sleeps.put_nowait((delay, resume))
        await resume.wait()

    async def next_sleep(self, expected):
        delay, resume = await asyncio.wait_for(self.sleeps.get(), timeout=1)
        self.assertEqual(delay, expected)
        return resume

    async def test_rejected_subscription_retries_with_fresh_auth_and_preserves_other_car(self):
        with self.assertLogs(websocket_module.__name__, level="WARNING"):
            await self.handler._handle_message({"type": "error", "id": "first"}, "old-token", "guid")
        retry = self.handler._retry_tasks["FIRSTVIN"]
        resume = await self.next_sleep(5)
        self.assertNotIn("FIRSTVIN", self.handler._subscriptions)
        self.assertEqual(self.handler._subscriptions["SECONDVIN"], "second")
        resume.set()
        await retry

        sent = self.socket.send_json.call_args.args[0]
        self.assertEqual(sent["type"], "start")
        self.assertEqual(sent["payload"]["extensions"]["authorization"]["Authorization"], "Bearer fresh-token")
        self.assertEqual(json.loads(sent["payload"]["data"])["variables"], {"vin": "FIRSTVIN"})
        self.assertNotEqual(sent["id"], "first")
        self.socket.close.assert_not_awaited()
        self.assertEqual(self.handler._subscriptions["SECONDVIN"], "second")

        await self.next_sleep(30)
        timeout = self.handler._retry_tasks["FIRSTVIN"]
        await self.handler._handle_message({"type": "start_ack", "id": sent["id"]}, "old-token", "guid")
        await asyncio.gather(timeout, return_exceptions=True)
        self.assertTrue(timeout.cancelled())
        self.assertNotIn("FIRSTVIN", self.handler._retry_tasks)
        self.client.graphql_confirm_subscription.assert_awaited_once_with("FIRSTVIN", None, "US")

    async def test_repeated_rejections_back_off_without_reconnecting_other_car(self):
        with self.assertLogs(websocket_module.__name__, level="WARNING"):
            for delay in (5, 10, 20, 40, 80, 160, 300, 300):
                await self.handler._handle_message({
                    "type": "error", "id": self.handler._subscriptions["FIRSTVIN"],
                    "payload": {"errors": [{"errorType": "ValidationError"}]},
                }, "token", "guid")
                retry = self.handler._retry_tasks["FIRSTVIN"]
                resume = await self.next_sleep(delay)
                resume.set()
                await retry
                await self.next_sleep(30)
        self.assertEqual(self.handler._subscriptions["SECONDVIN"], "second")
        self.socket.close.assert_not_awaited()

    async def test_unexpected_completion_retries_but_retired_messages_are_ignored(self):
        with self.assertLogs(websocket_module.__name__, level="WARNING"):
            await self.handler._handle_message({"type": "complete", "id": "first"}, "token", "guid")
        retry = self.handler._retry_tasks["FIRSTVIN"]
        await self.next_sleep(5)
        await self.handler._handle_message({"type": "complete", "id": "first"}, "token", "guid")
        await self.handler._handle_message({
            "type": "data", "id": "first",
            "payload": {"data": {"onVehicleStatusUpdated": {"vin": "FIRSTVIN"}}},
        }, "token", "guid")
        self.assertIs(self.handler._retry_tasks["FIRSTVIN"], retry)
        self.assertEqual(self.received, [])

    async def test_missing_subscription_ack_stops_attempt_and_retries(self):
        self.handler._subscriptions.pop("FIRSTVIN")
        await self.handler._subscribe_vin("FIRSTVIN", "token", "guid")
        sub_id = self.handler._subscriptions["FIRSTVIN"]
        timeout = self.handler._retry_tasks["FIRSTVIN"]
        resume = await self.next_sleep(30)
        resume.set()
        await timeout
        self.socket.send_json.assert_awaited_with({"id": sub_id, "type": "stop"})
        self.assertNotIn("FIRSTVIN", self.handler._subscriptions)
        await self.next_sleep(5)

    async def test_removing_car_cancels_retry_and_ignores_late_push(self):
        with self.assertLogs(websocket_module.__name__, level="WARNING"):
            await self.handler._handle_message({"type": "error", "id": "first"}, "token", "guid")
        retry = self.handler._retry_tasks["FIRSTVIN"]
        await self.next_sleep(5)
        self.handler._cached_status["FIRSTVIN"] = {"vin": "FIRSTVIN"}

        await self.handler.update_vehicle_contexts({"SECONDVIN": {"region": "CA"}})
        await asyncio.gather(retry, return_exceptions=True)
        await self.handler._handle_message({
            "type": "data", "id": "first",
            "payload": {"data": {"onVehicleStatusUpdated": {"vin": "FIRSTVIN"}}},
        }, "token", "guid")

        self.assertTrue(retry.cancelled())
        self.assertEqual(self.handler._subscriptions, {"SECONDVIN": "second"})
        self.assertIsNone(self.handler.get_cached_status("FIRSTVIN"))
        self.assertEqual(self.received, [])

    async def test_changed_region_resubscribes_and_added_car_joins_existing_connection(self):
        await self.handler.update_vehicle_contexts({
            "FIRSTVIN": {"region": "CA", "backdoor_type": "trunk"},
            "SECONDVIN": {"region": "CA"},
            "THIRDVIN": {"region": "US"},
        })
        self.socket.send_json.assert_awaited_once_with({"type": "stop", "id": "first"})
        retries = list(self.handler._retry_tasks.values())
        for _ in range(2):
            resume = await self.next_sleep(0)
            resume.set()
        await asyncio.gather(*retries)
        starts = [call.args[0] for call in self.socket.send_json.call_args_list if call.args[0]["type"] == "start"]
        regions = {
            json.loads(start["payload"]["data"])["variables"]["vin"]:
            start["payload"]["extensions"]["authorization"]["x-region"]
            for start in starts
        }
        self.assertEqual(regions, {"FIRSTVIN": "CA", "THIRDVIN": "US"})
        self.assertEqual(self.handler._subscriptions["SECONDVIN"], "second")
        self.socket.close.assert_not_awaited()

    async def test_empty_vehicle_list_disconnects_and_later_vehicle_restarts(self):
        await self.handler.update_vehicle_contexts({})
        self.socket.close.assert_awaited_once()
        self.assertIsNone(self.handler._task)
        self.assertFalse(self.handler.is_connected)
        self.assertTrue(self.handler._running)
        with patch.object(self.handler, "_run_loop", AsyncMock()) as run:
            await self.handler.update_vehicle_contexts({"NEWVIN": {"region": "US"}})
            await self.handler._task
        run.assert_awaited_once()

    async def test_stop_cancels_pending_retry(self):
        with self.assertLogs(websocket_module.__name__, level="WARNING"):
            await self.handler._handle_message({"type": "error", "id": "first"}, "token", "guid")
        retry = self.handler._retry_tasks["FIRSTVIN"]
        await self.next_sleep(5)
        await self.handler.stop()
        self.assertTrue(retry.cancelled())
        self.assertEqual(self.handler._retry_tasks, {})
        self.socket.send_json.assert_not_awaited()

    async def test_disconnected_socket_cancels_subscription_retries(self):
        frames = iter(("connection_ack", "start_ack", "start_ack", "error", "close"))
        pending = []

        async def receive():
            frame = next(frames)
            if frame == "close":
                pending.extend(self.handler._retry_tasks.values())
                return types.SimpleNamespace(type=websocket_module.aiohttp.WSMsgType.CLOSED)
            body = {"type": frame}
            if frame == "start_ack":
                vin = "FIRSTVIN" if self.client.graphql_confirm_subscription.await_count == 0 else "SECONDVIN"
                body["id"] = self.handler._subscriptions[vin]
            elif frame == "error":
                body["id"] = self.handler._subscriptions["FIRSTVIN"]
            return types.SimpleNamespace(type=websocket_module.aiohttp.WSMsgType.TEXT, data=json.dumps(body))

        self.socket.receive.side_effect = receive
        session = MagicMock()
        session.closed = False
        session.ws_connect = AsyncMock(return_value=self.socket)
        session.close = AsyncMock()
        with (
            patch.object(websocket_module.aiohttp, "ClientSession", return_value=session),
            self.assertLogs(websocket_module.__name__, level="WARNING"),
        ):
            await self.handler._connect_and_listen()

        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0].cancelled())
        self.assertEqual(self.handler._retry_tasks, {})
        self.assertEqual(self.handler._subscriptions, {})
        self.assertFalse(self.handler.is_connected)
        self.socket.close.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_connection_scoped_errors_close_and_retire_the_connection(self):
        for error in ({"type": "error"}, {"type": "error", "id": None}, {"type": "connection_error"}):
            with self.subTest(error=error):
                self.socket.reset_mock()
                self.socket.receive.side_effect = [
                    types.SimpleNamespace(type=websocket_module.aiohttp.WSMsgType.TEXT, data=json.dumps(frame))
                    for frame in ({"type": "connection_ack"}, error)
                ]
                session = MagicMock(closed=False)
                session.ws_connect = AsyncMock(return_value=self.socket)
                session.close = AsyncMock()
                with (
                    patch.object(websocket_module.aiohttp, "ClientSession", return_value=session),
                    self.assertLogs(websocket_module.__name__, level="WARNING"),
                    self.assertRaises(ConnectionError),
                ):
                    await self.handler._connect_and_listen()
                self.assertFalse(self.handler._ready)
                self.assertFalse(self.handler.is_connected)
                self.assertEqual({}, self.handler._subscriptions)
                self.assertEqual({}, self.handler._retry_tasks)
                self.socket.close.assert_awaited_once()
                session.close.assert_awaited_once()

    async def test_subscribed_electric_and_tire_data_reaches_vehicle(self):
        vehicle = make_24mm_vehicle()
        self.handler._status_callback = lambda vin, status: vehicle.apply_graphql_status(status)
        status = json.loads((ROOT / "tests/fixtures/vehicle_24mm.json").read_text())
        status["vin"] = "FIRSTVIN"
        await self.handler._subscribe_vin("FIRSTVIN", "token", "guid")
        sent = self.socket.send_json.call_args.args[0]
        query = json.loads(sent["payload"]["data"])["query"]
        for field in ("electric {", "tires {", "tripdetails {", "engine { running lastUpdateDateTime status"):
            self.assertIn(field, query)

        await self.handler._handle_message({
            "type": "data", "id": sent["id"],
            "payload": {"data": {"onVehicleStatusUpdated": status}},
        }, "token", "guid")

        self.assertEqual(vehicle.features[VehicleFeatures.ChargeLevel].value, 100)
        self.assertEqual(vehicle.features[VehicleFeatures.FrontDriverTire].value, 35.1)
        self.assertFalse(vehicle.features[VehicleFeatures.ChargingStatus].closed)
