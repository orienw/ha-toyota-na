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
from .vehicle_helpers import parse_api_timestamp

_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up notification events."""
    coordinator: DataUpdateCoordinator[list[ToyotaVehicle]] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator"]

    def discover_events():
        for vehicle in coordinator.data or []:
            if isinstance(vehicle.notifications, list):
                yield ToyotaNotificationEvent(coordinator, "Notifications", vehicle.vin)

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_events)


def notification_event_type(category) -> str:
    """Return the event type for a notification category."""
    return NOTIFICATION_EVENT_TYPES.get(category.lower(), "other") if isinstance(category, str) else "other"


class ToyotaNotificationEvent(ToyotaNABaseEntity, EventEntity):
    _attr_event_types = sorted({*NOTIFICATION_EVENT_TYPES.values(), "other"})
    _attr_icon = "mdi:bell-ring-outline"
    _attr_translation_key = "notification"

    def __init__(self, *args) -> None:
        super().__init__(*args)
        # IDs only accumulate, so a read that comes back empty can't make old
        # notifications look new on the next one.
        self._seen = set()

    @property
    def _notifications(self):
        vehicle = self.vehicle
        notifications = vehicle.notifications if vehicle is not None else None
        if not isinstance(notifications, list):
            return None
        return [item for item in notifications if isinstance(item.get("messageId"), (str, int))]

    @property
    def available(self):
        return self._notifications is not None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Notifications from before startup are history, not new events.
        self._seen.update(item["messageId"] for item in self._notifications or [])

    @callback
    def _handle_coordinator_update(self) -> None:
        notifications = self._notifications or []
        for item in sorted(notifications, key=lambda item: parse_api_timestamp(item.get("notificationDate")) or _OLDEST):
            if item["messageId"] in self._seen:
                continue
            self._seen.add(item["messageId"])
            self._trigger_event(notification_event_type(item.get("category")), {
                "message_id": item["messageId"],
                "category": item.get("category"),
                "display_category": item.get("displayCategory"),
                "subcategory": item.get("subcategory"),
                "title": item.get("title"),
                "message": item.get("message"),
                "status": item.get("status"),
                "date": item.get("notificationDate"),
            })
            self.async_write_ha_state()
        super()._handle_coordinator_update()
