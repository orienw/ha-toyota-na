"""Toyota's notification history reaches each vehicle."""

import types
import unittest
from unittest.mock import AsyncMock, patch

import test_vehicle_behavior as behavior

from custom_components.toyota_na import patch_client
from custom_components.toyota_na.patch_seventeen_cy_plus import SeventeenCYPlusToyotaVehicle
from custom_components.toyota_na.patch_vehicle import get_vehicles
from toyota_na.exceptions import AuthError

HISTORY = [
    {"vin": "FIRSTVIN", "modelDesc": "LC 500", "notifications": [
        {"messageId": "1", "category": "RemoteCommand", "vin": "FIRSTVIN"},
        {"messageId": "2", "category": "ServiceWarnings"},
        {"messageId": "3", "category": "RemoteCommand", "vin": "SECONDVIN"},
        "unexpected",
    ]},
    {"vin": None, "notifications": [{"messageId": "4", "category": "payment_alerts"}]},
    {"vin": "SECONDVIN", "notifications": None},
    "unexpected",
]


def message_ids(vehicle):
    return [item["messageId"] for item in vehicle.notifications]


class NotificationHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[
                {**behavior.LEXUS_21MM_COUPE, "vin": vin} for vin in ("FIRSTVIN", "SECONDVIN")
            ]),
            get_notification_history=AsyncMock(return_value=HISTORY),
        )

    async def poll(self):
        with patch.object(SeventeenCYPlusToyotaVehicle, "update", AsyncMock()):
            return await get_vehicles(self.client)

    async def test_each_vehicle_gets_its_notifications_from_one_read(self):
        first, second = await self.poll()
        self.assertEqual(message_ids(first), ["1", "2"])
        self.assertEqual(message_ids(second), ["3"])
        self.client.get_notification_history.assert_awaited_once_with()

    async def test_failed_or_unusable_reads_keep_the_last_notifications(self):
        await self.poll()
        for failure in (RuntimeError("[APIGW-403]"), {"status": "error"}, None):
            with self.subTest(failure=failure):
                if isinstance(failure, Exception):
                    self.client.get_notification_history.side_effect = failure
                else:
                    self.client.get_notification_history.side_effect = None
                    self.client.get_notification_history.return_value = failure
                first, second = await self.poll()
                self.assertEqual((message_ids(first), message_ids(second)), (["1", "2"], ["3"]))

    async def test_vehicles_stay_unread_until_history_answers(self):
        self.client.get_notification_history.side_effect = RuntimeError()
        first, second = await self.poll()
        self.assertEqual((first.notifications, second.notifications), (None, None))

    async def test_auth_failures_still_fail_the_poll(self):
        self.client.get_notification_history.side_effect = AuthError()
        with self.assertRaises(AuthError):
            await self.poll()

    async def test_accounts_without_vehicles_skip_the_read(self):
        self.client.get_user_vehicle_list.return_value = []
        self.assertEqual(await self.poll(), [])
        self.client.get_notification_history.assert_not_awaited()

    async def test_history_is_read_with_the_account_guid(self):
        client = types.SimpleNamespace(
            api_get=AsyncMock(return_value=[]),
            auth=types.SimpleNamespace(get_guid=AsyncMock(return_value="account-guid")),
        )
        self.assertEqual(await patch_client.get_notification_history(client), [])
        client.api_get.assert_awaited_once_with("v2/notification/history", {"GUID": "account-guid"})
