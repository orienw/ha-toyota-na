"""Sanitized end-to-end coverage across all supported vehicle generations."""

import types
from unittest.mock import AsyncMock

from custom_components.toyota_na.diagnostics import async_get_config_entry_diagnostics
from custom_components.toyota_na.vehicle import (
    GENERATION_TRANSPORTS,
    ApiVehicleGeneration,
    RemoteRequestCommand,
    VehicleFeatures,
)
from custom_components.toyota_na.vehicle_discovery import get_vehicles

REST_STATUS = {
    "occurrenceDate": "2026-09-15T12:00:00Z",
    "vehicleStatus": [
        {
            "category": "Driver Side",
            "sections": [
                {
                    "section": "Door",
                    "values": [{"value": "closed"}, {"value": "locked"}],
                }
            ],
        }
    ],
}

GRAPHQL_STATUS = {
    "lastUpdateDateTime": "2026-09-15T12:00:00Z",
    "vehicleState": {
        "lastUpdateDateTime": "2026-09-15T12:00:00Z",
        "doors": {
            "driverSide": {
                "position": {"status": "close"},
                "lock": {"status": "lock"},
            }
        },
        "engine": {"status": "off"},
    },
    "telemetry": {
        "lastUpdateDateTime": "2026-09-15T12:00:00Z",
        "odo": {"value": 1500, "unit": "km"},
    },
}

GENERATION_VEHICLES = [
    {
        "vin": "VIN00000000017CY",
        "generation": "17CY",
        "brand": "T",
        "region": "US",
        "modelYear": "2019",
        "modelName": "SEDAN",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": False,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN0000017CYPLUS",
        "generation": "17CYPLUS",
        "brand": "T",
        "region": "US",
        "modelYear": "2021",
        "modelName": "SUV",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": False,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN00000000021MM",
        "generation": "21MM",
        "brand": "L",
        "region": "US",
        "modelYear": "2023",
        "modelName": "COUPE",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": False,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN00000000024MM",
        "generation": "24MM",
        "brand": "T",
        "region": "CA",
        "modelYear": "2025",
        "modelName": "HYBRID",
        "subscriptionStatus": "SUBSCRIBED",
        "backdoorType": "hatch",
        "evVehicle": True,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN0000000026BEV",
        "generation": "26BEV",
        "brand": "T",
        "region": "US",
        "modelYear": "2026",
        "modelName": "ELECTRIC",
        "subscriptionStatus": "SUBSCRIBED",
        "backdoorType": "hatch",
        "evVehicle": True,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN000000000NG86",
        "generation": "NG86",
        "brand": "T",
        "region": "US",
        "modelYear": "2022",
        "modelName": "SPORTS",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": False,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
    {
        "vin": "VIN000000000GR86",
        "generation": "GR86",
        "brand": "T",
        "region": "US",
        "modelYear": "2024",
        "modelName": "SPORTS",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": False,
        "remoteServiceCapabilities": {"dlockUnlockCapable": True},
    },
]


def _make_multi_gen_client():
    return types.SimpleNamespace(
        auth=types.SimpleNamespace(get_guid=AsyncMock(return_value="test-guid")),
        get_user_vehicle_list=AsyncMock(return_value=list(GENERATION_VEHICLES)),
        get_telemetry=AsyncMock(return_value={"odometer": {"value": 1500, "unit": "km"}}),
        get_vehicle_status_17cy=AsyncMock(return_value=REST_STATUS),
        get_vehicle_status_17cyplus=AsyncMock(return_value=REST_STATUS),
        get_vehicle_status_21mm=AsyncMock(return_value=REST_STATUS),
        get_vehicle_status_route=AsyncMock(return_value=REST_STATUS),
        graphql_get_vehicle_status=AsyncMock(return_value=GRAPHQL_STATUS),
        get_engine_status_17cy=AsyncMock(return_value={"status": "OFF"}),
        get_engine_status_17cyplus=AsyncMock(return_value={"status": "OFF"}),
        get_engine_status_21mm=AsyncMock(return_value={"status": "OFF"}),
        get_engine_status_route=AsyncMock(return_value={"status": "OFF"}),
        get_electric_status=AsyncMock(return_value=None),
        get_tire_pressure=AsyncMock(return_value=None),
        get_notification_history=AsyncMock(return_value=[]),
        graphql_pre_wake=AsyncMock(),
        graphql_confirm_subscription=AsyncMock(),
        graphql_refresh_status=AsyncMock(),
        send_refresh_request_17cy=AsyncMock(),
        send_refresh_request_17cyplus=AsyncMock(),
        send_refresh_request_21mm=AsyncMock(),
        send_refresh_request_route=AsyncMock(),
        remote_request_17cy=AsyncMock(),
        remote_request_17cyplus=AsyncMock(),
        remote_request_21mm=AsyncMock(),
        remote_request_24mm=AsyncMock(),
        remote_request_route=AsyncMock(),
    )


async def test_all_seven_supported_generations_discover_poll_command_and_refresh():
    client = _make_multi_gen_client()
    vehicles_by_vin = {}

    vehicles = await get_vehicles(client, vehicles_by_vin=vehicles_by_vin)

    assert {vehicle.generation for vehicle in vehicles} == set(GENERATION_TRANSPORTS)
    assert len(vehicles) == len(ApiVehicleGeneration)
    for vehicle in vehicles:
        assert vehicle.doors_locked is True
        assert vehicle.features[VehicleFeatures.Odometer].value == 1500
        await vehicle.send_command(RemoteRequestCommand.DoorLock)
        await vehicle.poll_vehicle_refresh()

    client.get_vehicle_status_17cy.assert_awaited_once_with("VIN00000000017CY", "US")
    client.get_vehicle_status_17cyplus.assert_awaited_once_with("VIN0000017CYPLUS", "US")
    client.get_vehicle_status_21mm.assert_awaited_once_with("VIN00000000021MM", "US")
    assert client.get_vehicle_status_route.await_count == 2
    assert client.graphql_get_vehicle_status.await_count == 2

    client.remote_request_17cy.assert_awaited_once_with("VIN00000000017CY", "DL", 1, "US")
    client.remote_request_17cyplus.assert_awaited_once_with("VIN0000017CYPLUS", "door-lock", "US")
    client.remote_request_21mm.assert_awaited_once_with("VIN00000000021MM", "door-lock", "US")
    assert client.remote_request_24mm.await_count == 2
    assert client.remote_request_route.await_count == 2

    # 21MM triggers both GraphQL and REST refresh; 24MM/26BEV trigger GraphQL only.
    assert client.graphql_refresh_status.await_count == 3
    client.send_refresh_request_21mm.assert_awaited_once_with("VIN00000000021MM", "US")
    client.send_refresh_request_17cy.assert_awaited_once_with("VIN00000000017CY", "US")
    client.send_refresh_request_17cyplus.assert_awaited_once_with("VIN0000017CYPLUS", "US")
    assert client.send_refresh_request_route.await_count == 2


async def test_diagnostics_covers_all_seven_generations(hass, setup_vehicles):
    account = await setup_vehicles([])
    client = _make_multi_gen_client()
    account.entry.runtime_data.client = client

    report = await async_get_config_entry_diagnostics(hass, account.entry)

    assert len(report["vehicle_list"]["data"]) == 7
    assert all(status is not None for status in report["vehicle_status"]["data"])
    assert all(telemetry is not None for telemetry in report["telemetry"]["data"])
    assert sum(engine is not None for engine in report["engine_status"]["data"]) == 5
    for vehicle in GENERATION_VEHICLES:
        assert vehicle["vin"] not in str(report)
