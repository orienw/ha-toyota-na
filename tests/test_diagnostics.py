"""Ensure diagnostic payloads use exact account identifier redaction keys."""

from unittest.mock import AsyncMock, MagicMock

import aiohttp
import pytest
from common import FakeVehicle
from homeassistant.components.diagnostics import REDACTED
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)


@pytest.fixture
def diagnostics(hass, hass_client, setup_vehicles):
    """Return a function that sets up an account and downloads its diagnostics."""

    async def download(vehicle_list, vehicles=(), **responses):
        account = await setup_vehicles(list(vehicles))
        account.client.get_user_vehicle_list.return_value = vehicle_list
        for name, response in responses.items():
            setattr(account.client, name, AsyncMock(return_value=response))
        return account, await get_diagnostics_for_config_entry(hass, hass_client, account.entry)

    return download


async def test_routed_generations_keep_their_context_in_diagnostics(diagnostics):
    vehicles = [
        {"vin": f"TEST{generation}", "generation": generation, "brand": "T", "region": "CA"}
        for generation in ("NG86", "GR86")
    ]
    account, result = await diagnostics(
        vehicles,
        get_vehicle_status_route={"vehicleStatus": []},
        get_engine_status_route={"status": "stopped"},
        get_telemetry={},
        get_electric_status={},
    )
    client = account.client
    assert result["vehicle_status"]["data"] == [{"vehicleStatus": []}] * 2
    assert result["engine_status"]["data"] == [{"status": "stopped"}] * 2
    for generation in ("NG86", "GR86"):
        client.get_vehicle_status_route.assert_any_await(f"TEST{generation}", generation, "CA", "T")
        client.get_engine_status_route.assert_any_await(f"TEST{generation}", generation, "CA", "T")
        client.get_telemetry.assert_any_await(f"TEST{generation}", "CA", generation)


async def test_raw_vehicle_and_config_data_use_account_identifier_redaction(
    hass, hass_client, setup_vehicles
):
    vehicle = {
        "vin": "vin",
        "generation": "24MM",
        "remoteUserGuid": "remote-user",
        "subscriberGuid": "subscriber",
        "accountInfoId": "account",
        "modelName": "RAV4",
    }
    account = await setup_vehicles([])
    account.client.get_user_vehicle_list.return_value = [vehicle]
    account.client.graphql_get_vehicle_status = AsyncMock(return_value={})
    account.client.get_telemetry = AsyncMock(return_value={})
    # Setup drops a saved password, so put one back to show the report would hide it.
    hass.config_entries.async_update_entry(
        account.entry, data={**account.entry.data, "password": "secret"}
    )

    result = await get_diagnostics_for_config_entry(hass, hass_client, account.entry)

    config = result["config_entry"]
    for key in ("email", "password", "username", "device_id"):
        assert config[key] == REDACTED
    assert config["tokens"]["guid"] == REDACTED
    assert result["vehicle_list"]["data"] == [
        {
            "vin": REDACTED,
            "generation": "24MM",
            "remoteUserGuid": REDACTED,
            "subscriberGuid": REDACTED,
            "accountInfoId": REDACTED,
            "modelName": "RAV4",
        }
    ]


async def test_climate_schedules_come_from_the_last_poll(diagnostics):
    schedules = {
        "returnCode": "ONE-RES-10000",
        "airConditioningReservation": [{"reservationNo": 1, "date": "09-22-2026", "time": "06:30"}],
    }
    scheduled = FakeVehicle(set(), vin="SCHEDULED")
    scheduled.climate_schedules = schedules
    _, result = await diagnostics(
        [{"vin": vin, "generation": "24MM"} for vin in ("SCHEDULED", "OTHER")],
        [scheduled, FakeVehicle(set(), vin="OTHER")],
        graphql_get_vehicle_status={},
        get_telemetry={},
    )
    assert result["climate_schedules"]["data"] == [schedules, None]


async def test_vehicle_health_comes_from_the_last_poll(diagnostics):
    read = FakeVehicle(set(), vin="READ")
    read.health = {"read_at": 1000.0, "report": {"vehicleStatus": {}}, "campaigns": []}
    other = FakeVehicle(set(), vin="OTHER")
    other.health = {"read_at": 1000.0}
    _, result = await diagnostics(
        [{"vin": vin, "generation": "24MM"} for vin in ("READ", "OTHER")],
        [read, other],
        graphql_get_vehicle_status={},
        get_telemetry={},
    )
    assert result["vehicle_health"]["data"] == [
        {"report": {"vehicleStatus": {}}, "campaigns": []},
        None,
    ]


async def test_notification_history_keeps_only_structured_fields(diagnostics):
    item = {
        "messageId": "1",
        "category": "RemoteCommand",
        "displayCategory": "Remote",
        "subcategory": None,
        "type": "alert",
        "status": "completed",
        "notificationDate": "2026-10-06T07:00:00Z",
        "isRead": False,
        "title": "Doors locked",
        "message": "Locked at 1 Main St",
        "iconUrl": "https://example.com",
        "lat": 37.1,
        "lon": -122.1,
        "vin": "READ",
        "readTimestamp": None,
    }
    read = FakeVehicle(set(), vin="READ")
    read.notifications = [item]
    _, result = await diagnostics(
        [{"vin": vin, "generation": "24MM"} for vin in ("READ", "UNREAD")],
        [read, FakeVehicle(set(), vin="UNREAD")],
        # Location fields are redacted wherever they appear in the report.
        graphql_get_vehicle_status={"lat": 37.1, "lon": -122.1},
        get_telemetry={},
    )
    assert result["notification_history"]["data"] == [
        [
            {
                "messageId": "1",
                "category": "RemoteCommand",
                "displayCategory": "Remote",
                "subcategory": None,
                "type": "alert",
                "status": "completed",
                "notificationDate": "2026-10-06T07:00:00Z",
                "isRead": False,
            }
        ],
        None,
    ]
    assert result["vehicle_status"]["data"] == [{"lat": REDACTED, "lon": REDACTED}] * 2


def failure(error, code=None):
    if code is not None:
        error.response_code = code
    return AsyncMock(side_effect=error)


def forbidden(message):
    return aiohttp.ClientResponseError(
        MagicMock(real_url="https://example.invalid/oneapi/v2/vehicle/guid"),
        (),
        status=403,
        message=message,
    )


@pytest.mark.parametrize(
    ("lookup", "vehicle_list"),
    [
        (
            failure(
                RuntimeError("No vehicles for JTHTESTVIN0000001 owner@example.com [ONE-VL-10002]"),
                "ONE-VL-10002",
            ),
            {"data": None, "error": "RuntimeError [ONE-VL-10002]"},
        ),
        (
            failure(
                forbidden("Denied for ABCDEF12-3456-7890-ABCD-EF1234567890 [APIGW-403]"),
                "APIGW-403",
            ),
            {"data": None, "error": "ClientResponseError 403 [APIGW-403]"},
        ),
        (failure(TimeoutError()), {"data": None, "error": "TimeoutError"}),
        # Code-shaped text in Toyota's message is never exported.
        (
            failure(forbidden("Account [ONE-ACCOUNT-12345]")),
            {"data": None, "error": "ClientResponseError 403"},
        ),
        (
            failure(RuntimeError("Account [ACCOUNT-123456789012] [ONE-VL-10002]")),
            {"data": None, "error": "RuntimeError"},
        ),
        (
            failure(RuntimeError("Call us"), "PHONE-15555550123"),
            {"data": None, "error": "RuntimeError"},
        ),
        (AsyncMock(return_value=None), {"data": None}),
        (
            AsyncMock(
                return_value={
                    "status": {"messages": {"description": "Unavailable for owner@example.com"}}
                }
            ),
            {"data": None, "unexpected": "dict"},
        ),
    ],
)
async def test_report_survives_a_failed_or_unusable_vehicle_list(
    hass, hass_client, setup_vehicles, lookup, vehicle_list
):
    account = await setup_vehicles([])
    account.client.get_user_vehicle_list = lookup

    result = await get_diagnostics_for_config_entry(hass, hass_client, account.entry)

    assert result["vehicle_list"] == vehicle_list
    assert result["vehicle_status"]["data"] == []


async def test_vehicle_entries_without_a_vin_are_skipped(diagnostics):
    vehicles = ["unexpected", {"generation": "21MM"}, {"vin": "TESTVIN"}]
    _, result = await diagnostics(vehicles, get_telemetry={}, get_electric_status={})
    assert result["vehicle_list"]["data"] == [
        "unexpected",
        {"generation": "21MM"},
        {"vin": REDACTED},
    ]
    assert result["telemetry"]["data"] == [{}]
