import logging
from typing import Optional

from toyota_na.client import ToyotaOneClient
from toyota_na.vehicle.base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    ToyotaVehicle,
    VehicleFeatures,
)
from toyota_na.vehicle.entity_types.ToyotaLocation import ToyotaLocation
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric
from toyota_na.vehicle.entity_types.ToyotaOpening import ToyotaOpening
from toyota_na.vehicle.entity_types.ToyotaRemoteStart import ToyotaRemoteStart

from .vehicle_helpers import (
    backdoor_candidates,
    opening_state_from_graphql,
    opening_state_from_values,
    parse_api_timestamp,
)

_LOGGER = logging.getLogger(__name__)


class SeventeenCYPlusToyotaVehicle(ToyotaVehicle):

    _has_remote_subscription = False
    _has_electric = False
    _command_map = {
        RemoteRequestCommand.DoorLock: "door-lock",
        RemoteRequestCommand.DoorUnlock: "door-unlock",
        RemoteRequestCommand.EngineStart: "engine-start",
        RemoteRequestCommand.EngineStop: "engine-stop",
        RemoteRequestCommand.HazardsOn: "hazard-on",
        RemoteRequestCommand.HazardsOff: "hazard-off",
        RemoteRequestCommand.VehicleFinder: "find-vehicle",
        RemoteRequestCommand.Refresh: "refresh",
    }

    #  We'll parse these keys out in the parser by mapping the category and section types to a string literal
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
        "Other Moonroof": VehicleFeatures.Moonroof,
        "Other Hood": VehicleFeatures.Hood,
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
        "vehicleLocation": VehicleFeatures.ParkingLocation,
        "nextService": VehicleFeatures.NextService,
        "speed": VehicleFeatures.Speed,

        "driverWindow": VehicleFeatures.FrontDriverWindow,
        "passengerWindow": VehicleFeatures.FrontPassengerWindow,
        "rlWindow": VehicleFeatures.RearDriverWindow,
        "rrWindow": VehicleFeatures.RearPassengerWindow,
        "sunRoof": VehicleFeatures.Moonroof,
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
        backdoor_type: Optional[str] = None,
        remote_capabilities: Optional[dict] = None,
        extended_capabilities: Optional[dict] = None,
    ):
        self._has_remote_subscription = has_remote_subscription
        self._has_electric = has_electric

        ToyotaVehicle.__init__(
            self,
            client,
            has_remote_subscription,
            has_electric,
            model_name,
            model_year,
            vin,
            region,
            generation,
            brand,
            backdoor_type,
            remote_capabilities,
            extended_capabilities,
        )
        self._last_vehicle_status = None
        self._last_graphql_status = None
        self._feature_timestamps = {}

    async def update(self):

        try:
            telemetry = await self._client.get_telemetry(
                self._vin,
                self._region,
                self.endpoint_generation,
                self._brand,
            )
            if telemetry:
                self._parse_telemetry(telemetry)
        except Exception as e:
            _LOGGER.debug("Error fetching telemetry: %s", e)

        try:
            if self._has_remote_subscription:
                # Apply cached push data first. REST then wins when neither source
                # reports timestamps, while newer timestamped data wins either way.
                ws_handler = getattr(self._client, "_ws_handler", None)
                if ws_handler:
                    self.apply_graphql_status(
                        ws_handler.get_cached_status(self._vin)
                    )

                # REST v1/global/remote/status provides door/lock/window/hood/hatch
                vehicle_status = await self._client.get_vehicle_status_17cyplus(
                    self._vin, self._brand, self._region
                )
                if vehicle_status:
                    self._last_vehicle_status = vehicle_status
                    self._parse_vehicle_status(vehicle_status)
                elif self._last_vehicle_status:
                    self._parse_vehicle_status(self._last_vehicle_status)
        except Exception as e:
            _LOGGER.debug("Error fetching vehicle status: %s", e)

        try:
            if self._has_remote_subscription:
                # engine_status - use 17cyplus endpoint
                engine_status = await self._client.get_engine_status_17cyplus(
                    self._vin, self._brand, self._region
                )
                if engine_status:
                    _LOGGER.debug("Engine status received for VIN %s", self._vin[-4:])
                    self._parse_engine_status(engine_status)
                else:
                    _LOGGER.debug("Engine status returned None for VIN %s", self._vin[-4:])
        except Exception as e:
            _LOGGER.debug("Error fetching engine status: %s", e)

        try:
            if self._has_electric:
                # electric_status
                electric_status = await self._client.get_electric_status(
                    self.vin, brand=self._brand, region=self._region
                )
                if electric_status:
                    self._parse_electric_status(electric_status)
        except Exception as e:
            _LOGGER.debug("Error parsing electric status: %s", e)

    async def poll_vehicle_refresh(self) -> None:
        """Instructs Toyota's systems to ping the vehicle to upload a fresh status."""
        # GraphQL refresh flow: pre-wake -> confirm subscription -> refresh
        try:
            guid = await self._client.auth.get_guid()
            await self._client.graphql_pre_wake(guid, self._brand)
        except Exception as e:
            _LOGGER.debug("GraphQL pre-wake failed: %s", e)

        try:
            await self._client.graphql_confirm_subscription(
                self._vin, self._backdoor_type, self._brand
            )
        except Exception as e:
            _LOGGER.debug("GraphQL confirm subscription failed: %s", e)

        try:
            await self._client.graphql_refresh_status(self._vin, self._brand)
        except Exception as e:
            _LOGGER.debug("GraphQL refresh status failed: %s", e)

        # Also do REST refresh
        try:
            await self._client.send_refresh_request_17cyplus(
                self._vin, self._brand, self._region
            )
        except Exception as e:
            _LOGGER.debug("REST refresh request failed: %s", e)

        try:
            if self._has_electric:
                electric_status = await self._client.get_electric_realtime_status(
                    self.vin,
                    self.endpoint_generation,
                    self._brand,
                    self._region,
                )
                if electric_status:
                    self._parse_electric_status(electric_status)
        except Exception as e:
            _LOGGER.debug("Error refreshing electric status: %s", e)

    async def send_command(self, command: RemoteRequestCommand) -> None:
        """Start the engine. Periodically refreshes the vehicle status to determine if the engine is running."""
        await self._client.remote_request_17cyplus(
            self._vin, self._command_map[command], self._brand, self._region
        )

    #
    # engine_status
    #

    def _parse_engine_status(self, engine_status: dict) -> None:
        if not engine_status or "status" not in engine_status:
            return

        self._features[VehicleFeatures.RemoteStartStatus] = ToyotaRemoteStart(
            date=engine_status.get("date"),
            on=engine_status["status"] == "1",
            timer=engine_status.get("timer"),
        )
    
    #
    # electric_status
    #

    def _parse_electric_status(self, electric_status: dict) -> None:
        if not electric_status or "vehicleInfo" not in electric_status:
            return
        
        chargeInfo = electric_status["vehicleInfo"].get("chargeInfo", {})
        if not chargeInfo:
            return

        self._features[VehicleFeatures.ChargeDistance] = ToyotaNumeric(chargeInfo.get("evDistance"), chargeInfo.get("evDistanceUnit"))
        self._features[VehicleFeatures.ChargeDistanceAC] = ToyotaNumeric(chargeInfo.get("evDistanceAC"), chargeInfo.get("evDistanceUnit"))
        self._features[VehicleFeatures.ChargeLevel] = ToyotaNumeric(chargeInfo.get("chargeRemainingAmount"), "%")
        self._features[VehicleFeatures.PlugStatus] = ToyotaNumeric(chargeInfo.get("plugStatus"), "")
        self._features[VehicleFeatures.RemainingChargeTime] = ToyotaNumeric(chargeInfo.get("remainingChargeTime"), "")
        self._features[VehicleFeatures.EvTravelableDistance] = ToyotaNumeric(chargeInfo.get("evTravelableDistance"), "")
        self._features[VehicleFeatures.ChargeType] = ToyotaNumeric(chargeInfo.get("chargeType"), "")
        self._features[VehicleFeatures.ConnectorStatus] = ToyotaNumeric(chargeInfo.get("connectorStatus"), "")
        self._features[VehicleFeatures.ChargingStatus] = ToyotaOpening(chargeInfo.get("connectorStatus") != 5)

    def _store_opening(self, feature, closed, locked, observed_at=None) -> bool:
        """Merge known opening state without converting missing values to false."""
        current = self._features.get(feature)
        current_closed = current.closed if isinstance(current, ToyotaOpening) else None
        current_locked = (
            current.locked if isinstance(current, ToyotaLockableOpening) else None
        )

        def update_component(name, value, previous):
            if value is None:
                return previous
            timestamp = self._feature_timestamps.get((feature, name))
            if timestamp is not None and (
                observed_at is None or observed_at < timestamp
            ):
                return previous
            if observed_at is not None:
                self._feature_timestamps[(feature, name)] = observed_at
            return value

        closed = update_component("closed", closed, current_closed)
        locked = update_component("locked", locked, current_locked)
        if closed is None and locked is None:
            return False

        if locked is None:
            self._features[feature] = ToyotaOpening(closed=closed)
        else:
            self._features[feature] = ToyotaLockableOpening(
                closed=closed, locked=locked
            )
        return True

    def _parse_vehicle_status(self, vehicle_status: dict) -> None:
        if not vehicle_status:
            return

        if "latitude" in vehicle_status and "longitude" in vehicle_status:
            self._features[VehicleFeatures.ParkingLocation] = ToyotaLocation(
                vehicle_status["latitude"], vehicle_status["longitude"]
            )

        categories = vehicle_status.get("vehicleStatus")
        if not categories:
            return

        observed_at = parse_api_timestamp(
            vehicle_status.get("occurrenceDate")
            or vehicle_status.get("occuranceDate")
        )

        for category in categories:
            if not category or "sections" not in category:
                continue
            for section in category["sections"]:
                if not section:
                    continue

                category_type = category.get("category")
                section_type = section.get("section")

                key = f"{category_type} {section_type}"

                feature = self._vehicle_status_category_map.get(key)
                if feature is None:
                    continue
                closed, locked = opening_state_from_values(
                    section.get("values", [])
                )
                self._store_opening(feature, closed, locked, observed_at)

    #
    # GraphQL vehicle status parser
    #

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

    def apply_graphql_status(self, status: dict) -> bool:
        """Apply a pushed AppSync status to this vehicle."""
        if not status or not any(
            status.get(key) for key in ("vehicleState", "location", "telemetry")
        ):
            return False
        self._last_graphql_status = status
        self._parse_graphql_vehicle_status(status)
        return True

    def _parse_graphql_vehicle_status(self, status: dict) -> None:
        """Parse GraphQL GetVehicleStatus response into vehicle features."""
        if not status:
            return

        # Location
        location = status.get("location")
        if (
            location
            and location.get("latitude") is not None
            and location.get("longitude") is not None
        ):
            self._features[VehicleFeatures.ParkingLocation] = ToyotaLocation(
                location["latitude"], location["longitude"]
            )

        vehicle_state = status.get("vehicleState")
        if vehicle_state:
            observed_at = parse_api_timestamp(
                vehicle_state.get("lastUpdateDateTime")
                or status.get("lastUpdateDateTime")
            )

            # Doors (each has lock + position)
            doors = vehicle_state.get("doors")
            if doors:
                for door_key, feature in self._graphql_door_map.items():
                    door = doors.get(door_key)
                    if door:
                        closed, locked = opening_state_from_graphql(door)
                        self._store_opening(feature, closed, locked, observed_at)

            # Windows (position only)
            windows = vehicle_state.get("windows")
            if windows:
                for win_key, feature in self._graphql_window_map.items():
                    window = windows.get(win_key)
                    if window:
                        closed, _ = opening_state_from_graphql(window)
                        self._store_opening(feature, closed, None, observed_at)

            for opening_key in backdoor_candidates(self._backdoor_type):
                opening = vehicle_state.get(opening_key)
                if opening:
                    closed, locked = opening_state_from_graphql(opening)
                    if self._store_opening(
                        VehicleFeatures.Trunk, closed, locked, observed_at
                    ):
                        break

            # Hood (position only)
            hood = vehicle_state.get("hood")
            if hood:
                closed, _ = opening_state_from_graphql(hood)
                self._store_opening(
                    VehicleFeatures.Hood, closed, None, observed_at
                )

            # Moonroof (position only)
            moonroof = vehicle_state.get("moonroof")
            if moonroof:
                closed, _ = opening_state_from_graphql(moonroof)
                self._store_opening(
                    VehicleFeatures.Moonroof, closed, None, observed_at
                )

            # Engine
            engine = vehicle_state.get("engine")
            if engine:
                running = engine.get("running")
                if running is not None:
                    self._features[
                        VehicleFeatures.RemoteStartStatus
                    ] = ToyotaRemoteStart(
                        date=None,
                        on=bool(running),
                        timer=None,
                    )

        # Telemetry from GraphQL response
        telemetry = status.get("telemetry")
        if telemetry:
            odo = telemetry.get("odo")
            if odo and odo.get("value") is not None:
                self._features[VehicleFeatures.Odometer] = ToyotaNumeric(
                    odo["value"], odo.get("unit", "")
                )
            fugage = telemetry.get("fugage")
            if fugage and fugage.get("value") is not None:
                self._features[VehicleFeatures.FuelLevel] = ToyotaNumeric(
                    fugage["value"], fugage.get("unit", "%")
                )
            range_val = telemetry.get("range")
            if range_val and range_val.get("value") is not None:
                self._features[VehicleFeatures.DistanceToEmpty] = ToyotaNumeric(
                    range_val["value"], range_val.get("unit", "")
                )

    #
    # get_telemetry
    #

    def _parse_telemetry(self, telemetry: dict) -> None:
        if not telemetry:
            return

        observed_at = parse_api_timestamp(telemetry.get("lastTimestamp"))
        if observed_at is not None:
            self._features[VehicleFeatures.LastTimeStamp] = ToyotaNumeric(
                observed_at.timestamp(), ""
            )

        for key, value in telemetry.items():
            if value is None:
                continue

            if key == "lastTimestamp":
                continue

            if key == "tirePressureTimestamp":
                tire_observed_at = parse_api_timestamp(value)
                if tire_observed_at is not None:
                    self._features[
                        VehicleFeatures.LastTirePressureTimeStamp
                    ] = ToyotaNumeric(tire_observed_at.timestamp(), "")
                continue
                
            # fuel level is a primitive
            if key == "fuelLevel":
                self._features[VehicleFeatures.FuelLevel] = ToyotaNumeric(value, "%")
                continue

            # vehicle_location has a different shape and different target entity class
            if key == "vehicleLocation":
                self._features[VehicleFeatures.RealTimeLocation] = ToyotaLocation(
                    value.get("latitude"), value.get("longitude")
                )
                continue

            if "Window" in key or "Roof" in key:
                if value not in (1, 2):
                    continue
                feature = self._vehicle_telemetry_map.get(key)
                if feature is not None:
                    self._store_opening(
                        feature, closed=(value == 2), locked=None, observed_at=observed_at
                    )
                continue

            if self._vehicle_telemetry_map.get(key) is not None:
                if isinstance(value, dict) and "value" in value:
                    self._features[self._vehicle_telemetry_map[key]] = ToyotaNumeric(
                        value["value"], value.get("unit", "")
                    )
                else:
                    self._features[self._vehicle_telemetry_map[key]] = ToyotaNumeric(
                        value, ""
                    )
                continue
