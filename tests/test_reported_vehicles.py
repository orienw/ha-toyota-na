"""The integration against what Toyota returned for reporters' vehicles.

Every other test answers from payloads written from the app. These answer from diagnostics
downloads, saved by `diagnostics_fixture`, so they show what real responses turn into.
"""

import json
from functools import partial
from unittest.mock import patch

import payloads
import pytest
from common import FakeWebSocket, account_entry, set_up
from diagnostics_fixture import FIXTURES, ReportedClient, fixture_from_download
from ha_helpers import get_diagnostics_for_config_entry
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL


def entity_states(hass, entry) -> dict[str, str]:
    """Each entity's state and unit, by vehicle position and name so VINs don't matter."""
    vins = [vehicle.vin for vehicle in entry.runtime_data.coordinator.data]
    states = {}
    for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        vin, _, name = item.unique_id.partition(".")
        state = hass.states.get(item.entity_id)
        unit = state.attributes.get("unit_of_measurement", "") if state else ""
        states[f"{vins.index(vin)} {item.domain} {name}"] = (
            f"{state.state} {unit}".strip() if state else "disabled"
        )
    return states


async def replay(hass, fixture, caplog):
    """Set up an account whose cloud answers from the fixture."""
    with (
        patch("custom_components.toyota_na.ToyotaOneClient", partial(ReportedClient, fixture)),
        patch("custom_components.toyota_na.ToyotaWebSocketHandler", FakeWebSocket),
    ):
        entry = account_entry(options={**fixture["options"], CONF_WAKE_INTERVAL: 0})
        entry.add_to_hass(hass)
        await set_up(hass, entry, caplog)
    return entry


async def test_a_download_replays_into_the_reporters_entities(hass, hass_client, toyota, caplog):
    _, entry = await toyota(
        [
            payloads.vehicle_entry("24MM", True),
            payloads.vehicle_entry("21MM", False),
            payloads.vehicle_entry("17CY", True),
        ]
    )
    reported = entity_states(hass, entry)
    download = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    await hass.config_entries.async_remove(entry.entry_id)

    fixture = fixture_from_download({"data": download})
    replayed = entity_states(hass, await replay(hass, fixture, caplog))

    def comparable(states):
        # The tire pressure endpoint is not in diagnostics, so a fixture reads tires from telemetry.
        return {name: state for name, state in states.items() if "Tire" not in name}

    assert comparable(reported)
    assert comparable(replayed) == comparable(reported)


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.stem)
async def test_reported_vehicle(hass, path, snapshot, freezer, caplog):
    freezer.move_to("2027-01-01T12:00:00Z")
    entry = await replay(hass, json.loads(path.read_text()), caplog)
    states = entity_states(hass, entry)
    assert [f"{name}: {state}" for name, state in sorted(states.items())] == snapshot
