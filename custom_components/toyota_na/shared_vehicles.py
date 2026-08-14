"""Home Assistant lifecycle handling for vehicles shared across accounts."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir

from toyota_na.vehicle.base_vehicle import ToyotaVehicle

from .const import DOMAIN, OPT_EXCLUDED_VINS

_LOGGER = logging.getLogger(__name__)


def shared_vehicle_issue_id(vin: str, entry_id: str) -> str:
    """Return an issue ID owned by one config entry for one shared VIN."""
    return f"shared_vehicle_{vin}_{entry_id}"


async def async_update_options(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Reload only when the set of managed vehicles changes."""
    entry_runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if entry_runtime is None:
        return
    excluded_vins = set(entry.options.get(OPT_EXCLUDED_VINS, []))
    if entry_runtime.get("excluded_vins_snapshot") == excluded_vins:
        return
    await hass.config_entries.async_reload(entry.entry_id)


def report_vehicle_conflict(
    hass: HomeAssistant,
    entry: ConfigEntry,
    vehicle: ToyotaVehicle,
    managing_entry_id: str,
) -> None:
    """Report that another loaded account already manages a vehicle."""
    managing_entry = hass.config_entries.async_get_entry(managing_entry_id)
    managing_account = (
        managing_entry.title if managing_entry is not None else "another account"
    )
    _LOGGER.warning(
        "VIN ...%s (%s %s) is already managed by Toyota account %s; "
        "skipping it for %s. Use Configure to choose which account manages "
        "a shared vehicle.",
        vehicle.vin[-4:],
        vehicle.model_year,
        vehicle.model_name,
        managing_account,
        entry.title,
    )
    ir.async_create_issue(
        hass,
        DOMAIN,
        shared_vehicle_issue_id(vehicle.vin, entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="shared_vehicle",
        translation_placeholders={
            "vehicle": f"{vehicle.model_year} {vehicle.model_name}",
            "this_account": entry.title,
            "managing_account": managing_account,
        },
    )


def clear_vehicle_conflict(
    hass: HomeAssistant, entry_id: str, vin: str
) -> None:
    """Clear one conflict issue raised by a config entry."""
    ir.async_delete_issue(
        hass,
        DOMAIN,
        shared_vehicle_issue_id(vin, entry_id),
    )


def clear_entry_conflicts(hass: HomeAssistant, entry_id: str) -> None:
    """Clear every shared-vehicle issue owned by a config entry."""
    issue_registry = ir.async_get(hass)
    suffix = f"_{entry_id}"
    for issue_domain, issue_id in list(issue_registry.issues):
        if (
            issue_domain == DOMAIN
            and issue_id.startswith("shared_vehicle_")
            and issue_id.endswith(suffix)
        ):
            ir.async_delete_issue(hass, DOMAIN, issue_id)


def entry_manages_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Return whether a loaded entry still manages a vehicle device."""
    vin = next(
        (
            identifier[1]
            for identifier in device_entry.identifiers
            if identifier[0] == DOMAIN
        ),
        None,
    )
    entry_runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = entry_runtime.get("coordinator")
    return bool(
        vin
        and coordinator is not None
        and coordinator.data
        and any(vehicle.vin == vin for vehicle in coordinator.data)
    )


def prune_entry_devices(
    hass: HomeAssistant, entry: ConfigEntry, vins: set[str]
) -> None:
    """Remove devices only for explicitly excluded or contested VINs."""
    if not vins:
        return

    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(
        device_registry, entry.entry_id
    ):
        vin = next(
            (
                identifier[1]
                for identifier in device.identifiers
                if identifier[0] == DOMAIN
            ),
            None,
        )
        if vin in vins:
            device_registry.async_update_device(
                device.id,
                remove_config_entry_id=entry.entry_id,
            )
