import asyncio
import logging
import time
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto, unique

import aiohttp

from .charging_helpers import (
    CHARGE_SETTINGS,
    build_charge_schedule,
    charge_options,
    schedule_identifier,
    schedule_matches,
)
from .client import ToyotaOneClient
from .climate_helpers import apply_climate_changes
from .climate_schedule_helpers import build_climate_schedule, climate_schedule_matches
from .exceptions import AuthError
from .vehicle_helpers import (
    app_int,
    backdoor_candidates,
    can_extend_remote_runtime,
    first_capability,
    merge_opening_states,
    normalize_charging_state,
    normalize_engine_state,
    opening_state_from_graphql,
    opening_state_from_values,
    parse_api_timestamp,
)
from .vehicle_values import Location, Measurement, Opening, RemoteStart, VehicleValue

_LOGGER = logging.getLogger(__name__)

SCHEDULE_UPDATE_TIMEOUT = 90
# Toyota's app reads vehicle health once per dashboard load.
HEALTH_READ_INTERVAL = 3600

# Legacy generation that Toyota reports for non-connected pre-2018 vehicles.
KNOWN_UNSUPPORTED_GENERATIONS = frozenset({"PRE17CY"})


@unique
class ApiVehicleGeneration(Enum):
    CY17 = "17CY"
    CY17PLUS = "17CYPLUS"
    MM21 = "21MM"
    MM24 = "24MM"
    BEV26 = "26BEV"
    GR86 = "GR86"
    NG86 = "NG86"


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


_LEGACY_COMMAND_MAP = {
    RemoteRequestCommand.DoorLock: "DL",
    RemoteRequestCommand.DoorUnlock: "DL",
    RemoteRequestCommand.EngineStart: "RES",
    RemoteRequestCommand.EngineStop: "RES",
    RemoteRequestCommand.HazardsOn: "HZ",
    RemoteRequestCommand.HazardsOff: "HZ",
    RemoteRequestCommand.Refresh: "refresh",
}

_LEGACY_COMMAND_VALUE_MAP = {
    RemoteRequestCommand.DoorLock: 1,
    RemoteRequestCommand.DoorUnlock: 2,
    RemoteRequestCommand.EngineStart: 1,
    RemoteRequestCommand.EngineStop: 2,
    RemoteRequestCommand.HazardsOn: 1,
    RemoteRequestCommand.HazardsOff: 2,
}

_MODERN_COMMAND_MAP = {
    RemoteRequestCommand.DoorLock: "door-lock",
    RemoteRequestCommand.DoorUnlock: "door-unlock",
    RemoteRequestCommand.EngineStart: "engine-start",
    RemoteRequestCommand.EngineStop: "engine-stop",
    RemoteRequestCommand.HazardsOn: "hazard-on",
    RemoteRequestCommand.HazardsOff: "hazard-off",
    RemoteRequestCommand.VehicleFinder: "find-vehicle",
    RemoteRequestCommand.Refresh: "refresh",
    RemoteRequestCommand.ExtendRuntime: "add-runtime",
}


@dataclass(frozen=True)
class GenerationTransport:
    """Wire transport and capability rules for one Toyota telematics generation."""

    endpoint_generation: str
    status_transport: str
    engine_transport: str | None
    refresh_transports: tuple[str, ...]
    command_transport: str
    uses_appsync: bool = False
    appsync_push: bool = False
    explicit_command_capabilities: bool = False
    allows_ev_climate_schedules: bool = True


GENERATION_TRANSPORTS: dict[ApiVehicleGeneration, GenerationTransport] = {
    ApiVehicleGeneration.CY17: GenerationTransport(
        endpoint_generation="17CY",
        status_transport="legacy",
        engine_transport="legacy",
        refresh_transports=("legacy",),
        command_transport="legacy",
        allows_ev_climate_schedules=False,
    ),
    ApiVehicleGeneration.CY17PLUS: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="global",
        engine_transport="global",
        refresh_transports=("global",),
        command_transport="global",
    ),
    ApiVehicleGeneration.MM21: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="route_21mm",
        engine_transport="route_21mm",
        refresh_transports=("graphql", "route_21mm"),
        command_transport="route_21mm",
        appsync_push=True,
    ),
    ApiVehicleGeneration.MM24: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="graphql",
        engine_transport=None,
        refresh_transports=("graphql",),
        command_transport="graphql",
        uses_appsync=True,
        appsync_push=True,
    ),
    ApiVehicleGeneration.BEV26: GenerationTransport(
        endpoint_generation="17CYPLUS",
        status_transport="graphql",
        engine_transport=None,
        refresh_transports=("graphql",),
        command_transport="graphql",
        uses_appsync=True,
        appsync_push=True,
    ),
    ApiVehicleGeneration.NG86: GenerationTransport(
        endpoint_generation="NG86",
        status_transport="route",
        engine_transport="route",
        refresh_transports=("route",),
        command_transport="route",
        explicit_command_capabilities=True,
        allows_ev_climate_schedules=False,
    ),
    ApiVehicleGeneration.GR86: GenerationTransport(
        endpoint_generation="GR86",
        status_transport="route",
        engine_transport="route",
        refresh_transports=("route",),
        command_transport="route",
        explicit_command_capabilities=True,
    ),
}


async def fetch_vehicle_status(
    client: ToyotaOneClient,
    transport: GenerationTransport,
    *,
    vin: str,
    generation: str,
    region: str,
    brand: str,
    backdoor_type: str | None,
):
    """Fetch cached vehicle status using the generation's configured transport."""
    kind = transport.status_transport
    if kind == "graphql":
        return await client.graphql_get_vehicle_status(vin, backdoor_type, region)
    if kind == "legacy":
        return await client.get_vehicle_status_17cy(vin, region)
    if kind == "route_21mm":
        return await client.get_vehicle_status_21mm(vin, region)
    if kind == "route":
        return await client.get_vehicle_status_route(vin, generation, region, brand)
    return await client.get_vehicle_status_17cyplus(vin, region)


async def fetch_engine_status(
    client: ToyotaOneClient,
    transport: GenerationTransport,
    *,
    vin: str,
    generation: str,
    region: str,
    brand: str,
):
    """Fetch REST engine status when the generation exposes a separate endpoint."""
    kind = transport.engine_transport
    if kind is None:
        return None
    if kind == "legacy":
        return await client.get_engine_status_17cy(vin, region)
    if kind == "route_21mm":
        return await client.get_engine_status_21mm(vin, region)
    if kind == "route":
        return await client.get_engine_status_route(vin, generation, region, brand)
    return await client.get_engine_status_17cyplus(vin, region)


class ToyotaVehicle:
    """Vehicle control, state, and metadata across all supported generations."""

    _EXTENDED_COMMANDS = {
        RemoteRequestCommand.SoundHorn: ("sound-horn", ("hornCapable",)),
        RemoteRequestCommand.HeadlightsOn: ("headlight-on", ("lightsCapable",)),
        RemoteRequestCommand.SoundBuzzer: ("buzzer-warning", ("buzzerCapable",)),
        RemoteRequestCommand.TrunkLock: (
            "trunk-lock",
            ("trunkLockUnlockCapable", "powerTailgateCapable"),
        ),
        RemoteRequestCommand.TrunkUnlock: (
            "trunk-unlock",
            ("trunkLockUnlockCapable", "powerTailgateCapable"),
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

    _vehicle_status_category_map = {
        "Driver Side Door": VehicleFeatures.FrontDriverDoor,
        "Driver Side Window": VehicleFeatures.FrontDriverWindow,
        "Passenger Side Door": VehicleFeatures.FrontPassengerDoor,
        "Passenger Side Window": VehicleFeatures.FrontPassengerWindow,
        "Driver Side Rear Door": VehicleFeatures.RearDriverDoor,
        "Driver Side Rear Window": VehicleFeatures.RearDriverWindow,
        "Passenger Side Rear Door": VehicleFeatures.RearPassengerDoor,
        "Passenger Side Rear Window": VehicleFeatures.RearPassengerWindow,
        "Other Hatch": VehicleFeatures.Trunk,
        "Other Trunk": VehicleFeatures.Trunk,
        "Other Tailgate": VehicleFeatures.Trunk,
        "Other Moonroof": VehicleFeatures.Moonroof,
        "Other Hood": VehicleFeatures.Hood,
        "Other Back window": VehicleFeatures.GlassHatch,
        "Other Glass Hatch": VehicleFeatures.GlassHatch,
    }

    _vehicle_telemetry_map = {
        "distanceToEmpty": VehicleFeatures.DistanceToEmpty,
        "flTirePressure": VehicleFeatures.FrontDriverTire,
        "frTirePressure": VehicleFeatures.FrontPassengerTire,
        "rlTirePressure": VehicleFeatures.RearDriverTire,
        "rrTirePressure": VehicleFeatures.RearPassengerTire,
        "fuelLevel": VehicleFeatures.FuelLevel,
        "odometer": VehicleFeatures.Odometer,
        "spareTirePressure": VehicleFeatures.SpareTirePressure,
        "tripA": VehicleFeatures.TripDetailsA,
        "tripB": VehicleFeatures.TripDetailsB,
        "nextService": VehicleFeatures.NextService,
        "speed": VehicleFeatures.Speed,
    }

    # REST status sections Toyota's app finds by name in any category.
    _REST_ANY_CATEGORY_SECTIONS = {
        "trunk": VehicleFeatures.Trunk,
        "hatch": VehicleFeatures.Trunk,
        "tailgate": VehicleFeatures.Trunk,
        "moonroof": VehicleFeatures.Moonroof,
        "hood": VehicleFeatures.Hood,
        "bonnet": VehicleFeatures.Hood,
    }

    # Telemetry opening states, read only when the owner opts in.
    _TELEMETRY_OPENINGS = {
        "driverWindow": VehicleFeatures.FrontDriverWindow,
        "passengerWindow": VehicleFeatures.FrontPassengerWindow,
        "rlWindow": VehicleFeatures.RearDriverWindow,
        "rrWindow": VehicleFeatures.RearPassengerWindow,
        "sunRoof": VehicleFeatures.Moonroof,
    }

    _graphql_door_map = {
        "driverSide": VehicleFeatures.FrontDriverDoor,
        "passengerSide": VehicleFeatures.FrontPassengerDoor,
        "rearDriverSide": VehicleFeatures.RearDriverDoor,
        "rearPassengerSide": VehicleFeatures.RearPassengerDoor,
    }

    _graphql_window_map = {
        "driverSide": VehicleFeatures.FrontDriverWindow,
        "passengerSide": VehicleFeatures.FrontPassengerWindow,
        "rearDriverSide": VehicleFeatures.RearDriverWindow,
        "rearPassengerSide": VehicleFeatures.RearPassengerWindow,
    }

    _graphql_tire_map = {
        "frontLeft": VehicleFeatures.FrontDriverTire,
        "frontRight": VehicleFeatures.FrontPassengerTire,
        "rearLeft": VehicleFeatures.RearDriverTire,
        "rearRight": VehicleFeatures.RearPassengerTire,
        "spare": VehicleFeatures.SpareTirePressure,
    }

    _graphql_tire_warning_map = {
        "frontLeft": VehicleFeatures.FrontDriverTireWarning,
        "frontRight": VehicleFeatures.FrontPassengerTireWarning,
        "rearLeft": VehicleFeatures.RearDriverTireWarning,
        "rearRight": VehicleFeatures.RearPassengerTireWarning,
        "spare": VehicleFeatures.SpareTireWarning,
    }

    def __init__(
        self,
        client: ToyotaOneClient,
        has_remote_subscription: bool,
        has_electric: bool,
        model_name: str,
        model_year: str,
        vin: str,
        region: str,
        generation: ApiVehicleGeneration = ApiVehicleGeneration.CY17PLUS,
        brand: str = "T",
        backdoor_type: str | None = None,
        remote_capabilities: dict | None = None,
        extended_capabilities: dict | None = None,
        feature_flags: dict | None = None,
        legacy_capabilities: list | None = None,
        remote_display: int | None = None,
        window_telemetry: bool = False,
    ):
        self._features: dict[VehicleFeatures, VehicleValue] = {}
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
        self._remote_display = remote_display
        self._window_telemetry = window_telemetry
        self._climate_settings = {}
        self._climate_lock = asyncio.Lock()
        self._climate_schedules = {}
        self._climate_schedule_lock = asyncio.Lock()
        self._climate_schedule_writes = set()
        self._charge_settings = {}
        self._engine_details = {}
        self._schedule_lock = asyncio.Lock()
        self._health = {}
        self._tire_pressure = None
        self._notifications = None
        self._feature_timestamps: dict[tuple, datetime] = {}
        self._status_openings: set[VehicleFeatures] = set()

    def update_metadata(
        self,
        *,
        has_remote_subscription: bool,
        model_name: str | None = None,
        model_year: str | None = None,
        brand: str | None = None,
        backdoor_type: str | None = None,
        remote_capabilities: dict | None = None,
        extended_capabilities: dict | None = None,
        feature_flags: dict | None = None,
        legacy_capabilities: list | None = None,
        remote_display: int | None = None,
        window_telemetry: bool | None = None,
    ) -> None:
        """Refresh vehicle metadata in-place while preserving accumulated state and locks."""
        self._has_remote_subscription = has_remote_subscription
        if model_name:
            self._model_name = model_name
        if model_year:
            self._model_year = model_year
        if brand is not None:
            self._brand = brand
        self._backdoor_type = backdoor_type
        self._remote_capabilities = remote_capabilities or {}
        self._extended_capabilities = extended_capabilities or {}
        self._feature_flags = feature_flags
        self._legacy_capabilities = legacy_capabilities or []
        self._remote_display = remote_display
        if window_telemetry is not None:
            self._window_telemetry = window_telemetry

    @property
    def _transport(self) -> GenerationTransport:
        return GENERATION_TRANSPORTS[self._generation]

    @property
    def _command_map(self) -> dict[RemoteRequestCommand, str]:
        if self._transport.command_transport == "legacy":
            return _LEGACY_COMMAND_MAP
        return _MODERN_COMMAND_MAP

    @property
    def features(self) -> dict[VehicleFeatures, VehicleValue]:
        """Current observations for all reported vehicle features."""
        return self._features

    @property
    def doors_locked(self) -> bool | None:
        """Combine the reported door locks without including cargo openings."""
        states = [
            feature.locked
            for key, feature in self.features.items()
            if key
            in (
                VehicleFeatures.FrontDriverDoor,
                VehicleFeatures.FrontPassengerDoor,
                VehicleFeatures.RearDriverDoor,
                VehicleFeatures.RearPassengerDoor,
            )
            and isinstance(feature, Opening)
            and feature.locked is not None
        ]
        return all(states) if states else None

    @property
    def generation(self) -> ApiVehicleGeneration:
        return self._generation

    @property
    def endpoint_generation(self) -> str:
        return self._transport.endpoint_generation

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def model_year(self) -> str:
        return self._model_year

    @property
    def subscribed(self) -> bool:
        return self._has_remote_subscription

    @property
    def electric(self) -> bool:
        return self._has_electric

    @property
    def brand(self) -> str:
        return self._brand

    @property
    def region(self) -> str:
        return self._region

    @property
    def backdoor_type(self) -> str | None:
        return self._backdoor_type

    @property
    def remote_capabilities(self) -> dict:
        return self._remote_capabilities

    @property
    def extended_capabilities(self) -> dict:
        return self._extended_capabilities

    @property
    def remote_display(self) -> int | None:
        """Return Toyota's remote activation state from the vehicle list."""
        return self._remote_display

    @property
    def stolen(self) -> bool:
        """Toyota's app turns off remote controls while a vehicle is reported stolen."""
        return self._remote_display in (10, 11)

    @property
    def api_generation(self) -> str:
        """Return the generation string reported by vehicle discovery."""
        return self._generation.value

    @property
    def uses_appsync(self) -> bool:
        return self._transport.uses_appsync

    def feature_enabled(self, name: str, *, default=True) -> bool:
        """Older vehicle payloads omit the entire feature-state model."""
        if self._feature_flags is None:
            return default
        value = self._feature_flags.get(name)
        return type(value) is int and value == 1

    @property
    def can_receive_status(self) -> bool:
        return self._generation in GENERATION_TRANSPORTS

    @property
    def can_start_climate(self) -> bool:
        if self._extended_capabilities.get("remoteEConnectCapable") is True:
            return True
        return self.generation == ApiVehicleGeneration.CY17 and any(
            isinstance(item, dict) and str(item.get("name", "")).lower() == "evremoteservice"
            for item in self._legacy_capabilities
        )

    @property
    def supports_climate_settings(self) -> bool:
        return (
            self.subscribed
            and not self.stolen
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
    def climate_schedules(self) -> dict:
        return self._climate_schedules

    @property
    def _offers_climate_schedules(self) -> bool:
        # Toyota's app shows schedules for climate-capable 24MM and newer
        # vehicles, and for electric vehicles other than 17CY and NG86.
        return self._extended_capabilities.get("climateCapable") is True and (
            self.uses_appsync or (self.electric and self._transport.allows_ev_climate_schedules)
        )

    @property
    def supports_climate_schedules(self) -> bool:
        return (
            self._offers_climate_schedules
            and self.subscribed
            and not self.stolen
            and self.feature_enabled("remoteClimate")
        )

    async def _read_climate_schedules(self):
        settings = await self._client.get_climate_schedules(
            self.vin,
            self.api_generation,
            self.region,
            self.brand,
        )
        # Like Toyota's app, a successful response without a list has no
        # schedules, and reservations without an ID are skipped. A failure
        # code is rejected so it can't clear schedules and remove switches.
        if (
            isinstance(settings, dict)
            and settings.get("returnCode") == "ONE-RES-10000"
            and settings.get("airConditioningReservation") is None
        ):
            settings["airConditioningReservation"] = []
        if (
            not isinstance(settings, dict)
            or settings.get("returnCode") not in (None, "ONE-RES-10000")
            or not isinstance(settings.get("airConditioningReservation"), list)
            or any(not isinstance(item, dict) for item in settings["airConditioningReservation"])
        ):
            raise RuntimeError("Toyota did not return current climate schedules.")
        reservations = [
            item
            for item in settings["airConditioningReservation"]
            if item.get("reservationNo") is not None
        ]
        for item in reservations:
            # Toyota's app matches day names in any case.
            if isinstance(item.get("days"), list):
                item["days"] = [
                    day.capitalize() if isinstance(day, str) else day for day in item["days"]
                ]
        self._climate_schedules.clear()
        self._climate_schedules.update(settings, airConditioningReservation=reservations)
        return reservations, len(settings["airConditioningReservation"]) - len(reservations)

    async def update_climate_schedules(self) -> None:
        # A pending change reloads the schedules itself, so skip the read
        # rather than hold up the poll while it confirms.
        if self._offers_climate_schedules and not self._climate_schedule_writes:
            async with self._climate_schedule_lock:
                await self._read_climate_schedules()

    @asynccontextmanager
    async def _climate_schedule_write(self):
        """Serialize a change, marking it pending while it waits for the lock."""
        write = object()
        self._climate_schedule_writes.add(write)
        try:
            async with self._climate_schedule_lock:
                yield
        finally:
            self._climate_schedule_writes.discard(write)

    async def update_climate_schedule(self, identifier=None, *, zone, delete=False, **changes):
        if not self.supports_climate_schedules:
            raise ValueError("Climate schedules are unavailable for this vehicle.")
        async with self._climate_schedule_write():
            schedules, unidentified = await self._read_climate_schedules()
            existing = None
            if identifier is not None:
                identifier = schedule_identifier(identifier)
                existing = next(
                    (item for item in schedules if str(item["reservationNo"]) == str(identifier)),
                    None,
                )
                if existing is None:
                    raise ValueError("This climate schedule no longer exists.")
            if delete and identifier is None:
                raise ValueError("Choose a climate schedule to delete.")
            previous_ids = {str(item["reservationNo"]) for item in schedules}
            body = (
                {}
                if delete
                else build_climate_schedule(self.climate_schedules, existing, changes, zone)
            )
            result = await self._client.save_climate_schedule(
                self.vin,
                self.api_generation,
                body,
                self.region,
                self.brand,
                identifier=identifier,
                delete=delete,
            )
            saved_id = result.get("reservationNo")

            def confirmed(reading):
                schedules, now_unidentified = reading
                if identifier is not None:
                    candidates = [
                        item for item in schedules if str(item["reservationNo"]) == str(identifier)
                    ]
                elif saved_id is not None:
                    candidates = [
                        item
                        for item in schedules
                        if str(item["reservationNo"]) == str(saved_id)
                        and str(item["reservationNo"]) not in previous_ids
                    ]
                else:
                    candidates = [
                        item for item in schedules if str(item["reservationNo"]) not in previous_ids
                    ]
                # A reservation that newly lacks an ID may be the deleted one.
                if delete:
                    return not candidates and now_unidentified <= unidentified
                return any(climate_schedule_matches(item, body, zone) for item in candidates)

            message = "Toyota accepted the climate schedule change but did not return the updated schedule."
            if result.get("returnCode") not in (None, "ONE-RES-10000") and result.get("message"):
                message = result["message"]
            await self._wait_for_schedules(self._read_climate_schedules, confirmed, message)

    async def update(self, *, ws_handler=None) -> None:
        """Read all applicable endpoints for this vehicle sequentially."""
        try:
            telemetry = await self._client.get_telemetry(
                self._vin,
                self._region,
                self.endpoint_generation,
            )
            if telemetry:
                self._parse_telemetry(telemetry)
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching telemetry: %s", e)

        if self.can_receive_status:
            if self._transport.appsync_push and ws_handler is not None:
                try:
                    # Cached push data fills gaps. The polled source below wins when
                    # neither response has timestamps.
                    self.apply_graphql_status(ws_handler.get_cached_status(self._vin))
                except AuthError:
                    raise
                except Exception as e:
                    _LOGGER.debug("Error parsing cached vehicle status: %s", e)

            try:
                await self.poll_vehicle_status()
            except AuthError:
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching vehicle status: %s", e)

            if self._transport.engine_transport is not None:
                try:
                    await self.poll_engine_status()
                except AuthError:
                    raise
                except Exception as e:
                    _LOGGER.debug("Error fetching engine status: %s", e)

        try:
            if self.electric and not self.uses_appsync:
                electric_status = await self._client.get_electric_status(
                    self.vin, region=self._region, generation=self.api_generation
                )
                if electric_status:
                    self._parse_electric_status(electric_status)
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error parsing electric status: %s", e)

        try:
            await self.update_climate()
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching climate settings: %s", e)

        try:
            await self.update_tire_pressure()
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching tire pressure: %s", e)

        try:
            await self.update_climate_schedules()
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching climate schedules: %s", e)

        try:
            await self.update_health()
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error fetching vehicle health: %s", e)

    async def poll_vehicle_status(self) -> bool:
        """Read cached status for this vehicle, without waking it."""
        status = await fetch_vehicle_status(
            self._client,
            self._transport,
            vin=self.vin,
            generation=self.api_generation,
            region=self.region,
            brand=self.brand,
            backdoor_type=self.backdoor_type,
        )
        if status:
            if self.uses_appsync:
                self.apply_graphql_status(status)
            else:
                self._parse_vehicle_status(status)
        return bool(status)

    async def poll_engine_status(self) -> bool | None:
        """Read REST engine state without waking the vehicle or replacing a push."""
        if self._transport.engine_transport is None:
            return None
        previous = self._features.get(VehicleFeatures.RemoteStartStatus)
        status = await fetch_engine_status(
            self._client,
            self._transport,
            vin=self.vin,
            generation=self.api_generation,
            region=self.region,
            brand=self.brand,
        )
        if status and self._features.get(VehicleFeatures.RemoteStartStatus) is previous:
            self._parse_engine_status(status)
        current = self._features.get(VehicleFeatures.RemoteStartStatus)
        return current.on if current is not None and current is not previous else None

    async def poll_vehicle_refresh(self) -> None:
        """Instruct Toyota's cloud to wake the vehicle and upload fresh status."""
        if not self.supports_command(RemoteRequestCommand.Refresh):
            raise ValueError("Vehicle refresh is unavailable for this vehicle.")
        errors = []
        refreshed = False

        for kind in self._transport.refresh_transports:
            if kind == "graphql":
                try:
                    guid = await self._client.auth.get_guid()
                    await self._client.graphql_pre_wake(guid, self._region)
                except AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
                    _LOGGER.debug("GraphQL pre-wake failed: %s", e)

                try:
                    await self._client.graphql_confirm_subscription(
                        self._vin,
                        self._backdoor_type,
                        self._region,
                    )
                except AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
                    _LOGGER.debug("GraphQL confirm subscription failed: %s", e)

                try:
                    await self._client.graphql_refresh_status(self._vin, self._region)
                    refreshed = True
                except AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
                    _LOGGER.debug("GraphQL refresh status failed: %s", e)
            else:
                try:
                    if kind == "legacy":
                        await self._client.send_refresh_request_17cy(self._vin, self._region)
                    elif kind == "route_21mm":
                        await self._client.send_refresh_request_21mm(self._vin, self._region)
                    elif kind == "route":
                        await self._client.send_refresh_request_route(
                            self.vin, self.api_generation, self.region, self.brand
                        )
                    else:
                        await self._client.send_refresh_request_17cyplus(self._vin, self._region)
                    refreshed = True
                except AuthError:
                    raise
                except Exception as e:
                    errors.append(e)
                    _LOGGER.debug("REST refresh request failed: %s", e)

        if not refreshed:
            if errors:
                raise errors[-1]
            raise RuntimeError(f"No refresh transport is available for {self.api_generation}.")

        try:
            if self.electric and not self.uses_appsync:
                electric_status = await self._client.get_electric_realtime_status(
                    self.vin,
                    self.api_generation,
                    self._region,
                )
                if electric_status:
                    self._parse_electric_status(electric_status)
        except AuthError:
            raise
        except Exception as e:
            _LOGGER.debug("Error refreshing electric status: %s", e)

    async def send_command(self, command: RemoteRequestCommand) -> None:
        """Send a generation-appropriate remote command."""
        if command == RemoteRequestCommand.ExtendRuntime and self.supports_command(command):
            status = await self._client.graphql_get_vehicle_status(
                self.vin, self.backdoor_type, self.region
            )
            if not status:
                raise RuntimeError("Toyota did not return the current remote-start session.")
            self.apply_graphql_status(status)
            if not can_extend_remote_runtime(
                (status.get("vehicleState") or {}).get("engine") or {}
            ):
                raise ValueError("Runtime extension is unavailable for this session.")
        if not self.supports_command(command):
            raise ValueError("This command is unavailable for this vehicle.")
        if command in self._EXTENDED_COMMANDS:
            await self.send_extended_command(command)
            return
        if command in (
            RemoteRequestCommand.ChargeStart,
            RemoteRequestCommand.ChargeResume,
            RemoteRequestCommand.ChargeStop,
            RemoteRequestCommand.PowerSupplyStop,
        ):
            await self.send_charging_command(command)
            return

        kind = self._transport.command_transport
        command_name = self._command_map[command]
        if kind == "legacy":
            await self._client.remote_request_17cy(
                self._vin,
                command_name,
                _LEGACY_COMMAND_VALUE_MAP[command],
                self._region,
            )
        elif kind == "graphql":
            await self._client.remote_request_24mm(self._vin, command_name, self._region)
        elif kind == "route_21mm":
            await self._client.remote_request_21mm(self._vin, command_name, self._region)
        elif kind == "route":
            await self._client.remote_request_route(
                self.vin, self.api_generation, command_name, self.region, self.brand
            )
        else:
            await self._client.remote_request_17cyplus(self._vin, command_name, self._region)

    def _accept_observation(self, key: tuple, observed_at: datetime | None) -> bool:
        """Return whether an observation at observed_at should replace key, recording it if so."""
        previous = self._feature_timestamps.get(key)
        if previous is not None and (observed_at is None or observed_at < previous):
            return False
        if observed_at is not None:
            self._feature_timestamps[key] = observed_at
        return True

    def _store_opening(
        self, feature, closed, locked, observed_at=None, *, from_status=False
    ) -> bool:
        """Merge known opening state without converting missing values to false."""
        if closed is None and locked is None:
            return False
        current = self._features.get(feature)
        current_closed = current.closed if isinstance(current, Opening) else None
        current_locked = current.locked if isinstance(current, Opening) else None

        if closed is not None and self._accept_observation((feature, "closed"), observed_at):
            merged_closed = closed
            if from_status:
                self._status_openings.add(feature)
        else:
            merged_closed = current_closed

        if locked is not None and self._accept_observation((feature, "locked"), observed_at):
            merged_locked = locked
        else:
            merged_locked = current_locked

        if merged_closed is None and merged_locked is None:
            return False
        self._features[feature] = Opening(closed=merged_closed, locked=merged_locked)
        return True

    def _store_numeric(self, feature, value, unit="", observed_at=None) -> bool:
        """Store a numeric reading unless a newer observation already exists."""
        if value is None:
            return False
        if not self._accept_observation((feature, "value"), observed_at):
            return False
        current = self._features.get(feature)
        if not unit and isinstance(current, Measurement) and current.unit:
            # A unitless reading would otherwise publish as whatever unit HA last displayed.
            unit = current.unit
        if value in (65535, "65535") and feature in (
            VehicleFeatures.RemainingChargeTime,
            VehicleFeatures.RemainingChargeTimeTo80,
            VehicleFeatures.BatteryPowerSupplyTime,
            VehicleFeatures.GasolinePowerSupplyTime,
        ):
            value = None
        self._features[feature] = Measurement(value, unit)
        return True

    def _store_location(self, feature, latitude, longitude, observed_at=None) -> bool:
        """Store a location unless a newer observation already exists."""
        if latitude is None or longitude is None:
            return False
        if not self._accept_observation((feature, "location"), observed_at):
            return False
        self._features[feature] = Location(latitude, longitude)
        return True

    def _store_remote_start(
        self,
        running,
        observed_at=None,
        *,
        start_time=None,
        stop_time=None,
        timer=None,
    ) -> bool:
        """Store engine state unless a newer observation already exists."""
        running = normalize_engine_state(running)
        if running is None:
            return False
        if not self._accept_observation(
            (VehicleFeatures.RemoteStartStatus, "running"), observed_at
        ):
            return False
        if timer is None and start_time is not None and stop_time is not None:
            timer = (stop_time - start_time).total_seconds() / 60
        self._features[VehicleFeatures.RemoteStartStatus] = RemoteStart(
            on=running,
            timer=timer,
            start_time=start_time,
        )
        return True

    def _parse_engine_status(self, engine_status: dict) -> None:
        if not engine_status or "status" not in engine_status:
            return
        running = normalize_engine_state(engine_status["status"])
        if running is None:
            return
        # REST engine-status "date" is the engine start time, not the status observation time.
        self._features[VehicleFeatures.RemoteStartStatus] = RemoteStart(
            on=running,
            timer=engine_status.get("timer"),
            start_time=parse_api_timestamp(engine_status.get("date")),
        )

    def _parse_electric_status(self, electric_status: dict) -> None:
        if not electric_status:
            return
        vehicle_info = electric_status.get("vehicleInfo") or {}
        observed_at = parse_api_timestamp(vehicle_info.get("acquisitionDatetime"))
        self._store_charge_schedules(
            vehicle_info.get("timerChargeInfo"),
            parse_api_timestamp(
                vehicle_info.get(
                    "_schedule_acquisition_datetime", vehicle_info.get("acquisitionDatetime")
                )
            ),
        )
        if isinstance(vehicle_info.get("maxNoOfChargeSchedules"), int):
            self._charge_settings["maxNoOfChargeSchedules"] = vehicle_info["maxNoOfChargeSchedules"]
        charge_info = vehicle_info.get("chargeInfo") or {}
        if not charge_info:
            return

        self._store_numeric(
            VehicleFeatures.ChargingState, charge_info.get("plugStatus"), "", observed_at
        )
        distance_unit = charge_info.get("evDistanceUnit") or "mi"
        for key, feature, unit in (
            ("evDistance", VehicleFeatures.ChargeDistance, distance_unit),
            ("evDistanceAC", VehicleFeatures.ChargeDistanceAC, distance_unit),
            ("chargeRemainingAmount", VehicleFeatures.ChargeLevel, "%"),
            ("plugStatus", VehicleFeatures.PlugStatus, ""),
            ("remainingChargeTime", VehicleFeatures.RemainingChargeTime, "min"),
            ("gasolineTravelableDistance", VehicleFeatures.GasolineRange, distance_unit),
            ("chargeType", VehicleFeatures.ChargeType, ""),
            ("connectorStatus", VehicleFeatures.ConnectorStatus, ""),
        ):
            self._store_numeric(feature, charge_info.get(key), unit, observed_at)
        charging = normalize_charging_state(charge_info.get("plugStatus"))
        if charging is not None:
            self._store_opening(VehicleFeatures.ChargingStatus, not charging, None, observed_at)

    def _parse_vehicle_status(self, vehicle_status: dict) -> None:
        if not vehicle_status:
            return

        observed_at = parse_api_timestamp(
            vehicle_status.get("occurrenceDate") or vehicle_status.get("occuranceDate")
        )
        if "latitude" in vehicle_status and "longitude" in vehicle_status:
            self._store_location(
                VehicleFeatures.ParkingLocation,
                vehicle_status["latitude"],
                vehicle_status["longitude"],
                parse_api_timestamp(vehicle_status.get("locationAcquisitionDatetime"))
                or observed_at,
            )

        categories = vehicle_status.get("vehicleStatus")
        if not categories:
            return

        for feature, (closed, locked) in self._rest_openings(categories).items():
            self._store_opening(feature, closed, locked, observed_at, from_status=True)

    def apply_graphql_status(self, status: dict) -> bool:
        """Apply a pushed or polled AppSync status to this vehicle."""
        if not isinstance(status, dict):
            return False
        sections = {
            key: value if isinstance(value := status.get(key), dict) else {}
            for key in (
                "vehicleState",
                "location",
                "telemetry",
                "tripdetails",
                "electric",
            )
        }
        if not any(sections.values()):
            return False
        status = {**status, **sections}
        self._parse_graphql_vehicle_status(status)
        return True

    def _parse_graphql_vehicle_status(self, status: dict) -> None:
        """Parse GraphQL GetVehicleStatus response into vehicle features."""
        if not status:
            return

        sections = [status] + [
            status.get(key) or {}
            for key in ("vehicleState", "telemetry", "location", "electric", "tripdetails")
        ]
        electric = status.get("electric") or {}
        sections.extend(
            value
            for key in ("battery", "gasoline", "charging")
            if isinstance(value := electric.get(key), dict)
        )
        for section in sections:
            updated_at = parse_api_timestamp(section.get("lastUpdateDateTime"))
            if updated_at is not None:
                self._store_numeric(
                    VehicleFeatures.LastTimeStamp,
                    updated_at.timestamp(),
                    observed_at=updated_at,
                )

        location = status.get("location")
        if location:
            self._store_location(
                VehicleFeatures.ParkingLocation,
                location.get("latitude"),
                location.get("longitude"),
                parse_api_timestamp(
                    location.get("lastUpdateDateTime") or status.get("lastUpdateDateTime")
                ),
            )

        vehicle_state = status.get("vehicleState")
        if vehicle_state:
            observed_at = parse_api_timestamp(
                vehicle_state.get("lastUpdateDateTime") or status.get("lastUpdateDateTime")
            )

            doors = vehicle_state.get("doors")
            if doors:
                for door_key, feature in self._graphql_door_map.items():
                    door = doors.get(door_key)
                    if door:
                        closed, locked = opening_state_from_graphql(door)
                        self._store_opening(feature, closed, locked, observed_at, from_status=True)

            windows = vehicle_state.get("windows")
            if windows:
                for win_key, feature in self._graphql_window_map.items():
                    window = windows.get(win_key)
                    if window:
                        closed, _ = opening_state_from_graphql(window)
                        self._store_opening(feature, closed, None, observed_at, from_status=True)

            tires = vehicle_state.get("tires") or {}
            tire_observed_at = parse_api_timestamp(
                tires.get("lastUpdateDateTime")
                or vehicle_state.get("lastUpdateDateTime")
                or status.get("lastUpdateDateTime")
            )
            if tires and tire_observed_at is not None:
                self._store_numeric(
                    VehicleFeatures.LastTirePressureTimeStamp,
                    tire_observed_at.timestamp(),
                    observed_at=tire_observed_at,
                )
            for tire_key, feature in self._graphql_tire_map.items():
                tire = tires.get(tire_key) or {}
                warning = tire.get("displayLowTirePressureWarning")
                if isinstance(warning, bool):
                    self._store_opening(
                        self._graphql_tire_warning_map[tire_key],
                        not warning,
                        None,
                        tire_observed_at,
                    )
                for pressure_key, default_unit in (
                    ("psi", "psi"),
                    ("kpa", "kPa"),
                    ("bar", "bar"),
                ):
                    pressure = tire.get(pressure_key)
                    if pressure is None:
                        continue
                    if isinstance(pressure, dict):
                        value = pressure.get("value")
                        unit = pressure.get("unit") or default_unit
                    else:
                        value = pressure
                        unit = default_unit
                    if self._store_numeric(feature, value, unit, tire_observed_at):
                        break

            for opening_key in backdoor_candidates(self._backdoor_type):
                opening = vehicle_state.get(opening_key)
                if opening:
                    closed, locked = opening_state_from_graphql(opening)
                    if self._store_opening(
                        VehicleFeatures.Trunk, closed, locked, observed_at, from_status=True
                    ):
                        break

            for key, feature in (
                ("glassHatch", VehicleFeatures.GlassHatch),
                ("hood", VehicleFeatures.Hood),
                ("moonroof", VehicleFeatures.Moonroof),
            ):
                opening = vehicle_state.get(key)
                if opening:
                    closed, _ = opening_state_from_graphql(opening)
                    self._store_opening(feature, closed, None, observed_at, from_status=True)

            engine = vehicle_state.get("engine")
            if engine:
                engine_observed_at = parse_api_timestamp(
                    engine.get("lastUpdateDateTime")
                    or vehicle_state.get("lastUpdateDateTime")
                    or status.get("lastUpdateDateTime")
                )
                if self._accept_observation(("engine_details", "value"), engine_observed_at):
                    self._engine_details.update(engine)
                self._store_remote_start(
                    engine.get("running", engine.get("status")),
                    engine_observed_at,
                    start_time=parse_api_timestamp(engine.get("startTime")),
                    stop_time=parse_api_timestamp(engine.get("stopTime")),
                )

        telemetry = status.get("telemetry")
        if telemetry:
            telemetry_observed_at = parse_api_timestamp(
                telemetry.get("lastUpdateDateTime") or status.get("lastUpdateDateTime")
            )
            for key, feature, default_unit in (
                ("odo", VehicleFeatures.Odometer, ""),
                ("fugage", VehicleFeatures.FuelLevel, "%"),
                ("range", VehicleFeatures.DistanceToEmpty, ""),
                ("totalAverageFuelConsumption", VehicleFeatures.AverageFuelConsumption, ""),
                ("averageFuelConsumptionSinceStart", VehicleFeatures.TripFuelConsumption, ""),
            ):
                measurement = telemetry.get(key) or {}
                self._store_numeric(
                    feature,
                    measurement.get("value"),
                    measurement.get("unit") or default_unit,
                    telemetry_observed_at,
                )

        trip_details = status.get("tripdetails") or {}
        trip_observed_at = parse_api_timestamp(
            trip_details.get("lastUpdateDateTime") or status.get("lastUpdateDateTime")
        )
        for key, feature in (
            ("tripA", VehicleFeatures.TripDetailsA),
            ("tripB", VehicleFeatures.TripDetailsB),
            ("tripCount", VehicleFeatures.TripCount),
        ):
            trip = trip_details.get(key) or {}
            self._store_numeric(
                feature,
                trip.get("value"),
                trip.get("unit") or "",
                trip_observed_at,
            )

        self._parse_graphql_electric_status(
            status.get("electric"),
            parse_api_timestamp(status.get("lastUpdateDateTime")),
        )

    def _parse_graphql_electric_status(self, electric: dict, observed_at=None) -> None:
        """Parse the AppSync electric document returned for EVs and PHEVs."""
        if not electric:
            return

        observed_at = parse_api_timestamp(electric.get("lastUpdateDateTime")) or observed_at
        battery = electric.get("battery") or {}
        battery_observed_at = parse_api_timestamp(battery.get("lastUpdateDateTime")) or observed_at
        charge_level = None
        for key in (
            "stateOfChargeDisplay",
            "plugInEnergy",
            "chargeRemainingAmount",
        ):
            measurement = battery.get(key)
            if measurement and measurement.get("value") is not None:
                charge_level = measurement
                break
        if charge_level:
            self._store_numeric(
                VehicleFeatures.ChargeLevel,
                charge_level.get("value"),
                charge_level.get("unit") or "%",
                battery_observed_at,
            )

        electric_range = battery.get("travelableDistance") or {}
        self._store_numeric(
            VehicleFeatures.ChargeDistance,
            electric_range.get("value"),
            electric_range.get("unit") or "",
            battery_observed_at,
        )

        electric_range_ac = battery.get("travelableDistanceAC") or {}
        self._store_numeric(
            VehicleFeatures.ChargeDistanceAC,
            electric_range_ac.get("value"),
            electric_range_ac.get("unit") or "",
            battery_observed_at,
        )

        gasoline = electric.get("gasoline") or {}
        for source, key, feature in (
            (battery, "powerSupplyPossibleTime", VehicleFeatures.BatteryPowerSupplyTime),
            (gasoline, "powerSupplyPossibleTime", VehicleFeatures.GasolinePowerSupplyTime),
            (gasoline, "travelableDistance", VehicleFeatures.GasolineRange),
        ):
            measurement = source.get(key) or {}
            self._store_numeric(
                feature,
                measurement.get("value"),
                measurement.get("unit") or "",
                parse_api_timestamp(source.get("lastUpdateDateTime")) or observed_at,
            )

        charging = electric.get("charging") or {}
        if not charging:
            return
        charging_observed_at = (
            parse_api_timestamp(charging.get("lastUpdateDateTime")) or observed_at
        )

        self._store_numeric(
            VehicleFeatures.ChargingState,
            charging.get("chargingState"),
            "",
            charging_observed_at,
        )

        self._store_numeric(
            VehicleFeatures.ChargeType,
            charging.get("chargeType"),
            observed_at=charging_observed_at,
        )
        for key, feature in (
            ("remainingChargeTime", VehicleFeatures.RemainingChargeTime),
            ("actualChargingRate", VehicleFeatures.ChargingRate),
            ("remainingChargeTimeTo80Percent", VehicleFeatures.RemainingChargeTimeTo80),
        ):
            measurement = charging.get(key) or {}
            self._store_numeric(
                feature,
                measurement.get("value"),
                measurement.get("unit") or "",
                charging_observed_at,
            )
        settings = charging.get("chargeSettings") or {}
        settings_observed_at = (
            parse_api_timestamp(settings.get("lastUpdateDateTime")) or charging_observed_at
        )
        self._store_charge_schedules(settings.get("schedules"), settings_observed_at)
        for key, value in {
            **settings,
            "limitSelectionValues": charging.get("limitSelectionValues"),
        }.items():
            if key == "schedules" or value is None:
                continue
            if self._accept_observation(("charge_settings", key), settings_observed_at):
                self._charge_settings[key] = deepcopy(value)
        target = settings.get("targetLimit") or {}
        self._store_numeric(
            VehicleFeatures.ChargeTargetLimit,
            target.get("value"),
            target.get("unit") or "%",
            settings_observed_at,
        )

        connector = charging.get("connector") or {}
        self._store_numeric(
            VehicleFeatures.ConnectorStatus,
            connector.get("status"),
            observed_at=charging_observed_at,
        )
        plug_status = (
            connector.get("plugStatus")
            or connector.get("plugInInfo")
            or charging.get("chargingState")
        )
        self._store_numeric(
            VehicleFeatures.PlugStatus,
            plug_status,
            observed_at=charging_observed_at,
        )

        is_charging = normalize_charging_state(charging.get("chargingState"))
        if is_charging is None and not charging.get("chargingState"):
            is_charging = normalize_charging_state(charging.get("chargingStatus") or plug_status)
        if is_charging is not None:
            self._store_opening(
                VehicleFeatures.ChargingStatus,
                closed=not is_charging,
                locked=None,
                observed_at=charging_observed_at,
            )

    def _parse_telemetry(self, telemetry: dict) -> None:
        if not telemetry:
            return

        observed_at = parse_api_timestamp(telemetry.get("lastTimestamp"))
        tire_observed_at = parse_api_timestamp(telemetry.get("tirePressureTimestamp"))
        if observed_at is not None:
            self._store_numeric(
                VehicleFeatures.LastTimeStamp,
                observed_at.timestamp(),
                observed_at=observed_at,
            )

        for key, value in telemetry.items():
            if value is None or key == "lastTimestamp":
                continue

            if key == "tirePressureTimestamp":
                if tire_observed_at is not None:
                    self._store_numeric(
                        VehicleFeatures.LastTirePressureTimeStamp,
                        tire_observed_at.timestamp(),
                        observed_at=tire_observed_at,
                    )
                continue

            if key == "fuelLevel":
                self._store_numeric(VehicleFeatures.FuelLevel, value, "%", observed_at)
                continue

            # Toyota labels telemetry vehicleLocation as Last Parked. It is
            # also the only location available on some accounts, so it backs
            # both location entities.
            if key == "vehicleLocation" and isinstance(value, dict):
                latitude = value.get("latitude")
                longitude = value.get("longitude")
                self._store_location(
                    VehicleFeatures.RealTimeLocation, latitude, longitude, observed_at
                )
                self._store_location(
                    VehicleFeatures.ParkingLocation, latitude, longitude, observed_at
                )
                continue

            if key in self._TELEMETRY_OPENINGS:
                feature = self._TELEMETRY_OPENINGS[key]
                if (
                    self._window_telemetry
                    and app_int(value) in (1, 2)
                    and feature not in self._status_openings
                ):
                    # Only fill openings that have never been reported by vehicle status.
                    # The app reads rear windows as text and the others as numbers.
                    self._store_opening(feature, closed=(app_int(value) == 2), locked=None)
                continue

            feature = self._vehicle_telemetry_map.get(key)
            if feature is not None:
                feature_observed_at = observed_at
                if key.endswith("TirePressure") and tire_observed_at is not None:
                    feature_observed_at = tire_observed_at
                if isinstance(value, dict) and "value" in value:
                    self._store_numeric(
                        feature,
                        value["value"],
                        value.get("unit") or "",
                        feature_observed_at,
                    )
                else:
                    self._store_numeric(feature, value, observed_at=feature_observed_at)

    def _rest_openings(self, categories) -> dict:
        """Opening states from REST vehicle status, matched like Toyota's app."""
        category_map = {
            key.lower(): feature for key, feature in self._vehicle_status_category_map.items()
        }
        openings, named = {}, set()
        for category in categories:
            if not isinstance(category, dict) or not isinstance(category.get("sections"), list):
                continue
            category_name = str(category.get("category")).lower()
            for section in category["sections"]:
                if not isinstance(section, dict):
                    continue
                section_name = str(section.get("section")).lower()
                feature = category_map.get(f"{category_name} {section_name}")
                any_category = self._REST_ANY_CATEGORY_SECTIONS.get(section_name)
                if feature is None and any_category is None:
                    continue
                values = section.get("values")
                # A malformed section must not cost the rest of the response its update.
                values = (
                    [value for value in values if isinstance(value, dict)]
                    if isinstance(values, list)
                    else []
                )
                closed, locked = opening_state_from_values(values)
                if any_category is not None:
                    # The app reads open state from the first section with each
                    # name, in any category, and lock state only under Other.
                    feature = any_category
                    if section_name in named:
                        closed = None
                    named.add(section_name)
                    if category_name != "other":
                        locked = None
                if feature == VehicleFeatures.GlassHatch:
                    locked = None
                if feature in openings:
                    closed, locked = merge_opening_states(openings[feature], (closed, locked))
                openings[feature] = (closed, locked)
        return openings

    @property
    def health(self) -> dict:
        """Toyota's latest vehicle health responses, kept between polls."""
        return self._health

    @property
    def notifications(self):
        """This vehicle's items from Toyota's notification history, None until read."""
        return self._notifications

    @notifications.setter
    def notifications(self, items) -> None:
        self._notifications = items

    async def update_health(self) -> None:
        """Read vehicle health behind the app's feature gates.

        Each response is read every poll until it answers, then hourly.
        """
        now = time.monotonic()
        read_at = self._health.setdefault("read_at", {})
        reads = (
            (
                "report",
                dict,
                ("vehicleHealthReport", "scheduleMaintenance", "safetyRecall", "serviceCampaign"),
                lambda: self._client.get_vehicle_health_report(
                    self.vin, self.api_generation, self.region, self.brand
                ),
            ),
            (
                "status",
                dict,
                ("vehicleDiagnostic",),
                lambda: self._client.get_vehicle_health_status(
                    self.vin, self.api_generation, self.region, self.brand
                ),
            ),
            (
                "campaigns",
                list,
                ("safetyRecall", "serviceCampaign"),
                lambda: self._client.get_service_campaigns(self.vin),
            ),
            # The app checks for software updates behind the autoDrive flag.
            ("software", dict, ("autoDrive",), lambda: self._client.get_software_update(self.vin)),
        )
        for key, shape, features, read in reads:
            if key in read_at and now - read_at[key] < HEALTH_READ_INTERVAL:
                continue
            if not any(self.feature_enabled(feature) for feature in features):
                continue
            # Each response keeps its last good value when a read fails.
            try:
                response = await read()
            except AuthError:
                raise
            except Exception as e:
                _LOGGER.debug("Error fetching vehicle health %s: %s", key, e)
                continue
            # An empty report is a failed read; an empty campaign list means none.
            if isinstance(response, shape) and (response or shape is list):
                self._health[key] = response
                read_at[key] = now

    @property
    def tire_pressure(self):
        """Toyota's latest tire pressure response, None where the endpoint is not read."""
        return self._tire_pressure

    async def update_tire_pressure(self) -> None:
        # Toyota's app reads this endpoint only when tire pressure is enabled.
        if (
            self.uses_appsync
            or not self.can_receive_status
            or not self.feature_enabled("tirePressure")
        ):
            return
        status = await self._client.get_tire_pressure(
            self.vin,
            self.api_generation,
            self.region,
            self.brand,
        )
        self._tire_pressure = status
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
                    self._vehicle_telemetry_map[key],
                    value,
                    tire.get("unit") or "psi",
                    observed_at,
                )
        if observed_at is not None:
            self._store_numeric(
                VehicleFeatures.LastTirePressureTimeStamp,
                observed_at.timestamp(),
                observed_at=observed_at,
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
    def charge_settings(self) -> dict:
        return self._charge_settings

    def supports_charge_setting(self, field: str) -> bool:
        feature = "powerSupply" if field == "electricSupplyModeLimit" else "chargeSetting"
        return (
            field in CHARGE_SETTINGS
            and self.uses_appsync
            and self.electric
            and self.subscribed
            and not self.stolen
            and self.feature_enabled(feature)
        )

    @property
    def supports_charge_schedules(self) -> bool:
        return (
            self.electric
            and self.subscribed
            and not self.stolen
            and self.feature_enabled("multiDayCharging")
            and (self.uses_appsync or (self._feature_flags or {}).get("multiDayCharging") == 1)
            and isinstance(self.charge_settings.get("schedules"), list)
        )

    def _store_charge_schedules(self, schedules, observed_at) -> None:
        # A partial GraphQL result lists null schedules; it must not replace
        # the ones last read or pass as an empty list.
        if not isinstance(schedules, list) or any(not isinstance(item, dict) for item in schedules):
            return
        previous = self._charge_settings.get("_schedules_updated_at")
        if previous is not None and (observed_at is None or observed_at < previous):
            return
        self._charge_settings["schedules"] = schedules
        self._charge_settings["_schedules_updated_at"] = observed_at
        self._features[VehicleFeatures.ChargeScheduleCount] = Measurement(len(schedules), "")

    async def _read_charge_schedules(self):
        if self.uses_appsync:
            status = await self._client.graphql_get_vehicle_status(
                self.vin, self.backdoor_type, self.region
            )
            self.apply_graphql_status(status)
            electric = (status or {}).get("electric") or {}
            charging = electric.get("charging") or {}
            schedules = (charging.get("chargeSettings") or {}).get("schedules")
        else:
            status = await self._client.get_electric_status(
                self.vin, region=self.region, generation=self.api_generation
            )
            self._parse_electric_status(status)
            schedules = ((status or {}).get("vehicleInfo") or {}).get("timerChargeInfo")
        if not isinstance(schedules, list) or any(not isinstance(item, dict) for item in schedules):
            raise RuntimeError("Toyota did not return current charge schedules.")
        # A response older than the last observation is not stored, so use
        # what is shown rather than confirm a change from a stale one.
        return self._charge_settings["schedules"]

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
            unidentified = sum(
                isinstance(item, dict) and item.get("settingId") is None for item in schedules
            )
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
                self.vin,
                self.api_generation,
                body,
                self.region,
                self.brand,
                delete=delete,
            )

            def confirmed(schedules):
                candidates = [item for item in schedules if isinstance(item, dict)]
                if identifier is None:
                    candidates = [
                        item
                        for item in candidates
                        if str(item.get("settingId")) not in previous_ids
                    ]
                else:
                    candidates = [
                        item for item in candidates if str(item.get("settingId")) == str(identifier)
                    ]
                # A schedule that newly lacks an ID may be the deleted one.
                if delete:
                    return (
                        not candidates
                        and sum(
                            isinstance(item, dict) and item.get("settingId") is None
                            for item in schedules
                        )
                        <= unidentified
                    )
                return any(schedule_matches(item, body) for item in candidates)

            await self._wait_for_schedules(
                self._read_charge_schedules,
                confirmed,
                "Toyota accepted the schedule change but did not return the updated schedule.",
            )

    async def _wait_for_schedules(self, read, confirmed, message):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + SCHEDULE_UPDATE_TIMEOUT
        readback_delay = 5
        error = None
        while loop.time() < deadline:
            try:
                schedules = await asyncio.wait_for(read(), deadline - loop.time())
            except AuthError:
                raise
            except aiohttp.ClientResponseError as err:
                if err.status in (401, 403):
                    raise
                error = err
            except Exception as err:
                # A failed read can hide a saved change, so keep reading rather
                # than report a failure that invites a duplicate.
                error = err
            else:
                if confirmed(schedules):
                    return
                error = None
            await asyncio.sleep(min(readback_delay, max(0, deadline - loop.time())))
            readback_delay = min(30, readback_delay * 2)
        if error is not None and not isinstance(error, asyncio.TimeoutError):
            raise error
        raise RuntimeError(message)

    async def set_charge_setting(self, field, option):
        if not self.supports_charge_setting(field):
            raise ValueError("Charging preferences are unavailable for this vehicle.")
        status = await self._client.graphql_get_vehicle_status(
            self.vin, self.backdoor_type, self.region
        )
        if not status:
            raise RuntimeError("Toyota did not return charging preferences.")
        self.apply_graphql_status(status)
        charging = (status.get("electric") or {}).get("charging") or {}
        options = charge_options(
            {
                **(charging.get("chargeSettings") or {}),
                "limitSelectionValues": charging.get("limitSelectionValues"),
            },
            field,
            allow_missing_target=bool(charging)
            and self.feature_enabled("chargeSetting", default=False),
        )
        if option not in options:
            raise ValueError("This charging option is unavailable for this vehicle.")
        await self._client.update_charge_settings(
            self.vin,
            CHARGE_SETTINGS[field][3],
            options[option],
            self.region,
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
                self.vin,
                self.api_generation,
                command_name,
                self.region,
                self.brand,
            )

    def supports_command(self, command: RemoteRequestCommand) -> bool:
        """Return whether the API transport and vehicle support a command."""
        if self.stolen:
            return False
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
                first_capability(self._remote_capabilities, self._extended_capabilities, (key,))
                is True
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
                RemoteRequestCommand.PowerSupplyStop: (
                    "external_power_active",
                    "external_power_active_hybrid",
                ),
            }
            state = self.features.get(VehicleFeatures.ChargingState)
            state = str(state.value).lower() if state is not None else None
            return state in states[command] and (
                command == RemoteRequestCommand.ChargeStart or self.uses_appsync
            )
        if command not in self._command_map:
            return False

        if (
            command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.EngineStop)
            and self.can_start_climate
        ):
            return True

        keys = self._COMMAND_CAPABILITIES.get(command)
        if keys is None:
            return True
        supported = first_capability(
            self._remote_capabilities,
            self._extended_capabilities,
            keys,
        )
        if (
            command in self._COMMANDS_REQUIRING_EXPLICIT_CAPABILITY
            or self._transport.explicit_command_capabilities
        ):
            return supported is True
        return supported is not False

    @property
    def vin(self) -> str:
        return self._vin

    def __repr__(self) -> str:
        lines = "".join(f"       {key}={value}\n" for key, value in self._features.items())
        return f"{self.__class__.__name__}(\n    features=(\n{lines}  )\n)"
