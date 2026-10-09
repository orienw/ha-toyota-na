"""Run the integration in a real Home Assistant with Toyota's cloud stubbed."""

from copy import deepcopy
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from common import EMAIL, STATUS_24MM, FakeClient, FakeWebSocket, account_entry, set_up, settle
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL, LAST_VEHICLE_WAKES

VIN = "TESTVIN24"
LOCK = "lock.2026_rav4_plug_in_hybrid"


@pytest.fixture
async def loaded(hass, caplog):
    # A wake interval of 0 keeps setup from waking the vehicle.
    entry = account_entry(options={CONF_WAKE_INTERVAL: 0})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.toyota_na.ToyotaOneClient", FakeClient),
        patch("custom_components.toyota_na.ToyotaWebSocketHandler", FakeWebSocket),
        patch("custom_components.toyota_na.COMMAND_REFRESH_DELAY", 0),
    ):
        await set_up(hass, entry, caplog)
        runtime = hass.data[DOMAIN][entry.entry_id]
        yield entry, runtime["toyota_na_client"], runtime["ws_handler"]
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_reads_the_vehicle_into_entities_and_unloads(hass, loaded):
    entry, _, websocket = loaded

    assert entry.state is ConfigEntryState.LOADED
    assert hass.states.get(LOCK).state == "unlocked"
    assert hass.states.get("sensor.2026_rav4_plug_in_hybrid_odometer").state == "1234"
    assert hass.states.get("binary_sensor.2026_rav4_plug_in_hybrid_front_passenger_door").state == (
        "on"
    )
    websocket.start.assert_awaited_once_with({VIN: {"region": "CA", "backdoor_type": "hatch"}})

    assert await hass.config_entries.async_unload(entry.entry_id)

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert entry.entry_id not in hass.data[DOMAIN]
    websocket.stop.assert_awaited_once()
    assert hass.states.get(LOCK).state == "unavailable"


async def test_pushed_status_updates_entities(hass, loaded):
    _, _, websocket = loaded
    status = deepcopy(STATUS_24MM)
    status["lastUpdateDateTime"] = status["vehicleState"]["lastUpdateDateTime"] = (
        "2026-08-14T12:05:00Z"
    )
    status["vehicleState"]["doors"]["passengerSide"]["lock"]["status"] = "lock"

    websocket.on_status(VIN, status)
    await hass.async_block_till_done()

    assert hass.states.get(LOCK).state == "locked"


async def test_service_finds_the_vehicle_through_its_device(hass, loaded):
    entry, client, _ = loaded
    device_id = er.async_get(hass).async_get(LOCK).device_id

    await hass.services.async_call(DOMAIN, "door_lock", {"vehicle": device_id}, blocking=True)
    await settle(hass)

    client.remote_request_24mm.assert_awaited_once_with(VIN, ANY, "CA")
    assert [wake["vin"] for wake in entry.data[LAST_VEHICLE_WAKES]] == [VIN]
    # The command is followed by a fresh read of the account.
    assert client.get_user_vehicle_list.await_count == 2


async def test_sign_in_with_a_one_time_code_creates_the_account(hass):
    client = MagicMock()
    client.auth.authorize = AsyncMock(side_effect=[{"otp": True}, "test-authorization"])
    client.auth.request_tokens = AsyncMock()
    client.auth.get_id_info = AsyncMock(return_value={"email": EMAIL})
    client.auth.get_tokens = MagicMock(return_value={"access_token": "test-access"})

    with (
        patch("custom_components.toyota_na.config_flow.ToyotaOneClient", return_value=client),
        patch("custom_components.toyota_na.async_setup_entry", AsyncMock(return_value=True)),
        patch("custom_components.toyota_na.async_unload_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "user")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"username": EMAIL, "password": "test-password"}
        )
        assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "otp")
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"code": "123456"}
        )
        await hass.async_block_till_done()
        assert result["type"] is FlowResultType.CREATE_ENTRY
        # Setup was stubbed, so unload with the stub before the real unload sees the entry.
        assert await hass.config_entries.async_unload(result["result"].entry_id)

    assert result["title"] == EMAIL
    assert result["data"] == {
        "tokens": {"access_token": "test-access"},
        "email": EMAIL,
        "username": EMAIL,
    }
    assert result["result"].unique_id == f"{DOMAIN}:{EMAIL}"
    client.auth.request_tokens.assert_awaited_once_with("test-authorization")


async def test_options_save_the_wake_interval(hass):
    entry = account_entry()
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert (result["type"], result["step_id"]) == (FlowResultType.FORM, "init")
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_WAKE_INTERVAL: "0"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_WAKE_INTERVAL: 0}
