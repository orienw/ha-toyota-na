"""Fixtures that set up the integration in a real Home Assistant."""

from contextlib import ExitStack
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import pytest
from common import FakeClient, FakeWebSocket, account_entry, set_up
from ha_helpers import HomeAssistantSnapshotExtension, MockConfigEntry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from syrupy.assertion import SnapshotAssertion
from toyota_cloud import ToyotaCloud

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration from custom_components."""


@pytest.fixture
def snapshot(snapshot: SnapshotAssertion) -> SnapshotAssertion:
    """Store snapshots in tests/snapshots on every supported Home Assistant version."""
    return snapshot.use_extension(HomeAssistantSnapshotExtension)


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
            # Poll right after a command. Buttons and locks import their own copy of the delay.
            for module in ("", ".button", ".lock"):
                stack.enter_context(
                    patch(f"custom_components.toyota_na{module}.COMMAND_REFRESH_DELAY", 0)
                )
            # A wake interval of 0 keeps setup from waking the vehicle.
            entry = account_entry(options={CONF_WAKE_INTERVAL: 0, **(options or {})}, data=data)
            entry.add_to_hass(hass)
            await set_up(hass, entry, caplog)
            runtime = entry.runtime_data
            return Account(
                entry,
                runtime.coordinator,
                runtime.client,
                get_vehicles,
                runtime.ws_handler,
            )

        yield setup
        # Unload while the patches are still active.
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.fixture
async def toyota(hass, caplog):
    """Set up an account against a fake cloud serving the given vehicles."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    with ExitStack() as stack:

        async def setup(vehicles):
            cloud = ToyotaCloud(vehicles)
            stack.enter_context(
                patch(
                    "custom_components.toyota_na.async_create_clientsession",
                    return_value=cloud.session,
                )
            )
            for module in ("", ".button", ".lock"):
                stack.enter_context(
                    patch(f"custom_components.toyota_na{module}.COMMAND_REFRESH_DELAY", 0)
                )
            entry = account_entry(options={CONF_WAKE_INTERVAL: 0})
            entry.add_to_hass(hass)
            await set_up(hass, entry, caplog)
            return cloud, entry

        yield setup
        for entry in hass.config_entries.async_entries(DOMAIN):
            if entry.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(entry.entry_id)
