"""Saved seat climate and airflow preferences."""

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .base_entity import ToyotaNABaseEntity
from .climate_helpers import AIRFLOWS, SEATS, climate_parameters, seat_modes
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def discover_selects():
        for vehicle in coordinator.data or []:
            for setting, name in (
                ("airflow", "Climate Airflow"),
                ("frontDriver", "Driver Seat Climate"),
                ("frontPassenger", "Passenger Seat Climate"),
                ("rearDriver", "Rear Driver Seat Climate"),
                ("rearPassenger", "Rear Passenger Seat Climate"),
            ):
                entity = ToyotaClimateSelect(setting, coordinator, name, vehicle.vin)
                if entity.available:
                    yield entity

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_selects)


class ToyotaClimateSelect(ToyotaNABaseEntity, SelectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, setting, *args):
        super().__init__(*args)
        self._setting = setting
        self._attr_icon = "mdi:air-filter" if setting == "airflow" else "mdi:car-seat-heater"

    @property
    def settings(self):
        return self.vehicle.climate_settings if self.vehicle else {}

    @property
    def options(self):
        if self._setting == "airflow":
            parameters = climate_parameters(self.settings, "airflow")
            return [label for key, label in AIRFLOWS.items() if key in parameters]
        return seat_modes(self.settings, self._setting)

    @property
    def available(self):
        return (
            self.vehicle is not None and self.vehicle.supports_climate_settings
            and (bool(self.options) if self._setting == "airflow" else len(self.options) > 1)
        )

    @property
    def current_option(self):
        if self._setting == "airflow":
            parameters = climate_parameters(self.settings, "airflow")
            return next(
                (label for key, label in AIRFLOWS.items() if parameters.get(key, {}).get("enabled")),
                None,
            )
        heat = climate_parameters(self.settings, "seatHeat").get(self._setting, {})
        vent = climate_parameters(self.settings, "seatVent").get(SEATS[self._setting], {})
        if heat.get("enabled"):
            return "Heat"
        return "Ventilate" if vent.get("enabled") else "Off"

    async def async_select_option(self, option):
        if not self.available or option not in self.options:
            raise ValueError("This climate preference is unavailable for this vehicle.")
        if self._setting == "airflow":
            key = next(key for key, label in AIRFLOWS.items() if label == option)
            await self.vehicle.update_climate_settings(airflow=key)
        else:
            await self.vehicle.update_climate_settings(seat=(self._setting, option))
        self.coordinator.async_set_updated_data(self.coordinator.data)
