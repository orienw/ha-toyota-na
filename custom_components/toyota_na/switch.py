"""Enable the vehicle's saved climate preferences."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from .base_entity import ToyotaNABaseEntity, vehicle_entity_unique_id
from .climate_helpers import climate_parameters
from .const import DOMAIN
from .entity_discovery import setup_entity_discovery
from .service_helpers import translate_service_errors
from .wake_policy import record_vehicle_wake

CLIMATE_SWITCHES = (
    ("Front Defroster", "mdi:car-defrost-front", ("defrost", "frontDefrost")),
    ("Rear Defroster", "mdi:car-defrost-rear", ("defrost", "rearDefrost")),
    ("Steering Wheel Heat", "mdi:steering", ("steeringHeaterCat", "steeringWheel")),
    ("Recirculate Air", "mdi:car-air-filter", ("airCirculate", "insideAirCirculate")),
    ("Longer Climate Runtime", "mdi:timer-plus-outline", "extendedRuntime"),
)


async def async_setup_entry(hass, config_entry, async_add_entities):
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]

    def remove_stale_schedules():
        if not coordinator.last_update_success:
            return
        registry = er.async_get(hass)
        entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
        for vehicle in coordinator.data or []:
            schedules = vehicle.charge_settings.get("schedules")
            if not isinstance(schedules, list) or any(
                not isinstance(schedule, dict) or schedule.get("settingId") is None
                for schedule in schedules
            ):
                continue
            prefix = vehicle_entity_unique_id(vehicle.vin, "Charge Schedule ")
            current_ids = {f"{prefix}{schedule['settingId']}" for schedule in schedules}
            for entry in entries:
                if (entry.domain == "switch" and entry.platform == DOMAIN
                        and entry.unique_id.startswith(prefix) and entry.unique_id not in current_ids):
                    registry.async_remove(entry.entity_id)
                    yield entry.unique_id

    def discover_switches():
        for vehicle in coordinator.data or []:
            entity = ToyotaClimateSettingsSwitch(coordinator, "Use Climate Settings", vehicle.vin)
            if entity.available:
                yield entity
            for name, icon, setting in CLIMATE_SWITCHES:
                entity = ToyotaClimatePreferenceSwitch(setting, icon, coordinator, name, vehicle.vin)
                if entity.available:
                    yield entity
            for schedule in vehicle.charge_settings.get("schedules") or []:
                if not isinstance(schedule, dict) or schedule.get("settingId") is None:
                    continue
                identifier = str(schedule["settingId"])
                entity = ToyotaChargeScheduleSwitch(
                    identifier, config_entry, coordinator, f"Charge Schedule {identifier}", vehicle.vin,
                )
                if entity.available:
                    yield entity

    setup_entity_discovery(
        config_entry, coordinator, async_add_entities, discover_switches,
        remove_stale_entities=remove_stale_schedules,
    )


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
            raise ServiceValidationError("Climate settings are unavailable for this vehicle.")
        with translate_service_errors():
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


class ToyotaChargeScheduleSwitch(ToyotaNABaseEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, identifier, config_entry, *args):
        super().__init__(*args)
        self._identifier = identifier
        self._config_entry = config_entry

    @property
    def schedule(self):
        if self.vehicle is None:
            return {}
        schedules = self.vehicle.charge_settings.get("schedules") or []
        return next((item for item in schedules if isinstance(item, dict)
                     and str(item.get("settingId")) == self._identifier), {})

    @property
    def available(self):
        return (self.vehicle is not None and self.vehicle.supports_charge_schedules
                and isinstance(self.schedule.get("enabled"), bool))

    @property
    def is_on(self):
        return self.schedule.get("enabled")

    @property
    def extra_state_attributes(self):
        return {key: self.schedule.get(key) for key in ("startTime", "endTime", "daysOfTheWeek", "settingId")}

    async def async_turn_on(self, **kwargs):
        await self._set_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self._set_enabled(False)

    async def _set_enabled(self, enabled):
        if not self.available:
            raise ServiceValidationError("This charge schedule is unavailable.")
        with translate_service_errors():
            await self.vehicle.update_charge_schedule(self._identifier, enabled=enabled)
        record_vehicle_wake(self.hass, self._config_entry, self.vin)
        self.coordinator.async_set_updated_data(self.coordinator.data)
