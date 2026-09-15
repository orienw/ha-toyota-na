"""Enable the vehicle's saved climate preferences."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .base_entity import ToyotaNABaseEntity
from .climate_helpers import climate_parameters
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery

CLIMATE_SWITCHES = (
    ("Front Defroster", "mdi:car-defrost-front", ("defrost", "frontDefrost")),
    ("Rear Defroster", "mdi:car-defrost-rear", ("defrost", "rearDefrost")),
    ("Steering Wheel Heat", "mdi:steering", ("steeringHeaterCat", "steeringWheel")),
    ("Recirculate Air", "mdi:car-air-filter", ("airCirculate", "insideAirCirculate")),
    ("Longer Climate Runtime", "mdi:timer-plus-outline", "extendedRuntime"),
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def discover_switches():
        for vehicle in coordinator.data or []:
            entity = ToyotaClimateSettingsSwitch(coordinator, "Use Climate Settings", vehicle.vin)
            if entity.available:
                yield entity
            for name, icon, setting in CLIMATE_SWITCHES:
                entity = ToyotaClimatePreferenceSwitch(setting, icon, coordinator, name, vehicle.vin)
                if entity.available:
                    yield entity

    setup_entity_discovery(config_entry, coordinator, async_add_entities, discover_switches)


class ToyotaClimateSettingsSwitch(ToyotaNABaseEntity, SwitchEntity):
    _attr_icon = "mdi:car-cog"
    _attr_entity_category = EntityCategory.CONFIG

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
        await self.vehicle.update_climate_settings(**self._changes(enabled))
        self.coordinator.async_set_updated_data(self.coordinator.data)

    def _changes(self, enabled):
        return {"settingsOn": enabled}


class ToyotaClimatePreferenceSwitch(ToyotaClimateSettingsSwitch):
    def __init__(self, setting, icon, *args):
        super().__init__(*args)
        self._setting = setting
        self._attr_icon = icon

    @property
    def setting(self):
        settings = self.vehicle.climate_settings if self.vehicle else {}
        if self._setting == "extendedRuntime":
            runtime = settings.get("extendedRuntime") or {}
            return runtime if runtime.get("available") is True else {}
        category, name = self._setting
        return climate_parameters(settings, category).get(name, {})

    @property
    def available(self):
        return (
            self.vehicle is not None and self.vehicle.supports_climate_settings
            and isinstance(self.setting.get("enabled"), bool)
        )

    @property
    def is_on(self):
        return self.setting.get("enabled")

    def _changes(self, enabled):
        if self._setting == "extendedRuntime":
            return {"extendedRuntime": enabled}
        return {"parameter": (*self._setting, enabled)}
