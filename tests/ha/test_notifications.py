"""Toyota's notification history reaches each vehicle."""

import json
import types
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from common import LEXUS_21MM_COUPE, FakeVehicle, entity_id
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import async_capture_events
from toyota_na.exceptions import AuthError

from custom_components.toyota_na import event, patch_client
from custom_components.toyota_na.const import NOTIFICATION_EVENT_TYPES
from custom_components.toyota_na.patch_seventeen_cy_plus import SeventeenCYPlusToyotaVehicle
from custom_components.toyota_na.patch_vehicle import get_vehicles

INTEGRATION = Path(__file__).parents[2] / "custom_components/toyota_na"
HISTORY = [
    {
        "vin": "FIRSTVIN",
        "modelDesc": "LC 500",
        "notifications": [
            {"messageId": "1", "category": "RemoteCommand", "vin": "FIRSTVIN"},
            {"messageId": "2", "category": "ServiceWarnings"},
            {"messageId": "3", "category": "RemoteCommand", "vin": "SECONDVIN"},
            "unexpected",
        ],
    },
    {"vin": None, "notifications": [{"messageId": "4", "category": "payment_alerts"}]},
    {"vin": "SECONDVIN", "notifications": None},
    "unexpected",
]


STARTED = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
# Attributes an event entity's state carries besides the event's own data.
EVENT_ENTITY_ATTRIBUTES = {"event_types", "event_type", "friendly_name", "icon"}


def message_ids(vehicle):
    return [item["messageId"] for item in vehicle.notifications]


class NotificationHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(
                return_value=[{**LEXUS_21MM_COUPE, "vin": vin} for vin in ("FIRSTVIN", "SECONDVIN")]
            ),
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


def fired(changes, entity_id):
    """(event type, data) for each event the entity fired, from its state changes.

    Each event gets a new timestamp state; returning from unavailable restores an old one.
    """
    events, timestamps = [], set()
    for change in changes:
        state = change.data["new_state"]
        if (
            change.data["entity_id"] != entity_id
            or state is None
            or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE, *timestamps)
        ):
            continue
        timestamps.add(state.state)
        data = {
            key: value
            for key, value in state.attributes.items()
            if key not in EVENT_ENTITY_ATTRIBUTES
        }
        events.append((state.attributes["event_type"], data))
    return events


@pytest.fixture
async def notifications(hass, setup_vehicles, freezer):
    # Setup marks the start time; notifications Toyota dates before it are history.
    freezer.move_to(STARTED)
    vehicle = FakeVehicle(set())
    vehicle.notifications = [{"messageId": "old", "category": "RemoteCommand"}]
    unread = FakeVehicle(set(), vin="UNREADVIN")
    unread.notifications = None
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)
    account = await setup_vehicles([vehicle, unread])
    assert {
        item.unique_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), account.entry.entry_id)
        if item.domain == "event"
    } == {"TESTVIN.Notifications"}

    async def refresh():
        account.coordinator.async_set_updated_data(account.coordinator.data)
        await hass.async_block_till_done()

    def events(vin="TESTVIN"):
        return fired(changes, entity_id(hass, "event", f"{vin}.Notifications"))

    return vehicle, unread, refresh, events


async def test_new_notifications_fire_oldest_first_after_seeding(notifications):
    vehicle, _, refresh, events = notifications
    vehicle.notifications = [
        {"messageId": "old", "category": "RemoteCommand"},
        {
            "messageId": 7,
            "category": "SERVICEWARNINGS",
            "notificationDate": "2026-10-06T09:00:00Z",
            "title": "Low oil",
            "message": "Check oil",
            "lat": 37.1,
            "lon": -122.1,
        },
        {
            "messageId": "a",
            "category": "OTA_UPDATES_21MM",
            "notificationDate": "2026-10-06T08:00:00Z",
            "displayCategory": "Software",
            "subcategory": None,
            "status": "new",
        },
    ]
    await refresh()
    assert events() == [
        (
            "software_update",
            {
                "message_id": "a",
                "category": "OTA_UPDATES_21MM",
                "display_category": "Software",
                "subcategory": None,
                "title": None,
                "message": None,
                "status": "new",
                "date": "2026-10-06T08:00:00Z",
            },
        ),
        (
            "service_warning",
            {
                "message_id": "7",
                "category": "SERVICEWARNINGS",
                "display_category": None,
                "subcategory": None,
                "title": "Low oil",
                "message": "Check oil",
                "status": None,
                "date": "2026-10-06T09:00:00Z",
            },
        ),
    ]
    await refresh()
    assert len(events()) == 2


async def test_an_empty_read_does_not_refire_old_notifications(notifications):
    vehicle, _, refresh, events = notifications
    history = vehicle.notifications
    vehicle.notifications = []
    await refresh()
    vehicle.notifications = history
    await refresh()
    assert events() == []


async def test_unknown_or_unusable_items(notifications):
    vehicle, _, refresh, events = notifications
    vehicle.notifications = [
        {"messageId": "x", "category": "payment_alerts"},
        {"messageId": "y"},
        {"messageId": "y", "category": "RemoteCommand"},
        {"category": "RemoteCommand"},
        {"messageId": ["unhashable"], "category": "RemoteCommand"},
    ]
    await refresh()
    assert [event_type for event_type, _ in events()] == ["other", "other"]


async def test_notifications_dated_before_startup_never_fire(notifications):
    vehicle, _, refresh, events = notifications
    vehicle.notifications = [
        {
            "messageId": "restart",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T06:59:59Z",
        },
        {
            "messageId": "edge",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T07:00:00Z",
        },
        {
            "messageId": "new",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T07:00:01Z",
        },
    ]
    await refresh()
    assert [data["message_id"] for _, data in events()] == ["new"]
    vehicle.notifications = [
        {"messageId": "restart", "category": "RemoteCommand", "notificationDate": None}
    ]
    await refresh()
    assert [data["message_id"] for _, data in events()] == ["new"]


async def test_history_present_at_startup_stays_history_without_its_date(hass, notifications):
    _, unread, refresh, events = notifications
    unread.notifications = [
        {
            "messageId": "before",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T06:59:59Z",
        },
    ]
    await refresh()
    assert entity_id(hass, "event", "UNREADVIN.Notifications") is not None
    unread.notifications = [{"messageId": "before", "category": "RemoteCommand"}]
    await refresh()
    assert events("UNREADVIN") == []


async def test_a_late_first_read_still_fires_notifications_after_startup(hass, notifications):
    _, unread, refresh, events = notifications
    unread.notifications = [
        {
            "messageId": "before",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T06:00:00Z",
        },
        {
            "messageId": "after",
            "category": "RemoteCommand",
            "notificationDate": "2026-10-06T07:05:00Z",
        },
        {"messageId": "undated", "category": "RemoteCommand"},
    ]
    await refresh()
    assert entity_id(hass, "event", "UNREADVIN.Notifications") is not None
    await refresh()
    assert [data["message_id"] for _, data in events("UNREADVIN")] == ["after"]


async def test_ids_compare_as_text_like_the_app(notifications):
    vehicle, _, refresh, events = notifications
    vehicle.notifications = [{"messageId": 7, "category": "RemoteCommand"}]
    await refresh()
    vehicle.notifications = [
        {"messageId": "7", "category": "RemoteCommand"},
        {"messageId": ""},
    ]
    await refresh()
    assert [data["message_id"] for _, data in events()] == ["7"]


async def test_unavailable_until_history_is_read(hass, notifications):
    vehicle, _, refresh, events = notifications
    notification = entity_id(hass, "event", "TESTVIN.Notifications")
    assert hass.states.get(notification).state != STATE_UNAVAILABLE
    vehicle.notifications = None
    await refresh()
    assert hass.states.get(notification).state == STATE_UNAVAILABLE
    assert events() == []


async def test_every_event_type_is_declared_and_translated(hass, notifications):
    state = hass.states.get(entity_id(hass, "event", "TESTVIN.Notifications"))
    event_types = set(state.attributes["event_types"])
    assert event_types == {*NOTIFICATION_EVENT_TYPES.values(), "other"}
    for path in ("strings.json", "translations/en.json"):
        states = json.loads((INTEGRATION / path).read_text())["entity"]["event"]["notification"][
            "state_attributes"
        ]["event_type"]["state"]
        assert set(states) == event_types
    assert event.notification_event_type(None) == "other"
