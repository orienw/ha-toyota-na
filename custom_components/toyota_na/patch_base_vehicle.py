from abc import ABC, abstractmethod
import asyncio
from enum import Enum, auto, unique
from typing import Optional, Union

from toyota_na.client import ToyotaOneClient
from toyota_na.vehicle.entity_types.ToyotaLocation import ToyotaLocation
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric
from toyota_na.vehicle.entity_types.ToyotaOpening import ToyotaOpening
from toyota_na.vehicle.entity_types.ToyotaRemoteStart import ToyotaRemoteStart

from .vehicle_helpers import can_extend_remote_runtime, endpoint_generation, first_capability, is_appsync_generation, parse_api_timestamp
from .climate_helpers import apply_climate_changes
from .climate_schedule_helpers import build_climate_schedule, climate_schedule_matches
from .charging_helpers import (
    CHARGE_SETTINGS,
    build_charge_schedule,
    charge_options,
    schedule_identifier,
    schedule_matches,
)

SCHEDULE_UPDATE_TIMEOUT = 90


@unique
class ApiVehicleGeneration(Enum):
    CY17 = "17CY"
    CY17PLUS = "17CYPLUS"
    MM21 = "21MM"
    MM24 = "24MM"
    BEV26 = "26BEV"
    GR86 = "GR86"
    NG86 = "NG86"
    PRE17CY = "PRE17CY"


@unique
class VehicleFeatures(Enum):
    # Doors/Windows
    FrontDriverDoor = auto()
    FrontDriverWindow = auto()
    FrontPassengerDoor = auto()
    FrontPassengerWindow = auto()
    RearDriverDoor = auto()
    RearDriverWindow = auto()
    RearPassengerDoor = auto()
    RearPassengerWindow = auto()
    Trunk = auto()
    Moonroof = auto()
    Hood = auto()
    GlassHatch = auto()
    FrontDriverTireWarning = auto()
    FrontPassengerTireWarning = auto()
    RearDriverTireWarning = auto()
    RearPassengerTireWarning = auto()
    SpareTireWarning = auto()

    # Charging Status
    ChargingStatus = auto()
    ChargingState = auto()
    
    # Numeric values
    DistanceToEmpty = auto()
    FrontDriverTire = auto()
    FrontPassengerTire = auto()
    RearDriverTire = auto()
    RearPassengerTire = auto()
    SpareTirePressure = auto()
    FuelLevel = auto()
    ChargeDistance = auto()
    ChargeDistanceAC = auto()
    ChargeLevel = auto()
    Odometer = auto()
    TripDetailsA = auto()
    TripDetailsB = auto()
    NextService = auto()
    LastTimeStamp = auto()
    LastTirePressureTimeStamp = auto()
    Speed = auto()
    PlugStatus = auto()
    RemainingChargeTime = auto()
    EvTravelableDistance = auto()
    ChargeType = auto()
    ConnectorStatus = auto()
    RemainingChargeTimeTo80 = auto()
    ChargeTargetLimit = auto()
    BatteryPowerSupplyTime = auto()
    GasolinePowerSupplyTime = auto()
    GasolineRange = auto()
    AverageFuelConsumption = auto()
    TripFuelConsumption = auto()
    TripCount = auto()
    ChargingRate = auto()
    ChargeScheduleCount = auto()

    #Times
    OccurrenceDate = auto()

    # Engine status
    RemoteStartStatus = auto()

    # Location
    RealTimeLocation = auto()
    ParkingLocation = auto()


@unique
class RemoteRequestCommand(Enum):
    DoorLock = auto()
    DoorUnlock = auto()
    EngineStart = auto()
    EngineStop = auto()
    HazardsOn = auto()
    HazardsOff = auto()
    VehicleFinder = auto()
    Refresh = auto()
    ChargeStart = auto()
    ChargeResume = auto()
    ChargeStop = auto()
    PowerSupplyStop = auto()
    ExtendRuntime = auto()
    SoundHorn = auto()
    HeadlightsOn = auto()
    SoundBuzzer = auto()
    TrunkLock = auto()
    TrunkUnlock = auto()
    WindowsOpen = auto()
    WindowsClose = auto()
    MoonroofClose = auto()


class ToyotaVehicle(ABC):
    """Vehicle control and metadata object."""

    _client: ToyotaOneClient
    _features: dict[
        VehicleFeatures,
        Union[
            ToyotaLocation,
            ToyotaLockableOpening,
            ToyotaNumeric,
            ToyotaRemoteStart,
            ToyotaOpening,
        ],
    ]
    _has_remote_subscription = False
    _has_electric = False
    _model_name: str
    _model_year: str
    _generation: ApiVehicleGeneration
    _vin: str
    _region: str
    _command_map: dict[RemoteRequestCommand, str] = {}

    _EXTENDED_COMMANDS = {
        RemoteRequestCommand.SoundHorn: ("sound-horn", ("hornCapable",)),
        RemoteRequestCommand.HeadlightsOn: ("headlight-on", ("lightsCapable",)),
        RemoteRequestCommand.SoundBuzzer: ("buzzer-warning", ("buzzerCapable",)),
        RemoteRequestCommand.TrunkLock: (
            "trunk-lock", ("trunkLockUnlockCapable", "powerTailgateCapable"),
        ),
        RemoteRequestCommand.TrunkUnlock: (
            "trunk-unlock", ("trunkLockUnlockCapable", "powerTailgateCapable"),
        ),
        RemoteRequestCommand.WindowsOpen: ("power-window-open", ("powerWindowsOpenCapable",)),
        RemoteRequestCommand.WindowsClose: ("power-window-close", ("powerWindowsCloseCapable",)),
        RemoteRequestCommand.MoonroofClose: ("sunroof-close", ("moonroofCloseCapable",)),
    }

    _COMMAND_CAPABILITIES = {
        RemoteRequestCommand.DoorLock: (
            "dlockUnlockCapable",
            "doorLockUnlockCapable",
        ),
        RemoteRequestCommand.DoorUnlock: (
            "dlockUnlockCapable",
            "doorLockUnlockCapable",
        ),
        RemoteRequestCommand.EngineStart: (
            "estartEnabled",
            "estartStopCapable",
            "remoteEngineStartStop",
        ),
        RemoteRequestCommand.EngineStop: (
            "estopEnabled",
            "estartStopCapable",
            "remoteEngineStartStop",
        ),
        RemoteRequestCommand.HazardsOn: ("hazardCapable",),
        RemoteRequestCommand.HazardsOff: ("hazardCapable",),
        RemoteRequestCommand.VehicleFinder: (
            "vehicleFinderCapable",
            "vehicleFinder",
        ),
    }
    _COMMANDS_REQUIRING_EXPLICIT_CAPABILITY = {
        RemoteRequestCommand.VehicleFinder,
    }

    def __init__(
        self,
        client: ToyotaOneClient,
        has_remote_subscription,
        has_electric,
        model_name: str,
        model_year: str,
        vin: str,
        region: str,
        generation: ApiVehicleGeneration,
        brand: str = "T",
        backdoor_type: Optional[str] = None,
        remote_capabilities: Optional[dict] = None,
        extended_capabilities: Optional[dict] = None,
        feature_flags: Optional[dict] = None,
        legacy_capabilities: Optional[list] = None,
    ):
        """
        Initialize a new vehicle object. Must call `vehicle.update()` to fully populate the object.

        :param vin: Vehicle identification number
        """

        self._features = {}
        self._client = client
        self._generation = generation
        self._has_remote_subscription = has_remote_subscription
        self._has_electric = has_electric
        self._model_name = model_name
        self._model_year = model_year
        self._vin = vin
        self._region = region
        self._brand = brand
        self._backdoor_type = backdoor_type
        self._remote_capabilities = remote_capabilities or {}
        self._extended_capabilities = extended_capabilities or {}
        self._feature_flags = feature_flags
        self._legacy_capabilities = legacy_capabilities or []
        self._climate_settings = {}
        self._climate_lock = asyncio.Lock()
        self._climate_schedules = {}
        self._climate_schedule_lock = asyncio.Lock()
        self._charge_settings = {}
        self._engine_details = {}
        self._schedule_lock = asyncio.Lock()

    @abstractmethod
    async def poll_vehicle_refresh(self) -> None:
        """Instructs Toyota's systems to ping the vehicle to upload a fresh status. Useful when certain actions have been taken, such as locking or unlocking doors."""
        pass

    @abstractmethod
    async def send_command(self, command: RemoteRequestCommand) -> None:
        """Start the engine. Periodically refreshes the vehicle status to determine if the engine is running."""
        pass

    @abstractmethod
    async def update(self):
        """Calls the required Toyota APIs and instantiates all the attributes."""
        pass

    @property
    def features(
        self,
    ) -> dict[
        VehicleFeatures,
        Union[
            ToyotaLocation,
            ToyotaLockableOpening,
            ToyotaNumeric,
            ToyotaOpening,
            ToyotaRemoteStart,
        ],
    ]:
        """Provides a programmatic representation of all the features of the vehicle and their current states."""
        return self._features

    async def poll_engine_status(self):
        """Read REST engine state without waking the vehicle or replacing a push."""
        if self.uses_appsync:
            return None
        previous = self._features.get(VehicleFeatures.RemoteStartStatus)
        if self._generation == ApiVehicleGeneration.NG86:
            status = await self._client.get_engine_status_route(
                self.vin, self.api_generation, self.region, self.brand,
            )
        elif self._generation == ApiVehicleGeneration.MM21:
            status = await self._client.get_engine_status_21mm(self.vin, self.region)
        elif self.endpoint_generation == "17CY":
            status = await self._client.get_engine_status_17cy(self.vin, self.region)
        else:
            status = await self._client.get_engine_status_17cyplus(self.vin, self.region)
        if status and self._features.get(VehicleFeatures.RemoteStartStatus) is previous:
            self._parse_engine_status(status)
        current = self._features.get(VehicleFeatures.RemoteStartStatus)
        return current.on if current is not None and current is not previous else None

    # We only very sparingly expose direct properties. Most vehicle atrributes should be added to the features dictionary.
    @property
    def generation(self):
        return self._generation

    @property
    def endpoint_generation(self):
        return endpoint_generation(self._generation.value)

    @property
    def model_name(self):
        return self._model_name

    @property
    def model_year(self):
        return self._model_year

    @property
    def subscribed(self):
        return self._has_remote_subscription
    
    @property
    def electric(self):
        return self._has_electric

    @property
    def brand(self):
        return self._brand

    @property
    def region(self):
        return self._region

    @property
    def backdoor_type(self):
        return self._backdoor_type

    @property
    def remote_capabilities(self):
        return self._remote_capabilities

    @property
    def capabilities(self):
        """Compatibility alias for remote service capabilities."""
        return self._remote_capabilities

    @property
    def extended_capabilities(self):
        return self._extended_capabilities

    @property
    def api_generation(self):
        """Return the generation string reported by vehicle discovery."""
        return self._generation.value

    @property
    def uses_appsync(self) -> bool:
        return is_appsync_generation(self.api_generation)

    def feature_enabled(self, name: str, *, default=True) -> bool:
        """Older vehicle payloads omit the entire feature-state model."""
        if self._feature_flags is None:
            return default
        value = self._feature_flags.get(name)
        return type(value) is int and value == 1

    @property
    def can_receive_status(self) -> bool:
        return self.generation in (
            ApiVehicleGeneration.CY17, ApiVehicleGeneration.CY17PLUS,
            ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26,
            ApiVehicleGeneration.NG86,
        )

    @property
    def can_start_climate(self) -> bool:
        if self._extended_capabilities.get("remoteEConnectCapable") is True:
            return True
        return self.generation == ApiVehicleGeneration.CY17 and any(
            isinstance(item, dict)
            and str(item.get("name", "")).lower() == "evremoteservice"
            for item in self._legacy_capabilities
        )

    @property
    def supports_climate_settings(self) -> bool:
        return (
            self.subscribed
            and self._extended_capabilities.get("climateCapable") is True
            and self.feature_enabled("remoteClimate")
        )

    @property
    def climate_settings(self) -> dict:
        return self._climate_settings

    async def update_climate_settings(self, **changes) -> None:
        if not self.supports_climate_settings:
            raise ValueError("Climate settings are unavailable for this vehicle.")
        async with self._climate_lock:
            settings = await self._client.get_climate_settings(
                self.vin, self.api_generation, self.region, self.brand
            )
            if not isinstance(settings, dict) or not settings:
                raise RuntimeError("Toyota did not return climate settings.")
            settings = apply_climate_changes(settings, changes)
            await self._client.update_climate_settings(
                self.vin, self.api_generation, settings, self.region, self.brand
            )
            self._climate_settings.clear()
            self._climate_settings.update(settings)

    @property
    def climate_schedules(self):
        return self._climate_schedules

    @property
    def supports_climate_schedules(self):
        return (
            self._extended_capabilities.get("scheduleReservation") is True
            and self.subscribed and self.feature_enabled("remoteClimate")
        )

    async def _read_climate_schedules(self):
        settings = await self._client.get_climate_schedules(
            self.vin, self.api_generation, self.region, self.brand,
        )
        if (
            not isinstance(settings, dict)
            or settings.get("returnCode") not in (None, "ONE-RES-10000")
            or not isinstance(settings.get("airConditioningReservation"), list)
            or any(not isinstance(item, dict) or item.get("reservationNo") is None
                   for item in settings["airConditioningReservation"])
        ):
            raise RuntimeError("Toyota did not return current climate schedules.")
        self._climate_schedules.clear()
        self._climate_schedules.update(settings)
        return settings["airConditioningReservation"]

    async def update_climate_schedules(self):
        if self._extended_capabilities.get("scheduleReservation") is True:
            async with self._climate_schedule_lock:
                await self._read_climate_schedules()

    async def update_climate_schedule(self, identifier=None, *, zone, delete=False, **changes):
        if not self.supports_climate_schedules:
            raise ValueError("Climate schedules are unavailable for this vehicle.")
        async with self._climate_schedule_lock:
            schedules = await self._read_climate_schedules()
            existing = None
            if identifier is not None:
                identifier = schedule_identifier(identifier)
                existing = next((item for item in schedules if str(item["reservationNo"]) == str(identifier)), None)
                if existing is None:
                    raise ValueError("This climate schedule no longer exists.")
            if delete and identifier is None:
                raise ValueError("Choose a climate schedule to delete.")
            previous_ids = {str(item["reservationNo"]) for item in schedules}
            body = {} if delete else build_climate_schedule(self.climate_schedules, existing, changes, zone)
            saved_id = await self._client.save_climate_schedule(
                self.vin, self.api_generation, body, self.region, self.brand,
                identifier=identifier, delete=delete,
            )
            loop = asyncio.get_running_loop()
            deadline = loop.time() + SCHEDULE_UPDATE_TIMEOUT
            delay = 5
            while loop.time() < deadline:
                try:
                    schedules = await asyncio.wait_for(self._read_climate_schedules(), deadline - loop.time())
                except asyncio.TimeoutError:
                    break
                if identifier is not None:
                    candidates = [item for item in schedules if str(item["reservationNo"]) == str(identifier)]
                elif saved_id is not None:
                    candidates = [item for item in schedules if str(item["reservationNo"]) == str(saved_id)
                                  and str(item["reservationNo"]) not in previous_ids]
                else:
                    candidates = [item for item in schedules if str(item["reservationNo"]) not in previous_ids]
                if (delete and not candidates) or (not delete and any(climate_schedule_matches(item, body) for item in candidates)):
                    return
                await asyncio.sleep(min(delay, max(0, deadline - loop.time())))
                delay = min(30, delay * 2)
            raise RuntimeError("Toyota accepted the climate schedule change but did not return the updated schedule.")

    async def update_tire_pressure(self) -> None:
        if self.uses_appsync or not self.can_receive_status:
            return
        status = await self._client.get_tire_pressure(
            self.vin, self.api_generation, self.region, self.brand,
        )
        self._parse_tire_pressure(status)

    def _parse_tire_pressure(self, status) -> None:
        if not isinstance(status, dict) or status.get("vin", self.vin) != self.vin:
            return
        observed_at = parse_api_timestamp(status.get("tirePressureTimestamp"))
        for key, warning_feature in (
            ("flTirePressure", VehicleFeatures.FrontDriverTireWarning),
            ("frTirePressure", VehicleFeatures.FrontPassengerTireWarning),
            ("rlTirePressure", VehicleFeatures.RearDriverTireWarning),
            ("rrTirePressure", VehicleFeatures.RearPassengerTireWarning),
            ("spareTirePressure", VehicleFeatures.SpareTireWarning),
        ):
            tire = status.get(key)
            if not isinstance(tire, dict):
                continue
            warning = tire.get("displayLowTirePressureWarning")
            if isinstance(warning, bool):
                self._store_opening(warning_feature, not warning, None, observed_at)
            value = tire.get("value")
            if type(value) in (int, float):
                self._store_numeric(
                    self._vehicle_telemetry_map[key], value, tire.get("unit") or "psi", observed_at,
                )
        if observed_at is not None:
            self._store_numeric(
                VehicleFeatures.LastTirePressureTimeStamp, observed_at.timestamp(), observed_at=observed_at,
            )

    async def update_climate(self) -> None:
        if self.supports_climate_settings:
            async with self._climate_lock:
                settings = await self._client.get_climate_settings(
                    self.vin, self.api_generation, self.region, self.brand
                )
                if isinstance(settings, dict) and settings:
                    self._climate_settings.clear()
                    self._climate_settings.update(settings)

    async def send_charging_command(self, command: RemoteRequestCommand) -> None:
        command_name = {
            RemoteRequestCommand.ChargeStart: "immediate-charge",
            RemoteRequestCommand.ChargeResume: "resume-charge",
            RemoteRequestCommand.ChargeStop: "charge-stop",
            RemoteRequestCommand.PowerSupplyStop: "power-supply-stop",
        }[command]
        if self.uses_appsync:
            await self._client.remote_request_24mm(self.vin, command_name, self.region)
        else:
            status = await self._client.electric_command(
                self.vin, self.api_generation, command_name, self.region, self.brand
            )
            if status:
                self._parse_electric_status(status)

    @property
    def charge_settings(self):
        return self._charge_settings

    def supports_charge_setting(self, field):
        feature = "powerSupply" if field == "electricSupplyModeLimit" else "chargeSetting"
        return (
            field in CHARGE_SETTINGS and self.uses_appsync and self.electric
            and self.subscribed and self.feature_enabled(feature)
        )

    @property
    def supports_charge_schedules(self):
        return (
            self.electric and self.subscribed
            and self.feature_enabled("multiDayCharging")
            and (self.uses_appsync or (self._feature_flags or {}).get("multiDayCharging") == 1)
            and isinstance(self.charge_settings.get("schedules"), list)
        )

    def _store_charge_schedules(self, schedules, observed_at):
        if not isinstance(schedules, list):
            return
        previous = self._charge_settings.get("_schedules_updated_at")
        if previous is not None and (observed_at is None or observed_at < previous):
            return
        self._charge_settings["schedules"] = schedules
        self._charge_settings["_schedules_updated_at"] = observed_at
        self._features[VehicleFeatures.ChargeScheduleCount] = ToyotaNumeric(len(schedules), "")

    async def _read_charge_schedules(self):
        if self.uses_appsync:
            status = await self._client.graphql_get_vehicle_status(self.vin, self.backdoor_type, self.region)
            self.apply_graphql_status(status)
            electric = (status or {}).get("electric") or {}
            charging = electric.get("charging") or {}
            schedules = (charging.get("chargeSettings") or {}).get("schedules")
        else:
            status = await self._client.get_electric_status(self.vin, region=self.region, generation=self.api_generation)
            self._parse_electric_status(status)
            schedules = ((status or {}).get("vehicleInfo") or {}).get("timerChargeInfo")
        if not isinstance(schedules, list):
            raise RuntimeError("Toyota did not return current charge schedules.")
        return schedules

    async def update_charge_schedule(self, identifier=None, *, delete=False, **changes):
        if not self.supports_charge_schedules:
            raise ValueError("Multi-day charge schedules are unavailable for this vehicle.")
        async with self._schedule_lock:
            schedules = await self._read_charge_schedules()
            if identifier is not None:
                identifier = schedule_identifier(identifier)
            previous_ids = {
                str(item.get("settingId")) for item in schedules if isinstance(item, dict)
            }
            if delete:
                if identifier is None or str(identifier) not in previous_ids:
                    raise ValueError("This charge schedule no longer exists.")
                body = {"settingId": identifier}
            else:
                body = build_charge_schedule(schedules, identifier, **changes)
                maximum = self.charge_settings.get("maxNoOfChargeSchedules")
                if identifier is None and isinstance(maximum, int) and len(schedules) >= maximum:
                    raise ValueError("The vehicle has reached its charge schedule limit.")
            await self._client.save_charge_schedule(
                self.vin, self.api_generation, body, self.region, self.brand, delete=delete,
            )

            def confirmed(schedules):
                candidates = [item for item in schedules if isinstance(item, dict)]
                if identifier is None:
                    candidates = [item for item in candidates if str(item.get("settingId")) not in previous_ids]
                else:
                    candidates = [item for item in candidates if str(item.get("settingId")) == str(identifier)]
                return (delete and not candidates) or (not delete and any(schedule_matches(item, body) for item in candidates))

            await self._wait_for_charge_schedules(confirmed)

    async def disable_charge_schedules(self) -> bool:
        if not self.supports_charge_schedules:
            raise ValueError("Multi-day charge schedules are unavailable for this vehicle.")
        async with self._schedule_lock:
            def all_disabled(schedules):
                return all(isinstance(item, dict) and item.get("enabled") is False for item in schedules)

            if all_disabled(await self._read_charge_schedules()):
                return False
            await self._client.disable_charge_schedules(
                self.vin, self.api_generation, self.region, self.brand,
            )
            await self._wait_for_charge_schedules(all_disabled)
            return True

    async def _wait_for_charge_schedules(self, confirmed):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SCHEDULE_UPDATE_TIMEOUT
        readback_delay = 5
        while loop.time() < deadline:
            try:
                schedules = await asyncio.wait_for(self._read_charge_schedules(), deadline - loop.time())
            except asyncio.TimeoutError:
                break
            if confirmed(schedules):
                return
            await asyncio.sleep(min(readback_delay, max(0, deadline - loop.time())))
            readback_delay = min(30, readback_delay * 2)
        raise RuntimeError("Toyota accepted the schedule change but did not return the updated schedule.")

    async def set_charge_setting(self, field, option):
        if not self.supports_charge_setting(field):
            raise ValueError("Charging preferences are unavailable for this vehicle.")
        status = await self._client.graphql_get_vehicle_status(self.vin, self.backdoor_type, self.region)
        if not status:
            raise RuntimeError("Toyota did not return charging preferences.")
        self.apply_graphql_status(status)
        charging = ((status.get("electric") or {}).get("charging") or {})
        options = charge_options({
            **(charging.get("chargeSettings") or {}),
            "limitSelectionValues": charging.get("limitSelectionValues"),
        }, field, allow_missing_target=bool(charging) and self.feature_enabled("chargeSetting", default=False))
        if option not in options:
            raise ValueError("This charging option is unavailable for this vehicle.")
        await self._client.update_charge_settings(
            self.vin, CHARGE_SETTINGS[field][3], options[option], self.region,
        )
        self.apply_graphql_status(
            await self._client.graphql_get_vehicle_status(self.vin, self.backdoor_type, self.region)
        )

    async def send_extended_command(self, command: RemoteRequestCommand) -> None:
        command_name, _ = self._EXTENDED_COMMANDS[command]
        if self.uses_appsync:
            await self._client.remote_request_24mm(self.vin, command_name, self.region)
        else:
            await self._client.remote_request_route(
                self.vin, self.api_generation, command_name, self.region, self.brand,
            )

    def supports_command(self, command: RemoteRequestCommand) -> bool:
        """Return whether the API transport and vehicle support a command."""
        if command == RemoteRequestCommand.Refresh:
            return self.subscribed and self.feature_enabled("vehicleState")
        if not self.subscribed or not self.feature_enabled("remoteCommands"):
            return False
        if command == RemoteRequestCommand.ExtendRuntime:
            return self.uses_appsync and can_extend_remote_runtime(self._engine_details)
        if command in self._EXTENDED_COMMANDS:
            if command == RemoteRequestCommand.TrunkLock and self.uses_appsync:
                return False
            _, keys = self._EXTENDED_COMMANDS[command]
            return any(
                first_capability(self._remote_capabilities, self._extended_capabilities, (key,)) is True
                for key in keys
            )
        if command in (
            RemoteRequestCommand.ChargeStart,
            RemoteRequestCommand.ChargeResume,
            RemoteRequestCommand.ChargeStop,
            RemoteRequestCommand.PowerSupplyStop,
        ):
            if not self.electric:
                return False
            states = {
                RemoteRequestCommand.ChargeStart: ("36", "charge_now"),
                RemoteRequestCommand.ChargeResume: ("resume_charging",),
                RemoteRequestCommand.ChargeStop: ("charging",),
                RemoteRequestCommand.PowerSupplyStop: ("external_power_active", "external_power_active_hybrid"),
            }
            state = self.features.get(VehicleFeatures.ChargingState)
            state = str(state.value).lower() if state is not None else None
            return state in states[command] and (
                command == RemoteRequestCommand.ChargeStart or self.uses_appsync
            )
        if command not in self._command_map:
            return False

        if command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop):
            if self.can_start_climate:
                return True

        keys = self._COMMAND_CAPABILITIES.get(command)
        if keys is None:
            return True
        supported = first_capability(
            self._remote_capabilities,
            self._extended_capabilities,
            keys,
        )
        if command in self._COMMANDS_REQUIRING_EXPLICIT_CAPABILITY or self.generation == ApiVehicleGeneration.NG86:
            return supported is True
        return supported is not False

    def inherit_state(self, previous: "ToyotaVehicle") -> bool:
        """Share observations with matching vehicle instances used by active polls."""
        if (
            previous.vin != self.vin
            or previous.generation != self.generation
            or previous.region != self.region
            or previous.electric != self.electric
        ):
            return False
        self._features = previous.features
        self._climate_settings = previous.climate_settings
        self._climate_lock = previous._climate_lock
        self._climate_schedules = previous.climate_schedules
        self._climate_schedule_lock = previous._climate_schedule_lock
        self._charge_settings = previous.charge_settings
        self._engine_details = previous._engine_details
        self._schedule_lock = previous._schedule_lock
        return True

    @property
    def vin(self):
        return self._vin

    def __repr__(self):
        str = f"{self.__class__.__name__}(\n    features=(\n"
        for key, value in self._features.items():
            str += f"       {key}={value}\n"
        return f"{str}  )\n)"
