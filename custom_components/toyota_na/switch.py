"""Enable the vehicle's saved climate preferences."""

from homeassistant.components.switch import SwitchEntity

from .base_entity import ToyotaNABaseEntity
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def discover_switches():
        for vehicle in coordinator.data or []:
            entity = ToyotaClimateSettingsSwitch(coordinator, "Use Climate Settings", vehicle.vin)
            if entity.available:
                yield entity

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_switches)


class ToyotaClimateSettingsSwitch(ToyotaNABaseEntity, SwitchEntity):
    _attr_icon = "mdi:car-cog"

    @property
    def available(self):
        vehicle = self.vehicle
        return (
            vehicle is not None
            and vehicle.supports_climate_settings
            and isinstance((vehicle.climate_settings or {}).get("settingsOn"), bool)
        )

    @property
    def is_on(self):
        if self.vehicle:
            return (self.vehicle.climate_settings or {}).get("settingsOn")
        return None

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        if not self.available:
            raise ValueError("Climate settings are unavailable for this vehicle.")
        await self.vehicle.update_climate_settings(settingsOn=enabled)
        self.coordinator.async_set_updated_data(self.coordinator.data)
