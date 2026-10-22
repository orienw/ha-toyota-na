from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfPressure
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_conversion import PressureConverter

from toyota_na.vehicle.base_vehicle import ToyotaVehicle, VehicleFeatures
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric

from .base_entity import ToyotaNABaseEntity
from .climate_schedule_helpers import local_climate_schedule
from .const import DOMAIN, HEALTH_SENSORS, REMOTE_ACCESS_STATES, SENSORS
from .entity_discovery import setup_entity_discovery
from .health_helpers import health_reading

# Toyota reports tire pressure units in varying case, such as "kpa".
_PRESSURE_UNITS = {"psi": UnitOfPressure.PSI, "kpa": UnitOfPressure.KPA, "bar": UnitOfPressure.BAR}


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_devices: AddEntitiesCallback,
):
    """Set up vehicle sensors."""
    coordinator: DataUpdateCoordinator[list[ToyotaVehicle]] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator"]

    def discover_sensors():
        for vehicle in coordinator.data or []:
            if isinstance(vehicle.climate_schedules.get("airConditioningReservation"), list):
                yield ToyotaClimateSchedulesSensor(coordinator, "Climate Schedules", vehicle.vin)
            if vehicle.remote_display is not None:
                yield ToyotaRemoteAccessSensor(coordinator, "Remote Access", vehicle.vin)
            for config in HEALTH_SENSORS:
                if health_reading(vehicle, config["features"], config["items"]) is not None:
                    yield ToyotaHealthSensor(config, coordinator, config["name"], vehicle.vin)
            for config in SENSORS:
                feature = vehicle.features.get(config["feature"])
                if not isinstance(feature, ToyotaNumeric):
                    continue
                if vehicle.electric is False and config["electric"]:
                    continue
                yield ToyotaSensor(
                    config["feature"],
                    config["icon"],
                    config["unit"],
                    config["state_class"],
                    coordinator,
                    config["name"],
                    vehicle.vin,
                    device_class=config.get("device_class"),
                    states=config.get("states"),
                    translation_key=config.get("translation_key"),
                )

    setup_entity_discovery(config_entry, coordinator, async_add_devices, discover_sensors)


class ToyotaClimateSchedulesSensor(ToyotaNABaseEntity, SensorEntity):
    _attr_icon = "mdi:calendar-clock"

    @property
    def available(self):
        return self.vehicle is not None and isinstance(
            self.vehicle.climate_schedules.get("airConditioningReservation"), list
        )

    @property
    def native_value(self):
        if self.available:
            return len(self.vehicle.climate_schedules["airConditioningReservation"])
        return None

    @property
    def extra_state_attributes(self):
        if not self.available:
            return None
        settings = self.vehicle.climate_schedules
        zone = ZoneInfo(self.hass.config.time_zone)
        return {
            "schedules": [
                local_climate_schedule(item, zone)
                for item in settings["airConditioningReservation"]
            ],
            "temperature_unit": settings.get("temperatureUnit"),
            "min_temperature": settings.get("minTemp"),
            "max_temperature": settings.get("maxTemp"),
            "temperature_step": settings.get("tempInterval"),
        }


class ToyotaRemoteAccessSensor(ToyotaNABaseEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:car-connected"
    _attr_options = list(dict.fromkeys(REMOTE_ACCESS_STATES.values()))
    _attr_translation_key = "remote_access"

    @property
    def available(self):
        return self.vehicle is not None and self.vehicle.remote_display is not None

    @property
    def native_value(self):
        return REMOTE_ACCESS_STATES.get(self.vehicle.remote_display) if self.available else None

    @property
    def extra_state_attributes(self):
        return {"raw_value": self.vehicle.remote_display} if self.available else None


class ToyotaHealthSensor(ToyotaNABaseEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, config, *args):
        super().__init__(*args)
        self._config = config
        self._attr_icon = config["icon"]

    @property
    def _items(self):
        return health_reading(self.vehicle, self._config["features"], self._config["items"])

    @property
    def available(self):
        return self._items is not None

    @property
    def native_value(self):
        items = self._items
        return None if items is None else len(items)

    @property
    def extra_state_attributes(self):
        items = self._items
        return None if items is None else {self._config["attribute"]: items}


class ToyotaSensor(ToyotaNABaseEntity, SensorEntity):
    def __init__(
        self,
        vehicle_feature: VehicleFeatures,
        icon: str,
        unit_of_measurement: str | None,
        state_class: SensorStateClass | None,
        *args: Any,
        device_class: SensorDeviceClass | None = None,
        states: dict[str, str] | None = None,
        translation_key: str | None = None,
    ):
        super().__init__(*args)
        self._attr_icon = icon
        self._attr_state_class = state_class
        self._attr_device_class = device_class
        self._attr_translation_key = translation_key
        self._attr_options = list(dict.fromkeys(states.values())) if states else None
        self._states = states
        self._unit_of_measurement = unit_of_measurement
        self._vehicle_feature = vehicle_feature

    @property
    def native_value(self):
        feature = self.feature(self._vehicle_feature)
        if not isinstance(feature, ToyotaNumeric) or feature.value is None:
            return None
        if self._vehicle_feature == VehicleFeatures.FuelLevel and isinstance(
            feature.value, (int, float)
        ):
            return min(100, max(0, feature.value))
        if self._states:
            return self._states.get(str(feature.value).lower())
        if self.device_class == SensorDeviceClass.TIMESTAMP:
            return datetime.fromtimestamp(feature.value, UTC)
        if self._unit_of_measurement == UnitOfPressure.PSI and feature.unit:
            unit = _PRESSURE_UNITS.get(str(feature.unit).lower())
            if unit is None:
                return None
            if unit != UnitOfPressure.PSI:
                return round(PressureConverter.convert(feature.value, unit, UnitOfPressure.PSI), 1)
        return feature.value

    @property
    def native_unit_of_measurement(self):
        if self.device_class in (SensorDeviceClass.ENUM, SensorDeviceClass.TIMESTAMP):
            return None
        feature = self.feature(self._vehicle_feature)
        unit = feature.unit if isinstance(feature, ToyotaNumeric) else None
        if self._vehicle_feature == VehicleFeatures.Speed:
            return unit or self._unit_of_measurement
        if self._unit_of_measurement in (None, "MI_OR_KM"):
            return unit or None
        return self._unit_of_measurement or None

    @property
    def available(self):
        return isinstance(self.feature(self._vehicle_feature), ToyotaNumeric)

    @property
    def extra_state_attributes(self):
        if self._states:
            feature = self.feature(self._vehicle_feature)
            return {"raw_value": feature.value if isinstance(feature, ToyotaNumeric) else None}
        if self._vehicle_feature == VehicleFeatures.ChargeScheduleCount and self.vehicle:
            return {"schedules": self.vehicle.charge_settings.get("schedules", [])}
        return None
