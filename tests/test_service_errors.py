"""Expected failures reach Home Assistant through every command entry point."""

import asyncio
import base64
import json
import logging
import types
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import ClientResponseError
from common import FakeVehicle, entity_id, get_entity, make_24mm_vehicle, settle
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na import patch_client
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.exceptions import TokenExpired
from custom_components.toyota_na.patch_base_vehicle import RemoteRequestCommand
from custom_components.toyota_na.patch_client import ToyotaOneClient


@pytest.fixture
async def commands(hass, setup_vehicles):
    vehicle = FakeVehicle(
        {
            RemoteRequestCommand.DoorLock,
            RemoteRequestCommand.DoorUnlock,
            RemoteRequestCommand.EngineStart,
        }
    )
    account = await setup_vehicles([vehicle])
    # Nothing from setup is left waiting, so a poll after a failed command shows in the count.
    await settle(hass)
    lock = entity_id(hass, "lock", "TESTVIN.")
    device_id = er.async_get(hass).async_get(lock).device_id
    target = {"entity_id": entity_id(hass, "button", "TESTVIN.Remote Start")}
    refresh = {"entity_id": entity_id(hass, "button", "TESTVIN.Refresh Status")}
    actions = [
        ("button", "press", target, "send_command"),
        ("button", "press", refresh, "poll_vehicle_refresh"),
        ("lock", "lock", {"entity_id": lock}, "send_command"),
        ("lock", "unlock", {"entity_id": lock}, "send_command"),
        (DOMAIN, "door_lock", {"vehicle": device_id}, "send_command"),
        (DOMAIN, "refresh", {"vehicle": device_id}, "poll_vehicle_refresh"),
    ]
    return vehicle, account, lock, actions


async def test_expected_command_failures_use_home_assistant_errors(hass, commands, subtests):
    vehicle, account, lock, actions = commands
    data = dict(account.entry.data)
    polls = account.get_vehicles.await_count
    request_info = types.SimpleNamespace(real_url="https://example.invalid/remote/command")
    detail = "Vehicle not reachable [ONE-RES-40001]"
    for error, expected_type, message in (
        (
            ValueError("Command is unavailable."),
            ServiceValidationError,
            "Command is unavailable.",
        ),
        (
            RuntimeError("Toyota rejected the command."),
            HomeAssistantError,
            "Toyota rejected the command.",
        ),
        (
            TokenExpired(),
            HomeAssistantError,
            "Toyota authentication failed. Sign in again.",
        ),
        (
            json.JSONDecodeError("Expecting value", "invalid", 0),
            HomeAssistantError,
            "Toyota returned an invalid response.",
        ),
        (
            ClientResponseError(request_info, (), status=400, message=detail),
            HomeAssistantError,
            detail,
        ),
        (
            ClientResponseError(request_info, (), status=400),
            HomeAssistantError,
            "The Toyota request failed. Try again.",
        ),
    ):
        for domain, service, target, operation in actions:
            with subtests.test(domain=domain, service=service, target=target, error=type(error)):
                with patch.object(vehicle, operation, AsyncMock(side_effect=error)) as send:
                    with pytest.raises(expected_type) as raised:
                        await hass.services.async_call(domain, service, target, blocking=True)
                    await hass.async_block_till_done()
                assert type(raised.value) is expected_type
                assert str(raised.value) == message
                assert raised.value.__cause__ is error
                send.assert_awaited_once()
                # Without door lock states, inspect the cleared command directly.
                assert get_entity(hass, lock)._requested_command is None
                assert account.entry.data == data
                assert account.get_vehicles.await_count == polls


async def test_cancellation_and_unexpected_errors_propagate(hass, commands, subtests):
    vehicle, account, lock, actions = commands
    data = dict(account.entry.data)
    polls = account.get_vehicles.await_count
    for error in (asyncio.CancelledError(), KeyError("broken payload")):
        for domain, service, target, operation in actions:
            with subtests.test(domain=domain, service=service, target=target, error=type(error)):
                with patch.object(vehicle, operation, AsyncMock(side_effect=error)):
                    with pytest.raises(type(error)) as raised:
                        await hass.services.async_call(domain, service, target, blocking=True)
                    await hass.async_block_till_done()
                assert raised.value is error
                # Without door lock states, inspect the cleared command directly.
                assert get_entity(hass, lock)._requested_command is None
                assert account.entry.data == data
                assert account.get_vehicles.await_count == polls


@pytest.mark.parametrize("native", [True, False], ids=["native_lock", "toyota_service"])
@pytest.mark.parametrize("failure", ["timeout", "handshake"])
async def test_remote_connection_errors_keep_credentials_out_of_errors_and_logs(
    hass, setup_vehicles, hass_ws_client, caplog, native, failure
):
    token = "test-command-access-token"
    client = ToyotaOneClient(
        types.SimpleNamespace(
            get_access_token=AsyncMock(return_value=token),
            get_guid=AsyncMock(return_value="test-guid"),
            get_device_id=lambda: "test-device",
        )
    )
    client.graphql_send_remote_command = AsyncMock()
    vehicle = make_24mm_vehicle(client)
    await setup_vehicles([vehicle])
    lock = entity_id(hass, "lock", "TESTVIN24.")
    device_id = er.async_get(hass).async_get(lock).device_id
    websocket = await hass_ws_client(hass)
    attempted_urls = []

    async def timeout(request, **kwargs):
        attempted_urls.append(str(request.url))
        raise TimeoutError()

    def reject_handshake(url, **kwargs):
        attempted_urls.append(url)
        socket = MagicMock()
        socket.__aenter__.side_effect = aiohttp.WSServerHandshakeError(
            types.SimpleNamespace(real_url=url), (), status=500, message="Invalid response status"
        )
        return socket

    session = MagicMock()
    session.__aenter__.return_value = session
    session.ws_connect.side_effect = reject_handshake
    connection = (
        patch.object(aiohttp.TCPConnector, "connect", AsyncMock(side_effect=timeout))
        if failure == "timeout"
        else patch.object(patch_client.aiohttp, "ClientSession", return_value=session)
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG), connection:
        await websocket.send_json_auto_id(
            {
                "type": "call_service",
                "domain": "lock" if native else DOMAIN,
                "service": "lock" if native else "door_lock",
                "service_data": {"entity_id": lock} if native else {"vehicle": device_id},
            }
        )
        response = await websocket.receive_json()

    assert not response["success"]
    assert len(attempted_urls) == 1
    encoded_headers = parse_qs(urlparse(attempted_urls[0]).query)["header"][0]
    assert json.loads(base64.b64decode(encoded_headers))["Authorization"] == f"Bearer {token}"
    output = json.dumps(response) + caplog.text
    for private in (attempted_urls[0], encoded_headers, "header=", token):
        assert private not in output
    assert response["error"]["message"] == (
        "The Toyota connection timed out. Try again."
        if failure == "timeout"
        else "Could not connect to Toyota (HTTP 500). Try again."
    )
    client.graphql_send_remote_command.assert_not_awaited()
    assert not client._remote_locks[vehicle.vin].locked()
    if failure == "handshake":
        session.__aexit__.assert_awaited_once()
