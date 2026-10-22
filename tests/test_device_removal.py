"""Allow removing vehicles only after a successful account lookup."""

import pytest
from homeassistant.const import MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component

from custom_components.toyota_na.const import DOMAIN

REJECTED = "Failed to remove device entry, rejected by integration"


@pytest.fixture
async def removal(hass, setup_vehicles, hass_ws_client):
    """Return the account, a device for a vehicle it no longer lists, and a client to remove it."""
    assert await async_setup_component(hass, "config", {})
    account = await setup_vehicles([])
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=account.entry.entry_id, identifiers={(DOMAIN, "SOLDVIN")}
    )
    client = await hass_ws_client(hass)

    async def remove():
        # What the device page's delete button sends. Home Assistant 2026.9 replaced
        # remove_config_entry with remove, keeping the old command as a deprecated alias.
        if (MAJOR_VERSION, MINOR_VERSION) >= (2026, 9):
            message = {"type": "config/device_registry/remove", "device_id": device.id}
        else:
            message = {
                "type": "config/device_registry/remove_config_entry",
                "config_entry_id": account.entry.entry_id,
                "device_id": device.id,
            }
        await client.send_json_auto_id(message)
        return await client.receive_json()

    return account, device, remove


@pytest.mark.parametrize(
    ("vehicles", "allowed"),
    [
        ([], True),
        ([{"vin": "OTHERVIN"}], True),
        ([{"vin": "SOLDVIN", "generation": "FUTURE"}], False),
        (None, False),
        ({}, False),
        ([{}], False),
    ],
)
async def test_removal_checks_the_account_including_unsupported_vehicles(
    hass, removal, vehicles, allowed
):
    account, device, remove = removal
    account.client.get_user_vehicle_list.return_value = vehicles

    response = await remove()

    assert response["success"] is allowed
    if allowed:
        assert dr.async_get(hass).async_get(device.id) is None
    else:
        assert response["error"]["message"] == REJECTED
        assert dr.async_get(hass).async_get(device.id) is not None


async def test_failed_lookup_does_not_authorize_removal(hass, removal, subtests):
    account, device, remove = removal
    for error, code in (
        (TimeoutError(), "timeout"),
        (RuntimeError("[ONE-VL-10002]"), "unknown_error"),
    ):
        with subtests.test(error=type(error)):
            account.client.get_user_vehicle_list.side_effect = error

            response = await remove()

            assert not response["success"]
            assert response["error"]["code"] == code
            assert dr.async_get(hass).async_get(device.id) is not None


async def test_unloaded_account_and_foreign_device_cannot_be_removed(hass, removal):
    account, device, remove = removal
    registry = dr.async_get(hass)
    registry.async_update_device(device.id, new_identifiers={("other", "SOLDVIN")})
    response = await remove()
    assert response["error"]["message"] == REJECTED

    registry.async_update_device(device.id, new_identifiers={(DOMAIN, "SOLDVIN")})
    assert await hass.config_entries.async_unload(account.entry.entry_id)
    response = await remove()
    assert response["error"]["message"] == REJECTED

    assert registry.async_get(device.id) is not None
    account.client.get_user_vehicle_list.assert_not_awaited()
