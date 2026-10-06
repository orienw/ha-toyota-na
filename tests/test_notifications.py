"""Toyota's notification history reaches each vehicle."""

import types
import unittest
from unittest.mock import AsyncMock, patch

import test_button as ha
import test_vehicle_behavior as behavior

import json

from custom_components.toyota_na import event, patch_client
from custom_components.toyota_na.const import NOTIFICATION_EVENT_TYPES
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


class NotificationEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.vehicle = ha.FakeVehicle(set())
        self.vehicle.notifications = [{"messageId": "old", "category": "RemoteCommand"}]
        self.unread = ha.FakeVehicle(set(), vin="UNREADVIN")
        self.unread.notifications = None
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle, self.unread])
        entities = []
        await event.async_setup_entry(
            ha.FakeHass(self.coordinator), ha.ConfigEntry(), lambda added, update: entities.extend(added),
        )
        self.assertEqual([entity.unique_id for entity in entities], ["TESTVIN.Notifications"])
        self.entity = entities[0]
        await self.entity.async_added_to_hass()

    def events(self):
        return getattr(self.entity, "events", [])

    async def test_new_notifications_fire_oldest_first_after_seeding(self):
        self.vehicle.notifications = [
            {"messageId": "old", "category": "RemoteCommand"},
            {"messageId": 7, "category": "SERVICEWARNINGS", "notificationDate": "2026-10-06T09:00:00Z",
             "title": "Low oil", "message": "Check oil", "lat": 37.1, "lon": -122.1},
            {"messageId": "a", "category": "OTA_UPDATES_21MM", "notificationDate": "2026-10-06T08:00:00Z",
             "displayCategory": "Software", "subcategory": None, "status": "new"},
        ]
        self.entity._handle_coordinator_update()
        self.assertEqual(self.events(), [
            ("software_update", {
                "message_id": "a", "category": "OTA_UPDATES_21MM", "display_category": "Software",
                "subcategory": None, "title": None, "message": None, "status": "new",
                "date": "2026-10-06T08:00:00Z",
            }),
            ("service_warning", {
                "message_id": 7, "category": "SERVICEWARNINGS", "display_category": None,
                "subcategory": None, "title": "Low oil", "message": "Check oil", "status": None,
                "date": "2026-10-06T09:00:00Z",
            }),
        ])
        self.entity._handle_coordinator_update()
        self.assertEqual(len(self.events()), 2)

    async def test_an_empty_read_does_not_refire_old_notifications(self):
        notifications = self.vehicle.notifications
        self.vehicle.notifications = []
        self.entity._handle_coordinator_update()
        self.vehicle.notifications = notifications
        self.entity._handle_coordinator_update()
        self.assertEqual(self.events(), [])

    async def test_unknown_or_unusable_items(self):
        self.vehicle.notifications = [
            {"messageId": "x", "category": "payment_alerts"},
            {"messageId": "y"},
            {"messageId": "y", "category": "RemoteCommand"},
            {"category": "RemoteCommand"},
            {"messageId": ["unhashable"], "category": "RemoteCommand"},
        ]
        self.entity._handle_coordinator_update()
        self.assertEqual([event_type for event_type, _ in self.events()], ["other", "other"])

    async def test_unavailable_until_history_is_read(self):
        self.assertTrue(self.entity.available)
        self.vehicle.notifications = None
        self.assertFalse(self.entity.available)
        self.entity._handle_coordinator_update()
        self.assertEqual(self.events(), [])

    def test_every_event_type_is_declared_and_translated(self):
        types = set(event.ToyotaNotificationEvent._attr_event_types)
        self.assertEqual(types, {*NOTIFICATION_EVENT_TYPES.values(), "other"})
        for path in ("strings.json", "translations/en.json"):
            with open(ha.INTEGRATION / path) as file:
                states = json.load(file)["entity"]["event"]["notification"]["state_attributes"]["event_type"]["state"]
            self.assertEqual(set(states), types)
        self.assertEqual(event.notification_event_type(None), "other")
