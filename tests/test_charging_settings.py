"""Reported charging choices and confirmed settings changes."""

import asyncio
import json
import types
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from common import entity_id, make_24mm_vehicle
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na import patch_client
from custom_components.toyota_na.charging_helpers import (
    CHARGE_SETTINGS,
    charge_options,
    current_charge_option,
)
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    VehicleFeatures,
)

CHARGING = {
    "lastUpdateDateTime": "2026-09-14T12:00:00Z",
    "limitSelectionValues": ["Full", "90%", "80%"],
    "chargeSettings": {
        "targetLimit": {"value": "80", "unit": "%"},
        "maxACCurrent": {"value": "Max", "setting": "127"},
        "maxDCPower": {"value": "50kW", "setting": "50"},
        "electricSupplyModeLimit": {"value": "30%", "setting": "30"},
        "acCurrentSelections": [
            {"key": "Max", "enabled": True},
            {"key": "8A", "enabled": True},
            {"key": "16A", "enabled": False},
            {"key": "Broken", "enabled": True},
        ],
        "dcPowerSelections": [{"key": "Max", "enabled": True}, {"key": "50kW", "enabled": True}],
        "electricSupplyLimitSelections": [{"key": "30%", "enabled": True}],
    },
}


def status(charging=CHARGING):
    return {"electric": {"charging": deepcopy(charging)}}


def select_id(hass, field):
    return entity_id(hass, "select", f"TESTVIN24.{CHARGE_SETTINGS[field][0]}")


def select_unique_ids(hass, entry):
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == "select"
    }


async def push(hass, account):
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()


async def select_option(hass, select, option):
    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": option}, blocking=True
    )


async def test_charge_select_reports_validation_and_operation_failures(
    hass, setup_vehicles, subtests
):
    vehicle = make_24mm_vehicle()
    vehicle.apply_graphql_status(status())
    vehicle.set_charge_setting = AsyncMock()
    account = await setup_vehicles([vehicle])
    select = select_id(hass, "targetLimit")
    data = dict(account.entry.data)
    for error, expected_type in (
        (
            ValueError("This charging option is unavailable for this vehicle."),
            ServiceValidationError,
        ),
        (
            RuntimeError("Toyota rejected the charging change."),
            HomeAssistantError,
        ),
    ):
        vehicle.set_charge_setting.side_effect = error
        with subtests.test(error=type(error)):
            with pytest.raises(expected_type) as raised:
                await select_option(hass, select, "90%")
            assert str(raised.value) == str(error)
    assert hass.states.get(select).state == "80%"
    assert account.entry.data == data
    # Home Assistant rejects an option the select doesn't offer before the entity sees it.
    with pytest.raises(ServiceValidationError) as raised:
        await select_option(hass, select, "invalid")
    assert raised.value.translation_key == "not_valid_option"
    assert vehicle.set_charge_setting.await_count == 2


async def test_missing_charging_response_is_an_operational_error(hass, setup_vehicles):
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(return_value={}),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle.apply_graphql_status(status())
    await setup_vehicles([vehicle])
    with pytest.raises(
        HomeAssistantError, match="Toyota did not return charging preferences"
    ) as raised:
        await select_option(hass, select_id(hass, "targetLimit"), "90%")
    assert type(raised.value) is HomeAssistantError
    client.update_charge_settings.assert_not_awaited()


async def test_choices_follow_enabled_metadata_and_special_wire_values():
    vehicle = make_24mm_vehicle()
    vehicle.apply_graphql_status(status())
    settings = vehicle.charge_settings
    assert charge_options(settings, "targetLimit") == {"100%": 100, "90%": 90, "80%": 80}
    assert charge_options(settings, "maxACCurrent") == {"Max": 127, "8 A": 8}
    assert charge_options(settings, "maxDCPower") == {"Max": 1, "50 kW": 50}
    assert charge_options(settings, "electricSupplyModeLimit") == {"30%": 30}
    assert current_charge_option(settings, "maxACCurrent") == "Max"
    assert current_charge_option(settings, "maxDCPower") == "50 kW"
    assert current_charge_option(settings, "targetLimit") == "80%"
    assert current_charge_option(settings, "electricSupplyModeLimit") == "30%"


async def test_default_target_choices_require_reported_support():
    assert charge_options({}, "targetLimit") == {}
    assert charge_options({"targetLimit": {"value": 80}}, "targetLimit") == {
        f"{value}%": value for value in range(100, 39, -10)
    }


async def test_missing_target_uses_explicit_support_and_preserves_unknown_state(
    hass, setup_vehicles, subtests
):
    vehicle = make_24mm_vehicle()
    vehicle._feature_flags = {"chargeSetting": 1}
    account = await setup_vehicles([vehicle])
    assert select_unique_ids(hass, account.entry) == {"TESTVIN24.Charge Limit"}
    select = select_id(hass, "targetLimit")
    state = hass.states.get(select)
    assert state.state == "unknown"
    assert state.attributes["options"] == [f"{value}%" for value in range(100, 39, -10)]

    for flags in (
        None,
        {},
        {"chargeSetting": 0},
        {"chargeSetting": 2},
        {"chargeSetting": True},
        {"chargeSetting": "1"},
    ):
        with subtests.test(flags=flags):
            vehicle._feature_flags = flags
            await push(hass, account)
            assert hass.states.get(select).state == "unavailable"

    vehicle._feature_flags = {"chargeSetting": 1}
    vehicle.apply_graphql_status(
        status({"limitSelectionValues": ["Full", "80%"], "chargeSettings": {"targetLimit": None}})
    )
    await push(hass, account)
    state = hass.states.get(select)
    assert state.attributes["options"] == ["100%", "80%"]
    assert state.state == "unknown"
    assert entity_id(hass, "sensor", "TESTVIN24.Charge Target") is None
    vehicle._generation = ApiVehicleGeneration.MM21
    await push(hass, account)
    assert hass.states.get(select).state == "unavailable"
    vehicle._generation = ApiVehicleGeneration.BEV26
    await push(hass, account)
    assert hass.states.get(select).state == "unknown"
    vehicle._has_remote_subscription = False
    await push(hass, account)
    assert hass.states.get(select).state == "unavailable"


async def test_missing_target_writes_use_fresh_choices_and_report_only_readback():
    missing = status({"limitSelectionValues": ["Full", "90%"], "chargeSettings": {}})
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(return_value=missing),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle._feature_flags = {"chargeSetting": 1}
    await vehicle.set_charge_setting("targetLimit", "90%")
    client.update_charge_settings.assert_awaited_once_with(
        vehicle.vin, "chargingTargetLimit", 90, vehicle.region
    )
    assert current_charge_option(vehicle.charge_settings, "targetLimit") is None
    assert VehicleFeatures.ChargeTargetLimit not in vehicle.features

    client.update_charge_settings.reset_mock()
    with pytest.raises(ValueError, match="unavailable"):
        await vehicle.set_charge_setting("targetLimit", "80%")
    client.update_charge_settings.assert_not_awaited()

    updated = status({"chargeSettings": {"targetLimit": {"value": "90", "unit": "%"}}})
    client.graphql_get_vehicle_status.side_effect = [missing, updated]
    await vehicle.set_charge_setting("targetLimit", "90%")
    assert current_charge_option(vehicle.charge_settings, "targetLimit") == "90%"


async def test_missing_target_cannot_be_written_without_explicit_support_or_fresh_charging(
    subtests,
):
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    for flags, response in (
        (None, status({"chargeSettings": {}})),
        ({"chargeSetting": 1}, {"telemetry": {"odo": {"value": 100}}}),
        ({"chargeSetting": 1}, status({})),
    ):
        with subtests.test(flags=flags, response=response):
            vehicle._feature_flags = flags
            client.graphql_get_vehicle_status.return_value = response
            with pytest.raises(ValueError, match="unavailable"):
                await vehicle.set_charge_setting("targetLimit", "90%")
    client.update_charge_settings.assert_not_awaited()


async def test_selects_appear_only_on_supported_vehicles(hass, setup_vehicles):
    vehicle = make_24mm_vehicle()
    account = await setup_vehicles([vehicle])
    assert select_unique_ids(hass, account.entry) == set()
    vehicle.apply_graphql_status(status())
    await push(hass, account)
    assert select_unique_ids(hass, account.entry) == {
        f"TESTVIN24.{name}" for name, *_ in CHARGE_SETTINGS.values()
    }
    selects = [select_id(hass, field) for field in CHARGE_SETTINGS]

    def available():
        return [hass.states.get(select).state != "unavailable" for select in selects]

    assert all(available())
    vehicle._generation = ApiVehicleGeneration.MM21
    await push(hass, account)
    assert not any(available())
    vehicle._generation = ApiVehicleGeneration.BEV26
    await push(hass, account)
    assert all(available())
    vehicle._has_remote_subscription = False
    await push(hass, account)
    assert not any(available())


async def test_update_validates_fresh_choices_and_reads_back_confirmed_state():
    updated = status()
    updated["electric"]["charging"]["chargeSettings"]["targetLimit"]["value"] = "90"
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(side_effect=[status(), updated]),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle.apply_graphql_status(status())
    await vehicle.set_charge_setting("targetLimit", "90%")
    client.update_charge_settings.assert_awaited_once_with(
        vehicle.vin, "chargingTargetLimit", 90, vehicle.region
    )
    assert current_charge_option(vehicle.charge_settings, "targetLimit") == "90%"

    fresh = status()
    fresh["electric"]["charging"]["chargeSettings"]["acCurrentSelections"][0]["enabled"] = False
    client.graphql_get_vehicle_status.side_effect = [fresh]
    client.update_charge_settings.reset_mock()
    with pytest.raises(ValueError, match="unavailable"):
        await vehicle.set_charge_setting("maxACCurrent", "Max")
    client.update_charge_settings.assert_not_awaited()


async def test_charge_and_power_supply_controls_follow_their_own_feature_flags(
    hass, setup_vehicles, subtests
):
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(return_value=status()),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle.apply_graphql_status(status())
    account = await setup_vehicles([vehicle])
    for field, feature, option in (
        ("targetLimit", "chargeSetting", "90%"),
        ("maxACCurrent", "chargeSetting", "Max"),
        ("maxDCPower", "chargeSetting", "Max"),
        ("electricSupplyModeLimit", "powerSupply", "30%"),
    ):
        select = select_id(hass, field)
        for value in (0, 2, None, True, "1"):
            with subtests.test(field=field, value=value):
                vehicle._feature_flags = {
                    "remoteCommands": 1,
                    "chargeSetting": 1,
                    "powerSupply": 1,
                    feature: value,
                }
                await push(hass, account)
                assert hass.states.get(select).state == "unavailable"
                with pytest.raises(ValueError, match="unavailable"):
                    await vehicle.set_charge_setting(field, option)
        vehicle._feature_flags = {"remoteCommands": 2, feature: 1}
        await push(hass, account)
        assert hass.states.get(select).state != "unavailable"
        await vehicle.set_charge_setting(field, option)
    assert client.update_charge_settings.await_count == 4
    assert client.graphql_get_vehicle_status.await_count == 8


async def test_disabled_charging_controls_preserve_returned_readings(hass, setup_vehicles):
    vehicle = make_24mm_vehicle()
    vehicle._feature_flags = {"remoteCommands": 1, "chargeSetting": 2, "powerSupply": 2}
    vehicle.apply_graphql_status(status())
    account = await setup_vehicles([vehicle])
    assert select_unique_ids(hass, account.entry) == set()
    assert current_charge_option(vehicle.charge_settings, "targetLimit") == "80%"
    assert hass.states.get(entity_id(hass, "sensor", "TESTVIN24.Charge Target")).state == "80"


async def test_failure_does_not_optimistically_change_setting():
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(return_value=status()),
        update_charge_settings=AsyncMock(side_effect=RuntimeError("Vehicle rejected setting")),
    )
    vehicle = make_24mm_vehicle(client)
    with pytest.raises(RuntimeError, match="rejected"):
        await vehicle.set_charge_setting("targetLimit", "90%")
    assert current_charge_option(vehicle.charge_settings, "targetLimit") == "80%"


async def test_partial_read_without_fresh_settings_cannot_change_cached_choices():
    client = types.SimpleNamespace(
        graphql_get_vehicle_status=AsyncMock(return_value={"telemetry": {"odo": {"value": 100}}}),
        update_charge_settings=AsyncMock(),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle.apply_graphql_status(status())
    with pytest.raises(ValueError, match="unavailable"):
        await vehicle.set_charge_setting("targetLimit", "90%")
    client.update_charge_settings.assert_not_awaited()


async def test_older_or_partial_settings_cannot_erase_newer_choices():
    vehicle = make_24mm_vehicle()
    vehicle.apply_graphql_status(status())
    old = status()
    old["electric"]["charging"]["lastUpdateDateTime"] = "2026-09-14T11:00:00Z"
    old["electric"]["charging"]["limitSelectionValues"] = ["100"]
    vehicle.apply_graphql_status(old)
    vehicle.apply_graphql_status(
        {"electric": {"charging": {"chargeSettings": {"targetLimit": None}}}}
    )
    assert len(charge_options(vehicle.charge_settings, "targetLimit")) == 3
    replacement = make_24mm_vehicle()
    replacement.inherit_state(vehicle)
    assert replacement.charge_settings is vehicle.charge_settings


async def test_power_supply_stop_is_available_only_while_active():
    client = types.SimpleNamespace(remote_request_24mm=AsyncMock())
    vehicle = make_24mm_vehicle(client)
    for state in ("external_power_active", "external_power_active_hybrid"):
        vehicle.apply_graphql_status({"electric": {"charging": {"chargingState": state}}})
        await vehicle.send_command(RemoteRequestCommand.PowerSupplyStop)
        client.remote_request_24mm.assert_awaited_with(
            vehicle.vin, "power-supply-stop", vehicle.region
        )
    vehicle.apply_graphql_status({"electric": {"charging": {"chargingState": "charging"}}})
    assert not vehicle.supports_command(RemoteRequestCommand.PowerSupplyStop)


# PostChargeSettings never selects requestNo, so its first callback completes it.
@pytest.mark.parametrize(
    ("variable", "value", "operation", "key", "variables", "payload", "stage"),
    [
        (
            "currentCharge",
            127,
            "PostChargeSettings",
            "postChargeSettings",
            {"currentCharge": 127},
            {"correlationId": "correlation"},
            3,
        ),
        (
            "minimumElectricSupply",
            30,
            "PostPowerSupplyModeLimit",
            "executeRemoteCommand",
            {"command": "set-power-supply", "minimumElectricSupply": "30"},
            {"correlationId": "correlation", "requestNo": 42},
            4,
        ),
    ],
    ids=["currentCharge", "minimumElectricSupply"],
)
async def test_setting_mutations_wait_for_callback_after_subscription(
    variable, value, operation, key, variables, payload, stage
):
    websocket = _WebSocket()

    async def mutate(*args, **kwargs):
        assert websocket.stage == 2
        assert args[0] == operation
        assert args[2] == variables
        assert kwargs["region"] == "CA"
        return {key: {"payload": payload}}

    client = types.SimpleNamespace(auth=_Auth(), graphql_request=AsyncMock(side_effect=mutate))
    with patch.object(
        patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)
    ):
        result = await patch_client.update_charge_settings(
            client, "TESTVIN24", variable, value, "CA"
        )
    assert result["status"] == "COMPLETED"
    assert websocket.stage == stage


async def test_commands_for_one_vehicle_are_serialized():
    client = types.SimpleNamespace()
    active = 0
    peak = 0

    async def execute(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        await asyncio.sleep(0)
        active -= 1

    with patch.object(patch_client, "_execute_appsync_operation", side_effect=execute):
        await asyncio.gather(
            *(patch_client._run_appsync_operation(client, "TESTVIN", None, "CA") for _ in range(3))
        )
    assert peak == 1


# AppSync transport doubles, copied from the transport tests.
class _Auth:
    async def get_access_token(self):
        return "token"

    async def get_guid(self):
        return "guid"

    def get_device_id(self):
        return "device"


class _Message:
    type = aiohttp.WSMsgType.TEXT

    def __init__(self, body):
        self.data = json.dumps(body)


class _WebSocket:
    def __init__(self):
        self.sent = []
        self.subscription_id = None
        self.stage = 0

    async def send_json(self, value):
        self.sent.append(value)
        if value.get("type") == "start":
            self.subscription_id = value["id"]

    async def receive(self):
        if self.stage == 0:
            body = {"type": "connection_ack"}
        elif self.stage == 1:
            body = {"type": "start_ack", "id": self.subscription_id}
        else:
            request_no = 41 if self.stage == 2 else 42
            body = {
                "type": "data",
                "id": self.subscription_id,
                "payload": {
                    "data": {
                        "onPostRemoteCallback": {
                            "vin": "TESTVIN24",
                            "appRequestNo": request_no,
                            "status": "COMPLETED",
                            "commandEnded": True,
                        }
                    }
                },
            }
        self.stage += 1
        return _Message(body)


class _SocketContext:
    def __init__(self, websocket):
        self.websocket = websocket

    async def __aenter__(self):
        return self.websocket

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _WebSocketSession:
    def __init__(self, websocket):
        self.websocket = websocket
        self.url = None
        self.protocols = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def ws_connect(self, url, protocols, heartbeat):
        self.url = url
        self.protocols = protocols
        return _SocketContext(self.websocket)
