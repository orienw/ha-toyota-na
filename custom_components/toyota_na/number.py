"""Vehicle climate temperature and fan preferences."""

import math

from homeassistant.components.number import NumberDeviceClass, NumberEntity
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.exceptions import ServiceValidationError

from .base_entity import ToyotaNABaseEntity
from .climate_helpers import climate_bounds
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery
from .service_helpers import translate_service_errors


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def discover_numbers():
        for vehicle in coordinator.data or []:
            for entity in (
                ToyotaClimateTemperature(coordinator, "Climate Temperature", vehicle.vin),
                ToyotaClimateFanSpeed(coordinator, "Climate Fan Speed", vehicle.vin),
            ):
                if entity.available:
                    yield entity

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_numbers)


class ToyotaClimateNumber(ToyotaNABaseEntity, NumberEntity):
    _key: str
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def settings(self):
        vehicle = self.vehicle
        return (vehicle.climate_settings or {}) if vehicle else {}

    @property
    def available(self):
        value = self.native_value
        return (
            self.vehicle is not None
            and self.vehicle.supports_climate_settings
            and type(value) in (int, float) and math.isfinite(value)
            and climate_bounds(self.settings, self._key) is not None
            and (self._key != "temperature" or self.native_unit_of_measurement is not None)
        )

    @property
    def native_value(self):
        return self.settings.get(self._key)

    @property
    def native_min_value(self):
        return (climate_bounds(self.settings, self._key) or (0, 0, 1))[0]

    @property
    def native_max_value(self):
        return (climate_bounds(self.settings, self._key) or (0, 0, 1))[1]

    @property
    def native_step(self):
        return (climate_bounds(self.settings, self._key) or (0, 0, 1))[2]

    async def async_set_native_value(self, value):
        if not self.available:
            raise ServiceValidationError("This climate setting is unavailable for this vehicle.")
        with translate_service_errors():
            await self.vehicle.update_climate_settings(**{self._key: value})
        self.coordinator.async_set_updated_data(self.coordinator.data)


class ToyotaClimateTemperature(ToyotaClimateNumber):
    _key = "temperature"
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_icon = "mdi:thermometer"

    @property
    def native_unit_of_measurement(self):
        unit = str(self.settings.get("temperatureUnit", "")).upper()
        return {
            "C": UnitOfTemperature.CELSIUS,
            "°C": UnitOfTemperature.CELSIUS,
            "F": UnitOfTemperature.FAHRENHEIT,
            "°F": UnitOfTemperature.FAHRENHEIT,
        }.get(unit)


class ToyotaClimateFanSpeed(ToyotaClimateNumber):
    _key = "airFlowVolume"
    _attr_icon = "mdi:fan"
