"""Vehicle climate temperature preferences."""

import math

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import UnitOfTemperature

from .base_entity import ToyotaNABaseEntity
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def discover_numbers():
        for vehicle in coordinator.data or []:
            entity = ToyotaClimateTemperature(coordinator, "Climate Temperature", vehicle.vin)
            if entity.available:
                yield entity

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_numbers)


class ToyotaClimateTemperature(ToyotaNABaseEntity, NumberEntity):
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_icon = "mdi:thermometer"

    @property
    def settings(self):
        vehicle = self.vehicle
        return (vehicle.climate_settings or {}) if vehicle else {}

    @property
    def available(self):
        settings = self.settings
        values = [settings.get(key) for key in ("temperature", "minTemp", "maxTemp", "tempInterval")]
        return (
            self.vehicle is not None
            and self.vehicle.supports_climate_settings
            and all(type(value) in (int, float) and math.isfinite(value) for value in values)
            and settings["minTemp"] <= settings["maxTemp"]
            and settings["tempInterval"] > 0
            and self.native_unit_of_measurement is not None
        )

    @property
    def native_value(self):
        return self.settings.get("temperature")

    @property
    def native_min_value(self):
        return self.settings.get("minTemp", 0)

    @property
    def native_max_value(self):
        return self.settings.get("maxTemp", 0)

    @property
    def native_step(self):
        return self.settings.get("tempInterval", 1)

    @property
    def native_unit_of_measurement(self):
        unit = str(self.settings.get("temperatureUnit", "")).upper()
        return {
            "C": UnitOfTemperature.CELSIUS,
            "°C": UnitOfTemperature.CELSIUS,
            "F": UnitOfTemperature.FAHRENHEIT,
            "°F": UnitOfTemperature.FAHRENHEIT,
        }.get(unit)

    async def async_set_native_value(self, value):
        if not self.available:
            raise ValueError("Climate temperature is unavailable for this vehicle.")
        await self.vehicle.update_climate_settings(temperature=value)
        self.coordinator.async_set_updated_data(self.coordinator.data)
