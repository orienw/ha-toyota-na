"""Run the integration in a real Home Assistant with Toyota's cloud stubbed."""

import json
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.wake_policy import CONF_WAKE_INTERVAL, LAST_VEHICLE_WAKES

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

VIN = "TESTVIN24"
EMAIL = "owner@example.com"
STATUS = json.loads((Path(__file__).parents[1] / "fixtures/vehicle_24mm.json").read_text())
VEHICLE = {
    "vin": VIN,
    "modelYear": "2026",
    "modelName": "RAV4 PLUG-IN HYBRID",
    "generation": "24MM",
    "brand": "T",
    "region": "CA",
    "subscriptionStatus": "subscribed",
    "remoteSubscriptionExists": True,
    "remoteServiceCapabilities": {"estartStopCapable": True, "dlockUnlockCapable": True},
    "extendedCapabilities": {"remoteEngineStartStop": True, "doorLockUnlockCapable": True},
    "backdoorType": "hatch",
    "fuelType": "I",
    "evVehicle": False,
}
LOCK = "lock.2026_rav4_plug_in_hybrid"


class FakeClient:
    """Answer for one 24MM vehicle. Every other read returns nothing."""

    def __init__(self, auth):
        self.auth = auth
        self.calls = {}
        self.get_user_vehicle_list = AsyncMock(return_value=[VEHICLE])

    async def graphql_get_vehicle_status(self, vin, backdoor_type, region):
        return STATUS

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.calls.setdefault(name, AsyncMock(return_value=None))


class FakeWebSocket:
    def __init__(self, client, on_status):
        self.on_status = on_status
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.update_vehicle_contexts = AsyncMock()

    def get_cached_status(self, vin):
        return None


def account_entry(**kwargs):
    now = time.time()
    tokens = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "id_token": "test-id",
        "expires_at": now + 3600,
        "updated_at": now,
        "guid": "test-guid",
        "clock": "unix",
    }
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=f"{DOMAIN}:{EMAIL}",
        data={"tokens": tokens, "device_id": "test-device", "email": EMAIL, "username": EMAIL},
        **kwargs,
    )


@pytest.fixture
async def loaded(hass):
    # A wake interval of 0 keeps setup from waking the vehicle.
    entry = account_entry(options={CONF_WAKE_INTERVAL: 0})
    entry.add_to_hass(hass)
    with (
        patch("custom_components.toyota_na.ToyotaOneClient", FakeClient),
        patch("custom_components.toyota_na.ToyotaWebSocketHandler", FakeWebSocket),
        patch("custom_components.toyota_na.COMMAND_REFRESH_DELAY", 0),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
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
    status = deepcopy(STATUS)
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
    await hass.async_block_till_done()

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
