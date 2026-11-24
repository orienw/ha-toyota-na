"""Diagnostics support for ha-toyota-na."""

from __future__ import annotations

import logging
import re

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
)
from homeassistant.core import HomeAssistant

from .client import ToyotaOneClient
from .vehicle import (
    GENERATION_TRANSPORTS,
    ApiVehicleGeneration,
    fetch_engine_status,
    fetch_vehicle_status,
)
from .vehicle_helpers import endpoint_generation, is_appsync_generation

_LOGGER = logging.getLogger(__name__)
# Toyota's structured response codes, never its free text.
TOYOTA_ERROR_CODE = re.compile(r"(?:ONE(?:-[A-Z]+)+|APIGW)-\d{3,5}")

TO_REDACT = {
    CONF_ACCESS_TOKEN,
    CONF_EMAIL,
    CONF_PASSWORD,
    "address",
    "ctsLinks",  # contains a vin number
    "dealerName",
    "device_id",
    "firstName",
    "guid",
    "lastName",
    "phoneNumber",
    "preferredDealerCode",
    "remoteUserGuid",
    "subscriberGuid",
    "accountInfoId",
    "id_token",
    "imei",
    "emergencyContact",
    "nickName",
    "vehicleName",
    "parkingNotes",
    "euiccid",
    "vehicleUnitTerminalNumber",
    "vhrgenID",  # vehicle health report identifiers
    "vhrid",
    "contractId",
    "registrationNumber",
    "oldImei",
    "refresh_token",
    "subscriptionID",  # contains a vin number
    "username",
    "vin",
    "latitude",
    "longitude",
    "lat",  # notification history
    "lon",
}
# Notification fields that describe an item without Toyota's text or location.
_NOTIFICATION_FIELDS = (
    "messageId",
    "category",
    "displayCategory",
    "subcategory",
    "type",
    "status",
    "notificationDate",
    "isRead",
)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> dict:
    """Return diagnostics for a config entry."""
    runtime_data = (
        getattr(config_entry, "runtime_data", None)
        if config_entry.state is ConfigEntryState.LOADED
        else None
    )
    client: ToyotaOneClient | None = getattr(runtime_data, "client", None)
    ws_handler = getattr(runtime_data, "ws_handler", None)

    # Keep the report available while Toyota's vehicle list is failing.
    vehicle_list = {"data": None}
    try:
        if client is not None:
            vehicle_list["data"] = await client.get_user_vehicle_list()
    except Exception as err:
        _LOGGER.debug("Vehicle list diagnostics failed: %s", err)
        # Toyota's message text can carry account details that redaction can't see.
        error = type(err).__name__
        if isinstance(getattr(err, "status", None), int):
            error += f" {err.status}"
        code = getattr(err, "response_code", None)
        if isinstance(code, str) and TOYOTA_ERROR_CODE.fullmatch(code):
            error += f" [{code}]"
        vehicle_list["error"] = error
    if vehicle_list["data"] is not None and not isinstance(vehicle_list["data"], list):
        # An unusable body is Toyota's free text too; keep only its shape.
        vehicle_list["unexpected"] = type(vehicle_list["data"]).__name__
        vehicle_list["data"] = None
    user_vehicle_list = vehicle_list["data"] or []
    # Climate, tire pressure and vehicle health as the last poll read them, to see what Toyota
    # reports.
    coordinator = getattr(runtime_data, "coordinator", None)
    vehicles = {vehicle.vin: vehicle for vehicle in getattr(coordinator, "data", None) or []}

    vehicle_status = []
    telemetry = []
    engine_status = []
    electric_status = []
    climate_settings = []
    climate_schedules = []
    tire_pressure = []
    vehicle_health = []
    notification_history = []

    for vehicle in user_vehicle_list:
        if not isinstance(vehicle, dict) or not isinstance(vehicle.get("vin"), str):
            continue
        user_vehicle_status = None
        user_telemetry = None
        user_engine_status = None
        user_electric_status = None
        vin = vehicle["vin"]
        raw_generation = vehicle.get("generation")
        api_generation = (
            raw_generation.strip().upper()
            if isinstance(raw_generation, str) and raw_generation.strip()
            else None
        )
        try:
            transport = (
                GENERATION_TRANSPORTS[ApiVehicleGeneration(api_generation)]
                if api_generation is not None
                else None
            )
        except ValueError:
            transport = None
        generation = (
            transport.endpoint_generation
            if transport is not None
            else endpoint_generation(api_generation)
        )
        raw_region = vehicle.get("region")
        region = raw_region.upper() if isinstance(raw_region, str) and raw_region else "US"
        raw_brand = vehicle.get("brand")
        brand = raw_brand.upper() if isinstance(raw_brand, str) and raw_brand else "T"
        raw_backdoor = vehicle.get("backdoorType")
        backdoor_type = (
            raw_backdoor.lower() if isinstance(raw_backdoor, str) and raw_backdoor else None
        )

        if transport is not None:
            try:
                user_vehicle_status = await fetch_vehicle_status(
                    client,
                    transport,
                    vin=vin,
                    generation=api_generation,
                    region=region,
                    brand=brand,
                    backdoor_type=backdoor_type,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Vehicle status diagnostics failed for VIN ...%s: %s",
                    vin[-4:],
                    err,
                )

        try:
            user_telemetry = await client.get_telemetry(vin, region, generation)
        except Exception as err:
            _LOGGER.debug("Telemetry diagnostics failed for VIN ...%s: %s", vin[-4:], err)

        if transport is not None and transport.engine_transport is not None:
            try:
                user_engine_status = await fetch_engine_status(
                    client,
                    transport,
                    vin=vin,
                    generation=api_generation,
                    region=region,
                    brand=brand,
                )
            except Exception as err:
                _LOGGER.debug(
                    "Engine status diagnostics failed for VIN ...%s: %s",
                    vin[-4:],
                    err,
                )

        try:
            if not (
                transport.uses_appsync
                if transport is not None
                else is_appsync_generation(api_generation)
            ):
                user_electric_status = await client.get_electric_status(
                    vin, region=region, generation=api_generation
                )
        except Exception as err:
            _LOGGER.debug(
                "Electric status diagnostics failed for VIN ...%s: %s",
                vin[-4:],
                err,
            )

        vehicle_status.append(user_vehicle_status)
        telemetry.append(user_telemetry)
        engine_status.append(user_engine_status)
        electric_status.append(user_electric_status)
        climate_settings.append(getattr(vehicles.get(vin), "climate_settings", None) or None)
        climate_schedules.append(getattr(vehicles.get(vin), "climate_schedules", None) or None)
        tire_pressure.append(getattr(vehicles.get(vin), "tire_pressure", None) or None)
        health = getattr(vehicles.get(vin), "health", None) or {}
        vehicle_health.append(
            {key: value for key, value in health.items() if key != "read_at"} or None
        )
        notifications = getattr(vehicles.get(vin), "notifications", None)
        notification_history.append(
            [
                {key: item[key] for key in _NOTIFICATION_FIELDS if key in item}
                for item in notifications
            ]
            if isinstance(notifications, list)
            else None
        )

    return async_redact_data(
        {
            "config_entry": dict(config_entry.data),
            "options": dict(config_entry.options),
            "websocket": {
                "connected": bool(ws_handler and getattr(ws_handler, "is_connected", False))
            },
            "vehicle_list": vehicle_list,
            "vehicle_status": {"data": vehicle_status},
            "telemetry": {"data": telemetry},
            "engine_status": {"data": engine_status},
            "electric_status": {"data": electric_status},
            "climate_settings": {"data": climate_settings},
            "climate_schedules": {"data": climate_schedules},
            "tire_pressure": {"data": tire_pressure},
            "vehicle_health": {"data": vehicle_health},
            "notification_history": {"data": notification_history},
        },
        TO_REDACT,
    )
