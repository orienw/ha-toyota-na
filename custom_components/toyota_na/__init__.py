from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    ServiceValidationError,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import service
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .auth import ToyotaOneAuth
from .client import ToyotaOneClient
from .command_refresh import refresh_after_command
from .const import (
    COMMAND_MAP,
    COMMAND_REFRESH_DELAY,
    CONF_WINDOW_TELEMETRY,
    DOMAIN,
    REFRESH_STATUS_INTERVAL,
    UPDATE_INTERVAL,
)
from .exceptions import AuthError
from .generations import GENERATION_TRANSPORTS
from .remote_access import sync_remote_access_issues
from .service_helpers import translate_service_errors
from .session import HTTP_TIMEOUT
from .vehicle import RemoteRequestCommand, ToyotaVehicle
from .vehicle_discovery import get_vehicles
from .wake_policy import automatic_wake_due, record_vehicle_wake
from .websocket_handler import ToyotaWebSocketHandler

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [
    "binary_sensor",
    "button",
    "device_tracker",
    "event",
    "lock",
    "number",
    "select",
    "sensor",
    "switch",
]

_VEHICLE_SERVICE_SCHEMA = vol.Schema(
    {vol.Optional("vehicle"): str},
    extra=vol.PREVENT_EXTRA,
)
_SET_CHARGE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle"): str,
        vol.Optional("schedule_id"): vol.Any(None, int, float, str),
        vol.Optional("enabled"): bool,
        vol.Optional("start_time"): vol.Any(str, time),
        vol.Optional("end_time"): vol.Any(str, time),
        vol.Optional("days"): [str],
    },
    extra=vol.PREVENT_EXTRA,
)
_DELETE_CHARGE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle"): str,
        vol.Optional("schedule_id"): vol.Any(None, int, float, str),
    },
    extra=vol.PREVENT_EXTRA,
)
_SET_CLIMATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle"): str,
        vol.Optional("schedule_id"): vol.Any(None, int, float, str),
        vol.Optional("enabled"): bool,
        vol.Optional("start_time"): vol.Any(str, time),
        vol.Optional("date"): vol.Any(str, date),
        vol.Optional("days"): [str],
        vol.Optional("temperature"): vol.Any(int, float),
    },
    extra=vol.PREVENT_EXTRA,
)
_DELETE_CLIMATE_SCHEDULE_SCHEMA = vol.Schema(
    {
        vol.Optional("vehicle"): str,
        vol.Optional("schedule_id"): vol.Any(None, int, float, str),
    },
    extra=vol.PREVENT_EXTRA,
)
_SERVICE_SCHEMAS = {
    **dict.fromkeys(COMMAND_MAP, _VEHICLE_SERVICE_SCHEMA),
    "set_charge_schedule": _SET_CHARGE_SCHEDULE_SCHEMA,
    "delete_charge_schedule": _DELETE_CHARGE_SCHEDULE_SCHEMA,
    "set_climate_schedule": _SET_CLIMATE_SCHEDULE_SCHEMA,
    "delete_climate_schedule": _DELETE_CLIMATE_SCHEDULE_SCHEMA,
}


@dataclass
class ToyotaRuntimeData:
    """Runtime objects owned by a loaded Toyota config entry."""

    client: ToyotaOneClient
    coordinator: ToyotaCoordinator
    ws_handler: ToyotaWebSocketHandler
    started: datetime


type ToyotaConfigEntry = ConfigEntry[ToyotaRuntimeData]


def _loaded_runtime_data(config_entry: ConfigEntry | None) -> ToyotaRuntimeData | None:
    """Return runtime data for a loaded entry, or None when unloaded or missing."""
    if config_entry is None or config_entry.state is not ConfigEntryState.LOADED:
        return None
    runtime_data = getattr(config_entry, "runtime_data", None)
    return runtime_data if isinstance(runtime_data, ToyotaRuntimeData) else None


class ToyotaCoordinator(DataUpdateCoordinator[list[ToyotaVehicle]]):
    """Coordinate vehicle discovery, sequential cloud polling, and push state."""

    config_entry: ToyotaConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ToyotaConfigEntry,
        client: ToyotaOneClient,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),
        )
        self.client = client
        self.ws_handler: ToyotaWebSocketHandler | None = None
        self.background_tasks: set[asyncio.Task[Any]] = set()
        self._vehicles_by_vin: dict[str, ToyotaVehicle] = {}
        self._unsupported_vins: set[str] = set()
        # Stays set until the entry reloads, so a later poll's error can't undo reauth.
        self.reauth_required = False

    def schedule_background_task(self, coro) -> asyncio.Task[Any]:
        """Run a follow-up task and evict it on completion so unload only cancels live ones."""
        task = self.hass.async_create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    @callback
    def cancel_background_tasks(self) -> None:
        """Cancel all in-flight follow-up tasks owned by this entry."""
        for task in list(self.background_tasks):
            task.cancel()

    async def _stop_websocket(self) -> None:
        if self.ws_handler is None:
            return
        try:
            await self.ws_handler.stop()
        except Exception as err:
            _LOGGER.debug("WebSocket cleanup failed: %s", err)

    @callback
    def handle_auth_error(self, err: AuthError) -> None:
        """Stop push updates, mark the entry as needing reauthentication, and notify entities."""
        self.reauth_required = True
        if self.ws_handler is not None:
            self.schedule_background_task(self._stop_websocket())
        auth_err = ConfigEntryAuthFailed(err)
        auth_err.__cause__ = err
        self.last_exception = auth_err
        self.last_update_success = False
        self._unschedule_refresh()
        self.async_update_listeners()
        if self.config_entry is not None:
            self.config_entry.async_start_reauth_if_available(self.hass)

    async def _async_update_data(self) -> list[ToyotaVehicle]:
        entry = self.config_entry
        if self.reauth_required:
            raise ConfigEntryAuthFailed("Sign in to Toyota again.")
        try:
            _LOGGER.debug("Updating vehicle status")
            raw_vehicles = await get_vehicles(
                self.client,
                window_telemetry=entry.options.get(CONF_WINDOW_TELEMETRY) is True,
                vehicles_by_vin=self._vehicles_by_vin,
                unsupported_vins=self._unsupported_vins,
                ws_handler=self.ws_handler,
            )
            if self.ws_handler is not None:
                await self.ws_handler.update_vehicle_contexts(_websocket_contexts(raw_vehicles))
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
                        record_vehicle_wake(self.hass, entry, vehicle.vin)
                        wake_requested = True
                    except AuthError:
                        raise
                    except Exception as e:
                        _LOGGER.warning(
                            "Vehicle refresh failed (%s), continuing without refresh", e
                        )
                vehicles.append(vehicle)
            if wake_requested:
                self.schedule_background_task(_refresh_coordinator_after_command(self))
            sync_remote_access_issues(self.hass, entry, vehicles)
            return vehicles
        except AuthError as e:
            self.reauth_required = True
            await self._stop_websocket()
            if not self.last_update_success:
                # Home Assistant skips listeners when a failed poll follows another, so
                # entities would miss the switch to unavailable.
                self.hass.loop.call_soon(self.async_update_listeners)
            raise ConfigEntryAuthFailed(e) from e
        except Exception as e:
            _LOGGER.debug("Error fetching data", exc_info=True)
            raise UpdateFailed(e) from e


async def _refresh_coordinator_after_command(coordinator, vin=None, command=None) -> None:
    """Poll Toyota's cloud after it has had time to process a command."""
    await refresh_after_command(coordinator, vin, command, delay=COMMAND_REFRESH_DELAY)


async def async_setup(hass: HomeAssistant, _processed_config) -> bool:
    @service.verify_domain_control(DOMAIN)
    async def async_service_handle(service_call: ServiceCall) -> None:
        """Handle dispatched services."""

        device_id = service_call.data.get("vehicle")
        if device_id is None:
            raise ServiceValidationError("Select a vehicle.")
        device_registry = dr.async_get(hass)
        device = device_registry.async_get(device_id)
        remote_action = service_call.service

        if device is None:
            raise ServiceValidationError("Device does not exist.")

        vin = next(
            (identifier[1] for identifier in device.identifiers if identifier[0] == DOMAIN),
            None,
        )
        if vin is None:
            raise ServiceValidationError("Device is not a Toyota or Lexus vehicle.")

        entry_id = device.config_entry_id
        config_entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
        runtime_data = _loaded_runtime_data(config_entry)
        coordinator = runtime_data.coordinator if runtime_data is not None else None
        if (
            coordinator is None
            or coordinator.data is None
            or not any(vehicle.vin == vin for vehicle in coordinator.data)
        ):
            raise ServiceValidationError(
                "Vehicle is unavailable. Check that the integration is loaded."
            )

        vehicle = next(item for item in coordinator.data if item.vin == vin)
        if remote_action in ("set_climate_schedule", "delete_climate_schedule"):
            fields = {
                "enabled": "enabled",
                "start_time": "time",
                "date": "date",
                "days": "days",
                "temperature": "temperature",
            }
            changes = {
                field: service_call.data[key]
                for key, field in fields.items()
                if key in service_call.data
            }
            with translate_service_errors(coordinator):
                await vehicle.update_climate_schedule(
                    service_call.data.get("schedule_id"),
                    zone=dt_util.get_default_time_zone(),
                    delete=remote_action == "delete_climate_schedule",
                    **changes,
                )
            coordinator.async_update_listeners()
            return
        if remote_action in ("set_charge_schedule", "delete_charge_schedule"):
            fields = {
                "enabled": "enabled",
                "start_time": "startTime",
                "end_time": "endTime",
                "days": "daysOfTheWeek",
            }
            changes = {
                field: service_call.data[key]
                for key, field in fields.items()
                if key in service_call.data
            }
            with translate_service_errors(coordinator):
                await vehicle.update_charge_schedule(
                    service_call.data.get("schedule_id"),
                    delete=remote_action == "delete_charge_schedule",
                    **changes,
                )
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)
            coordinator.async_update_listeners()
            return
        command = COMMAND_MAP[remote_action]
        if not vehicle.supports_command(command):
            raise ServiceValidationError(
                f"{remote_action.replace('_', ' ').capitalize()} is unavailable for this vehicle."
            )

        if remote_action.upper() == "REFRESH":
            with translate_service_errors(coordinator):
                await vehicle.poll_vehicle_refresh()
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)
            coordinator.async_update_listeners()
        else:
            with translate_service_errors(coordinator):
                await vehicle.send_command(command)
            if config_entry is not None:
                record_vehicle_wake(hass, config_entry, vin)

        coordinator.schedule_background_task(
            _refresh_coordinator_after_command(coordinator, vin, command)
        )
        _LOGGER.debug("Handling service call %s for VIN ...%s", remote_action, vin[-4:])

        return

    for action, schema in _SERVICE_SCHEMAS.items():
        hass.services.async_register(DOMAIN, action, async_service_handle, schema=schema)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ToyotaConfigEntry) -> bool:
    if "password" in entry.data:
        entry_data = dict(entry.data)
        del entry_data["password"]
        hass.config_entries.async_update_entry(entry, data=entry_data)

    # Like the app's HTTP clients, send no cookies. Home Assistant closes this session on unload.
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.DummyCookieJar(), timeout=HTTP_TIMEOUT
    )
    client = ToyotaOneClient(
        ToyotaOneAuth(
            refresh_secs=-180,
            initial_tokens=entry.data["tokens"],
            callback=lambda tokens: update_tokens(tokens, hass, entry),
            session=session,
        ),
        session=session,
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
    except (aiohttp.ClientError, TimeoutError) as e:
        raise ConfigEntryNotReady(e) from e

    # Notifications Toyota dates before setup are history, not new events.
    started = datetime.now(UTC)
    coordinator = ToyotaCoordinator(hass, entry, client)
    entry.async_on_unload(coordinator.cancel_background_tasks)
    ws_handler: ToyotaWebSocketHandler | None = None
    try:
        await coordinator.async_config_entry_first_refresh()

        @callback
        def handle_vehicle_status(vin: str, status: dict) -> None:
            vehicles = coordinator.data or []
            vehicle = next((item for item in vehicles if item.vin == vin), None)
            if vehicle is None or not hasattr(vehicle, "apply_graphql_status"):
                return
            if vehicle.apply_graphql_status(status):
                # Vehicle pushes must not postpone account-only reads.
                coordinator.async_update_listeners()

        ws_handler = ToyotaWebSocketHandler(
            client,
            handle_vehicle_status,
            auth_error_callback=coordinator.handle_auth_error,
        )
        await ws_handler.start(_websocket_contexts(coordinator.data or []))
        coordinator.ws_handler = ws_handler
        entry.runtime_data = ToyotaRuntimeData(
            client=client,
            coordinator=coordinator,
            ws_handler=ws_handler,
            started=started,
        )

        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        coordinator.cancel_background_tasks()
        if ws_handler is not None:
            try:
                await ws_handler.stop()
            except Exception as err:
                _LOGGER.debug("WebSocket cleanup failed: %s", err)
        raise

    return True


def update_tokens(tokens: dict[str, str], hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.debug("Tokens refreshed, updating ConfigEntry")
    data = dict(entry.data)
    data["tokens"] = tokens
    hass.config_entries.async_update_entry(entry, data=data)


def _websocket_contexts(vehicles):
    return {
        vehicle.vin: {
            "region": vehicle.region,
            "backdoor_type": vehicle.backdoor_type,
        }
        for vehicle in vehicles
        if (transport := GENERATION_TRANSPORTS.get(vehicle.generation)) is not None
        and transport.appsync_push
    }


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: ToyotaConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow removing a vehicle that is no longer in the Toyota account."""
    runtime_data = _loaded_runtime_data(config_entry)
    client = runtime_data.client if runtime_data is not None else None
    if client is None or not any(domain == DOMAIN for domain, _ in device_entry.identifiers):
        return False
    vehicles = await client.get_user_vehicle_list()
    if not isinstance(vehicles, list) or any(
        not isinstance(vehicle, dict) or not vehicle.get("vin") for vehicle in vehicles
    ):
        return False
    return not any((DOMAIN, vehicle["vin"]) in device_entry.identifiers for vehicle in vehicles)


async def async_unload_entry(hass: HomeAssistant, entry: ToyotaConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        runtime_data = getattr(entry, "runtime_data", None)
        ws_handler = (
            runtime_data.ws_handler if isinstance(runtime_data, ToyotaRuntimeData) else None
        )
        if ws_handler is not None:
            await ws_handler.stop()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear the repairs of an account that was removed."""
    sync_remote_access_issues(hass, entry, [])
