from datetime import timedelta
import logging
from zoneinfo import ZoneInfo

from toyota_na.auth import ToyotaOneAuth
from toyota_na.client import ToyotaOneClient
from .patch_auth import (
    authorize, check_tokens, extract_tokens, get_tokens, logged_in,
    refresh_tokens, request_tokens, set_tokens,
)

ToyotaOneAuth.authorize = authorize
ToyotaOneAuth.check_tokens = check_tokens
ToyotaOneAuth.request_tokens = request_tokens
ToyotaOneAuth.refresh_tokens = refresh_tokens
ToyotaOneAuth._extract_tokens = extract_tokens
ToyotaOneAuth.get_tokens = get_tokens
ToyotaOneAuth.set_tokens = set_tokens
ToyotaOneAuth.logged_in = logged_in

# Patch client code
from .patch_client import (
    get_electric_realtime_status,
    get_electric_status,
    get_climate_settings,
    get_climate_schedules,
    save_climate_schedule,
    update_climate_settings,
    update_charge_settings,
    save_charge_schedule,
    disable_charge_schedules,
    electric_command,
    api_request,
    _auth_headers,
    get_telemetry,
    get_tire_pressure,
    get_vehicle_status_17cyplus,
    get_vehicle_status_21mm,
    get_vehicle_status_route,
    get_engine_status_17cyplus,
    get_engine_status_21mm,
    get_engine_status_route,
    send_refresh_request_17cyplus,
    send_refresh_request_21mm,
    send_refresh_request_route,
    remote_request_17cyplus,
    remote_request_21mm,
    remote_request_route,
    get_vehicle_status_17cy,
    get_engine_status_17cy,
    send_refresh_request_17cy,
    remote_request_17cy,
    graphql_request,
    graphql_pre_wake,
    graphql_confirm_subscription,
    graphql_refresh_status,
    graphql_get_vehicle_status,
    graphql_send_remote_command,
    remote_request_24mm,
)
ToyotaOneClient.get_electric_realtime_status = get_electric_realtime_status
ToyotaOneClient.get_electric_status = get_electric_status
ToyotaOneClient.get_climate_settings = get_climate_settings
ToyotaOneClient.get_climate_schedules = get_climate_schedules
ToyotaOneClient.save_climate_schedule = save_climate_schedule
ToyotaOneClient.save_charge_schedule = save_charge_schedule
ToyotaOneClient.disable_charge_schedules = disable_charge_schedules
ToyotaOneClient.update_charge_settings = update_charge_settings
ToyotaOneClient.update_climate_settings = update_climate_settings
ToyotaOneClient.electric_command = electric_command
ToyotaOneClient.api_request = api_request
ToyotaOneClient._auth_headers = _auth_headers
ToyotaOneClient.get_telemetry = get_telemetry
ToyotaOneClient.get_tire_pressure = get_tire_pressure
ToyotaOneClient.get_vehicle_status_17cyplus = get_vehicle_status_17cyplus
ToyotaOneClient.get_vehicle_status_21mm = get_vehicle_status_21mm
ToyotaOneClient.get_vehicle_status_route = get_vehicle_status_route
ToyotaOneClient.get_engine_status_17cyplus = get_engine_status_17cyplus
ToyotaOneClient.get_engine_status_21mm = get_engine_status_21mm
ToyotaOneClient.get_engine_status_route = get_engine_status_route
ToyotaOneClient.send_refresh_request_17cyplus = send_refresh_request_17cyplus
ToyotaOneClient.send_refresh_request_21mm = send_refresh_request_21mm
ToyotaOneClient.send_refresh_request_route = send_refresh_request_route
ToyotaOneClient.remote_request_17cyplus = remote_request_17cyplus
ToyotaOneClient.remote_request_21mm = remote_request_21mm
ToyotaOneClient.remote_request_route = remote_request_route
ToyotaOneClient.get_vehicle_status_17cy = get_vehicle_status_17cy
ToyotaOneClient.get_engine_status_17cy = get_engine_status_17cy
ToyotaOneClient.send_refresh_request_17cy = send_refresh_request_17cy
ToyotaOneClient.remote_request_17cy = remote_request_17cy
ToyotaOneClient.graphql_request = graphql_request
ToyotaOneClient.graphql_pre_wake = graphql_pre_wake
ToyotaOneClient.graphql_confirm_subscription = graphql_confirm_subscription
ToyotaOneClient.graphql_refresh_status = graphql_refresh_status
ToyotaOneClient.graphql_get_vehicle_status = graphql_get_vehicle_status
ToyotaOneClient.graphql_send_remote_command = graphql_send_remote_command
ToyotaOneClient.remote_request_24mm = remote_request_24mm

# Patch base_vehicle
import toyota_na.vehicle.base_vehicle
from .patch_base_vehicle import ApiVehicleGeneration
toyota_na.vehicle.base_vehicle.ApiVehicleGeneration = ApiVehicleGeneration
from .patch_base_vehicle import VehicleFeatures
toyota_na.vehicle.base_vehicle.VehicleFeatures = VehicleFeatures
from .patch_base_vehicle import RemoteRequestCommand
toyota_na.vehicle.base_vehicle.RemoteRequestCommand = RemoteRequestCommand
from .patch_base_vehicle import ToyotaVehicle
toyota_na.vehicle.base_vehicle.ToyotaVehicle = ToyotaVehicle

# Patch seventeen_cy_plus
import toyota_na.vehicle.vehicle_generations.seventeen_cy_plus
from .patch_seventeen_cy_plus import (
    SeventeenCYPlusToyotaVehicle as PatchedSeventeenCYPlusToyotaVehicle,
)
toyota_na.vehicle.vehicle_generations.seventeen_cy_plus.SeventeenCYPlusToyotaVehicle = PatchedSeventeenCYPlusToyotaVehicle

# Patch seventeen_cy
import toyota_na.vehicle.vehicle_generations.seventeen_cy
from .patch_seventeen_cy import SeventeenCYToyotaVehicle as PatchedSeventeenCYToyotaVehicle
toyota_na.vehicle.vehicle_generations.seventeen_cy.SeventeenCYToyotaVehicle = PatchedSeventeenCYToyotaVehicle

from toyota_na.exceptions import AuthError
from toyota_na.vehicle.base_vehicle import RemoteRequestCommand, ToyotaVehicle

#Patch get_vehicles
from .patch_vehicle import get_vehicles
#from toyota_na.vehicle.vehicle import get_vehicles

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ServiceValidationError
from homeassistant.helpers import device_registry as dr, service
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .websocket_handler import ToyotaWebSocketHandler
from .command_refresh import refresh_after_command
from .service_helpers import translate_service_errors
from .wake_policy import automatic_wake_due, record_vehicle_wake

from .const import (
    COMMAND_MAP,
    COMMAND_REFRESH_DELAY,
    DOMAIN,
    UPDATE_INTERVAL,
    REFRESH_STATUS_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = ["binary_sensor", "button", "device_tracker", "lock", "number", "select", "sensor", "switch"]


async def _refresh_coordinator_after_command(coordinator, vin=None, command=None) -> None:
    """Poll Toyota's cloud after it has had time to process a command."""
    await refresh_after_command(coordinator, vin, command, delay=COMMAND_REFRESH_DELAY)


async def async_setup(hass: HomeAssistant, _processed_config) -> bool:
    @service.verify_domain_control(DOMAIN)
    async def async_service_handle(service_call: ServiceCall) -> None:
        """Handle dispatched services."""

        device_registry = dr.async_get(hass)
        device = device_registry.async_get(service_call.data["vehicle"])
        remote_action = service_call.service

        if device is None:
            _LOGGER.warning("Device does not exist")
            return

        if len(device.config_entries) == 0:
            _LOGGER.warning("Device missing config entry")
            return

        vin = next(
            (
                identifier[1]
                for identifier in device.identifiers
                if identifier[0] == DOMAIN
            ),
            None,
        )
        if vin is None:
            _LOGGER.warning("Device has no %s identifier", DOMAIN)
            return

        coordinator = None
        config_entry = None
        for entry_id in device.config_entries:
            if entry_id not in hass.data[DOMAIN]:
                _LOGGER.warning("Config entry not found")
                continue

            if "coordinator" not in hass.data[DOMAIN][entry_id]:
                _LOGGER.warning("Coordinator not found")
                continue

            candidate = hass.data[DOMAIN][entry_id]["coordinator"]
            if candidate.data is None:
                _LOGGER.warning("No coordinator data")
                continue
            if not any(vehicle.vin == vin for vehicle in candidate.data):
                continue

            coordinator = candidate
            config_entry = hass.config_entries.async_get_entry(entry_id)
            break

        if coordinator is None:
            _LOGGER.warning("No loaded coordinator found for device")
            return

        vehicle = next(
            item for item in coordinator.data if item.vin == vin
        )
        if remote_action in ("set_climate_schedule", "delete_climate_schedule"):
            fields = {"enabled": "enabled", "start_time": "time", "date": "date", "days": "days", "temperature": "temperature"}
            changes = {field: service_call.data[key] for key, field in fields.items() if key in service_call.data}
            with translate_service_errors():
                await vehicle.update_climate_schedule(
                    service_call.data.get("schedule_id"), zone=ZoneInfo(hass.config.time_zone),
                    delete=remote_action == "delete_climate_schedule", **changes,
                )
            coordinator.async_set_updated_data(coordinator.data)
            return
        if remote_action == "disable_charge_schedules":
            with translate_service_errors():
                changed = await vehicle.disable_charge_schedules()
            if changed and config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)
            coordinator.async_set_updated_data(coordinator.data)
            return
        if remote_action in ("set_charge_schedule", "delete_charge_schedule"):
            fields = {"enabled": "enabled", "start_time": "startTime", "end_time": "endTime", "days": "daysOfTheWeek"}
            changes = {field: service_call.data[key] for key, field in fields.items() if key in service_call.data}
            with translate_service_errors():
                await vehicle.update_charge_schedule(
                    service_call.data.get("schedule_id"),
                    delete=remote_action == "delete_charge_schedule", **changes,
                )
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)
            coordinator.async_set_updated_data(coordinator.data)
            return
        command = COMMAND_MAP[remote_action]
        if not vehicle.supports_command(command):
            raise ServiceValidationError(
                f"{remote_action.replace('_', ' ').capitalize()} is unavailable for this vehicle."
            )

        if remote_action.upper() == "REFRESH":
            with translate_service_errors():
                await vehicle.poll_vehicle_refresh()
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)
            coordinator.async_set_updated_data(coordinator.data)
        else:
            with translate_service_errors():
                await vehicle.send_command(command)
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)

        task = hass.async_create_task(
            _refresh_coordinator_after_command(coordinator, vin, command)
        )
        if config_entry is not None:
            config_entry.async_on_unload(task.cancel)
        _LOGGER.info("Handling service call %s for VIN ...%s", remote_action, vin[-4:])

        return

    for action in (
        *COMMAND_MAP, "set_charge_schedule", "delete_charge_schedule", "disable_charge_schedules",
        "set_climate_schedule", "delete_climate_schedule",
    ):
        hass.services.async_register(DOMAIN, action, async_service_handle)

    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    if "password" in entry.data:
        entry_data = dict(entry.data)
        del entry_data["password"]
        hass.config_entries.async_update_entry(entry, data=entry_data)
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})

    client = ToyotaOneClient(
        ToyotaOneAuth(
            refresh_secs=-180,
            initial_tokens=entry.data["tokens"],
            callback=lambda tokens: update_tokens(tokens, hass, entry),
        )
    )
    try:
        client.auth.set_tokens(entry.data["tokens"])
        device_id = entry.data.get("device_id")
        if isinstance(device_id, str) and device_id:
            client.auth.set_device_id(device_id)
        else:
            entry_data = dict(entry.data)
            entry_data["device_id"] = client.auth.get_device_id()
            hass.config_entries.async_update_entry(entry, data=entry_data)
        await client.auth.check_tokens()
    except AuthError as e:
        _LOGGER.exception(e)
        raise ConfigEntryAuthFailed(e) from e

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=lambda: update_vehicles_status(
            hass, client, entry, coordinator
        ),
        update_interval=timedelta(seconds=UPDATE_INTERVAL),
    )
    ws_handler = None
    try:
        await coordinator.async_config_entry_first_refresh()

        @callback
        def handle_vehicle_status(vin: str, status: dict) -> None:
            vehicles = coordinator.data or []
            vehicle = next((item for item in vehicles if item.vin == vin), None)
            if vehicle is None or not hasattr(vehicle, "apply_graphql_status"):
                return
            if vehicle.apply_graphql_status(status):
                coordinator.async_set_updated_data(vehicles)

        ws_handler = ToyotaWebSocketHandler(client, handle_vehicle_status)
        await ws_handler.start(_websocket_contexts(coordinator.data or []))
        client._ws_handler = ws_handler

        hass.data[DOMAIN][entry.entry_id] = {
            "toyota_na_client": client,
            "coordinator": coordinator,
            "ws_handler": ws_handler,
        }

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        if ws_handler is not None:
            try:
                await ws_handler.stop()
            except Exception as err:
                _LOGGER.debug("WebSocket cleanup failed: %s", err)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        raise

    return True


def update_tokens(tokens: dict[str, str], hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.info("Tokens refreshed, updating ConfigEntry")
    data = dict(entry.data)
    data["tokens"] = tokens
    hass.config_entries.async_update_entry(entry, data=data)


def _websocket_contexts(vehicles):
    websocket_generations = {
        ApiVehicleGeneration.MM21,
        ApiVehicleGeneration.MM24,
        ApiVehicleGeneration.BEV26,
    }
    return {
        vehicle.vin: {
            "region": vehicle.region,
            "backdoor_type": vehicle.backdoor_type,
        }
        for vehicle in vehicles
        if vehicle.can_receive_status
        and vehicle.generation in websocket_generations
    }


async def update_vehicles_status(
    hass: HomeAssistant,
    client: ToyotaOneClient,
    entry: ConfigEntry,
    coordinator: DataUpdateCoordinator,
):
    try:
        _LOGGER.debug("Updating vehicle status")
        raw_vehicles = await get_vehicles(client)
        ws_handler = getattr(client, "_ws_handler", None)
        if ws_handler is not None:
            await ws_handler.update_vehicle_contexts(_websocket_contexts(raw_vehicles))
        wake_requested = False
        vehicles: list[ToyotaVehicle] = []
        for vehicle in raw_vehicles:
            need_refresh = automatic_wake_due(
                entry.data,
                entry.options,
                REFRESH_STATUS_INTERVAL,
                vin=vehicle.vin,
            )
            if need_refresh and vehicle.supports_command(RemoteRequestCommand.Refresh):
                try:
                    _LOGGER.info(
                        "Requesting vehicle refresh for %s %s",
                        vehicle.model_year,
                        vehicle.model_name,
                    )
                    await vehicle.poll_vehicle_refresh()
                    record_vehicle_wake(hass, entry, vehicle.vin)
                    wake_requested = True
                except AuthError:
                    raise
                except Exception as e:
                    _LOGGER.warning("Vehicle refresh failed (%s), continuing without refresh", e)
            vehicles.append(vehicle)
        if wake_requested:
            task = hass.async_create_task(
                _refresh_coordinator_after_command(coordinator)
            )
            entry.async_on_unload(task.cancel)
        return vehicles
    except AuthError as e:
        raise ConfigEntryAuthFailed(e) from e
    except Exception as e:
        _LOGGER.exception("Error fetching data")
        raise UpdateFailed(e) from e


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a vehicle that is no longer in the Toyota account."""
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id, {})
    client = entry_data.get("toyota_na_client")
    if client is None or not any(domain == DOMAIN for domain, _ in device_entry.identifiers):
        return False
    vehicles = await client.get_user_vehicle_list()
    if not isinstance(vehicles, list) or any(
        not isinstance(vehicle, dict) or not vehicle.get("vin") for vehicle in vehicles
    ):
        return False
    return not any(
        (DOMAIN, vehicle["vin"]) in device_entry.identifiers for vehicle in vehicles
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        entry_data = hass.data[DOMAIN].pop(entry.entry_id)
        ws_handler = entry_data.get("ws_handler")
        if ws_handler:
            await ws_handler.stop()

    return unload_ok
