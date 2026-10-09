"""Fire an event for each new notification in Toyota's history."""

from datetime import datetime, timezone

from toyota_na.vehicle.base_vehicle import ToyotaVehicle

from homeassistant.components.event import EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .base_entity import ToyotaNABaseEntity
from .const import DOMAIN, NOTIFICATION_EVENT_TYPES
from .entity_discovery import setup_entity_discovery
from .vehicle_helpers import app_string, parse_api_timestamp

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up notification events."""
    entry_data = hass.data[DOMAIN][config_entry.entry_id]
    coordinator: DataUpdateCoordinator[list[ToyotaVehicle]] = entry_data["coordinator"]

    def discover_events():
        for vehicle in coordinator.data or []:
            if isinstance(vehicle.notifications, list):
                yield ToyotaNotificationEvent(
                    entry_data["started"], coordinator, "Notifications", vehicle.vin
                )

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_events)


def notification_event_type(category) -> str:
    """Return the event type for a notification category."""
    return (
        NOTIFICATION_EVENT_TYPES.get(category.lower(), "other")
        if isinstance(category, str)
        else "other"
    )


class ToyotaNotificationEvent(ToyotaNABaseEntity, EventEntity):
    _attr_event_types = sorted({*NOTIFICATION_EVENT_TYPES.values(), "other"})
    _attr_icon = "mdi:bell-ring-outline"
    _attr_translation_key = "notification"

    def __init__(self, started, *args) -> None:
        super().__init__(*args)
        # Notifications fire only when Toyota dates them after setup, so a late
        # first read or a partial one after a restart can't replay old ones.
        self._started = started
        # IDs only accumulate, so a read that comes back empty can't make old
        # notifications look new on the next one.
        self._seen = set()

    @property
    def _notifications(self):
        """(ID, date, item) for each notification, with IDs as text like the app."""
        vehicle = self.vehicle
        notifications = vehicle.notifications if vehicle is not None else None
        if not isinstance(notifications, list):
            return None
        return [
            (message_id, parse_api_timestamp(item.get("notificationDate")), item)
            for item in notifications
            if (message_id := app_string(item.get("messageId")))
        ]

    @property
    def available(self):
        return self._notifications is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Everything present at startup is history, unless Toyota dates it after setup.
        self._seen.update(
            message_id
            for message_id, date, _ in self._notifications or []
            if date is None or date <= self._started
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        notifications = self._notifications or []
        for message_id, date, item in sorted(
            notifications, key=lambda notification: notification[1] or _OLDEST
        ):
            if message_id in self._seen:
                continue
            # Remember skipped IDs too, so a later read that drops the date
            # can't make an old notification fire.
            self._seen.add(message_id)
            if date is not None and date <= self._started:
                continue
            self._trigger_event(
                notification_event_type(item.get("category")),
                {
                    "message_id": message_id,
                    "category": item.get("category"),
                    "display_category": item.get("displayCategory"),
                    "subcategory": item.get("subcategory"),
                    "title": item.get("title"),
                    "message": item.get("message"),
                    "status": item.get("status"),
                    "date": item.get("notificationDate"),
                },
            )
            self.async_write_ha_state()
        super()._handle_coordinator_update()
