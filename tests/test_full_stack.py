"""The real integration against a fake Toyota cloud, faked only at the aiohttp session.

Other integration tests replace the client, vehicles or WebSocket handler. These run all of them,
so they catch the seams between those parts: sign-in recovery, reauthentication and live updates.
"""

from contextlib import ExitStack
from unittest.mock import patch

import payloads
import pytest
from common import account_entry, entity_id, set_up
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from toyota_cloud import ToyotaCloud

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL

APPSYNC_EV = payloads.vehicle_entry("24MM", True)
APPSYNC_GAS = payloads.vehicle_entry("24MM", False)
REST_GAS = payloads.vehicle_entry("21MM", False)
LEGACY_GAS = payloads.vehicle_entry("17CY", False)


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


def odometer(hass, vehicle):
    return hass.states.get(entity_id(hass, "sensor", f"{vehicle['vin']}.Odometer")).state


def reauth_started(hass):
    return any(
        flow["context"]["source"] == "reauth" for flow in hass.config_entries.flow.async_progress()
    )


async def poll(hass, entry):
    await entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()


def pushed_status(vin, odometer_miles):
    status = payloads.graphql_status(0, vin)
    status["lastUpdateDateTime"] = "2026-09-15T13:00:00Z"
    status["telemetry"]["lastUpdateDateTime"] = "2026-09-15T13:00:00Z"
    status["telemetry"]["odo"] = {"value": odometer_miles, "unit": "mi"}
    return status


async def test_appsync_vehicle_reads_and_follows_live_updates(hass, toyota):
    cloud, _ = await toyota([APPSYNC_EV])
    assert odometer(hass, APPSYNC_EV) == "12345"
    assert not cloud.socket.closed
    assert APPSYNC_EV["vin"] in cloud.subscriptions
    assert cloud.requested(operation="ConfirmSubscriptionStatus")

    cloud.push_status(APPSYNC_EV["vin"], pushed_status(APPSYNC_EV["vin"], 20000))
    await hass.async_block_till_done()
    assert odometer(hass, APPSYNC_EV) == "20000"


async def test_rest_vehicle_reads_into_entities(hass, toyota):
    cloud, entry = await toyota([REST_GAS])
    assert odometer(hass, REST_GAS) == "12345"
    door = entity_id(hass, "binary_sensor", f"{REST_GAS['vin']}.Front Driver Door")
    assert hass.states.get(door).state == "off"
    assert cloud.requested("/v1/remote/route/status")

    cloud.rest_status[REST_GAS["vin"]] = payloads.rest_status(1)
    cloud.telemetry[REST_GAS["vin"]] = payloads.telemetry(1)
    await poll(hass, entry)
    assert hass.states.get(door).state == "on"
    assert odometer(hass, REST_GAS) == "12346"


async def test_vehicle_without_live_updates_reads_without_a_socket(hass, toyota):
    cloud, _ = await toyota([LEGACY_GAS])
    assert odometer(hass, LEGACY_GAS) == "12345"
    door = entity_id(hass, "binary_sensor", f"{LEGACY_GAS['vin']}.Front Driver Door")
    assert hass.states.get(door).state == "off"
    assert cloud.requested("/v2/legacy/remote/status")
    assert cloud.sockets == []


async def test_revoked_access_token_is_refreshed_and_the_read_retried(hass, toyota):
    cloud, entry = await toyota([REST_GAS])
    cloud.revoke_access_token()
    cloud.telemetry[REST_GAS["vin"]] = payloads.telemetry(1)

    await poll(hass, entry)

    assert cloud.refreshes == 1
    assert entry.data["tokens"]["access_token"] == "access-1"
    assert odometer(hass, REST_GAS) == "12346"
    assert not reauth_started(hass)


async def test_failed_token_refresh_starts_reauth_and_stops_live_updates(hass, toyota):
    cloud, entry = await toyota([APPSYNC_EV])
    cloud.revoke_access_token(refresh_works=False)

    await poll(hass, entry)

    assert odometer(hass, APPSYNC_EV) == "unavailable"
    assert reauth_started(hass)
    assert cloud.socket.closed
    assert not entry.runtime_data.ws_handler.is_connected


async def test_vehicle_toyota_refuses_keeps_the_account_signed_in(hass, toyota):
    cloud, entry = await toyota([APPSYNC_EV, APPSYNC_GAS])
    cloud.refused_vins.add(APPSYNC_EV["vin"])
    cloud.graphql_status[APPSYNC_GAS["vin"]] = pushed_status(APPSYNC_GAS["vin"], 30000)

    await poll(hass, entry)

    # The refusal looks like a rejected token, so the client refreshes once, then gives up.
    assert cloud.refreshes == 1
    assert not reauth_started(hass)
    assert odometer(hass, APPSYNC_GAS) == "30000"
    assert odometer(hass, APPSYNC_EV) == "12345"
    assert not cloud.socket.closed


async def test_sign_in_failure_during_an_action_stops_live_updates(hass, toyota):
    cloud, entry = await toyota([APPSYNC_EV])
    cloud.revoke_access_token(refresh_works=False)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "lock",
            "lock",
            {"entity_id": entity_id(hass, "lock", f"{APPSYNC_EV['vin']}.")},
            blocking=True,
        )
    await hass.async_block_till_done()

    assert reauth_started(hass)
    assert odometer(hass, APPSYNC_EV) == "unavailable"
    assert cloud.socket.closed
    assert not entry.runtime_data.ws_handler.is_connected
