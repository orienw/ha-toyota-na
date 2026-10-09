"""Fixtures that set up the integration in a real Home Assistant."""

from contextlib import ExitStack
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from common import FakeClient, FakeWebSocket, account_entry, set_up
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""


@dataclass
class Account:
    entry: MockConfigEntry
    coordinator: DataUpdateCoordinator
    client: FakeClient
    get_vehicles: AsyncMock
    websocket: FakeWebSocket


@pytest.fixture
async def setup_vehicles(hass, caplog):
    """Return a function that sets up an account whose vehicle list is the given vehicles.

    The patches last until teardown, since every coordinator refresh reads the list again.
    """
    with ExitStack() as stack:

        async def setup(vehicles, *, options=None, data=None) -> Account:
            get_vehicles = AsyncMock(return_value=vehicles)
            stack.enter_context(patch("custom_components.toyota_na.get_vehicles", get_vehicles))
            stack.enter_context(patch("custom_components.toyota_na.ToyotaOneClient", FakeClient))
            stack.enter_context(
                patch("custom_components.toyota_na.ToyotaWebSocketHandler", FakeWebSocket)
            )
            stack.enter_context(patch("custom_components.toyota_na.COMMAND_REFRESH_DELAY", 0))
            # A wake interval of 0 keeps setup from waking the vehicle.
            entry = account_entry(options={CONF_WAKE_INTERVAL: 0, **(options or {})}, data=data)
            entry.add_to_hass(hass)
            await set_up(hass, entry, caplog)
            runtime = hass.data[DOMAIN][entry.entry_id]
            return Account(
                entry,
                runtime["coordinator"],
                runtime["toyota_na_client"],
                get_vehicles,
                runtime["ws_handler"],
            )

        yield setup
        # Unload while the patches are still active.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
