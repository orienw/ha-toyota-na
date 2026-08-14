import asyncio
import logging
from typing import Any, cast

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from toyota_na.vehicle.base_vehicle import RemoteRequestCommand, ToyotaVehicle

from .base_entity import ToyotaNABaseEntity
from .const import COMMAND_BUTTONS, COMMAND_REFRESH_DELAY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up vehicle command buttons."""
    coordinator: DataUpdateCoordinator[list[ToyotaVehicle]] = hass.data[DOMAIN][
        config_entry.entry_id
    ]["coordinator"]
    buttons = []

    for vehicle in coordinator.data or []:
        if not vehicle.subscribed:
            continue
        for config in COMMAND_BUTTONS:
            command = cast(RemoteRequestCommand, config["command"])
            if vehicle.supports_command(command):
                buttons.append(
                    ToyotaCommandButton(
                        command,
                        cast(str, config["icon"]),
                        coordinator,
                        cast(str, config["name"]),
                        vehicle.vin,
                    )
                )
        buttons.append(ToyotaRefreshButton(coordinator, "Refresh Status", vehicle.vin))

    async_add_entities(buttons, True)


class ToyotaButtonBase(ToyotaNABaseEntity, ButtonEntity):
    """Shared behavior for vehicle command buttons."""

    def _schedule_refresh(self) -> None:
        self.hass.async_create_task(self._async_refresh_after_delay())

    async def _async_refresh_after_delay(self) -> None:
        try:
            await asyncio.sleep(COMMAND_REFRESH_DELAY)
            await self.coordinator.async_request_refresh()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Post-command refresh failed: %s", err)


class ToyotaCommandButton(ToyotaButtonBase):
    """Send a capability-gated remote command."""

    def __init__(
        self,
        command: RemoteRequestCommand,
        icon: str,
        *args: Any,
    ) -> None:
        super().__init__(*args)
        self._command = command
        self._attr_icon = icon

    @property
    def available(self) -> bool:
        vehicle = self.vehicle
        return (
            vehicle is not None
            and vehicle.subscribed
            and vehicle.supports_command(self._command)
        )

    async def async_press(self) -> None:
        """Send the command and schedule a status poll."""
        vehicle = self.vehicle
        if vehicle is None:
            return
        await vehicle.send_command(self._command)
        self._schedule_refresh()


class ToyotaRefreshButton(ToyotaButtonBase):
    """Request fresh vehicle status."""

    _attr_icon = "mdi:refresh"

    @property
    def available(self) -> bool:
        vehicle = self.vehicle
        return vehicle is not None and vehicle.subscribed

    async def async_press(self) -> None:
        """Request a vehicle refresh and schedule a status poll."""
        vehicle = self.vehicle
        if vehicle is None:
            return
        await vehicle.poll_vehicle_refresh()
        self._schedule_refresh()
