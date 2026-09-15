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

from .vehicle_helpers import endpoint_generation, first_capability, is_appsync_generation
from .climate_helpers import apply_climate_changes
from .charging_helpers import CHARGE_SETTINGS, charge_options


@unique
class ApiVehicleGeneration(Enum):
    CY17 = "17CY"
    CY17PLUS = "17CYPLUS"
    MM21 = "21MM"
    MM24 = "24MM"
    BEV26 = "26BEV"
    NG86 = "GR86"
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
        self._charge_settings = {}

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

    def feature_enabled(self, name: str) -> bool:
        """Older vehicle payloads omit the entire feature-state model."""
        if self._feature_flags is None:
            return True
        value = self._feature_flags.get(name)
        return type(value) is int and value == 1

    @property
    def can_receive_status(self) -> bool:
        return self.subscribed or (self.uses_appsync and self.electric)

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
                raise ValueError("Toyota did not return climate settings.")
            settings = apply_climate_changes(settings, changes)
            await self._client.update_climate_settings(
                self.vin, self.api_generation, settings, self.region, self.brand
            )
            self._climate_settings.clear()
            self._climate_settings.update(settings)

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

    @property
    def supports_charge_settings(self):
        return self.uses_appsync and self.electric and self.subscribed and self.feature_enabled("remoteCommands")

    async def set_charge_setting(self, field, option):
        if not self.supports_charge_settings:
            raise ValueError("Charging preferences are unavailable for this vehicle.")
        status = await self._client.graphql_get_vehicle_status(self.vin, self.backdoor_type, self.region)
        if not status:
            raise ValueError("Toyota did not return charging preferences.")
        self.apply_graphql_status(status)
        options = charge_options(self.charge_settings, field)
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
        if command in self._COMMANDS_REQUIRING_EXPLICIT_CAPABILITY:
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
        self._charge_settings = previous.charge_settings
        return True

    @property
    def vin(self):
        return self._vin

    def __repr__(self):
        str = f"{self.__class__.__name__}(\n    features=(\n"
        for key, value in self._features.items():
            str += f"       {key}={value}\n"
        return f"{str}  )\n)"
