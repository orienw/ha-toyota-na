"""Repairs follow each vehicle's remote access state."""

import logging

import pytest
from common import FakeVehicle
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.remote_access import sync_remote_access_issues


def issues(hass):
    return {
        issue_id: issue.translation_key
        for (domain, issue_id), issue in ir.async_get(hass).issues.items()
        if domain == DOMAIN
    }


@pytest.fixture
def vehicle():
    return FakeVehicle(set())


async def test_only_states_needing_action_raise_a_repair(hass, setup_vehicles, vehicle, subtests):
    account = await setup_vehicles([vehicle])
    for remote_display, expected in (
        (None, None),
        (0, None),
        (7, None),
        (12, None),
        (1, "remote_authorization_required"),
        (2, "remote_subscription_cancelled"),
        (3, "remote_subscription_cancelled"),
        (4, "remote_activation_failed"),
        (5, "remote_activation_pending"),
        (6, "remote_activation_error"),
        (8, "remote_subscription_expired"),
        (9, "remote_subscription_expired"),
        (10, "vehicle_stolen"),
        (11, "vehicle_stolen_immobilizer"),
        (7, None),
    ):
        with subtests.test(remote_display=remote_display):
            vehicle.remote_display = remote_display
            await account.coordinator.async_refresh()
            await hass.async_block_till_done()
            prefix = f"remote_access_{account.entry.entry_id}_TESTVIN_"
            assert issues(hass) == ({f"{prefix}{expected}": expected} if expected else {})


async def test_repair_names_the_vehicle(hass, setup_vehicles, vehicle):
    vehicle.remote_display = 1
    await setup_vehicles([vehicle])

    [issue] = ir.async_get(hass).issues.values()
    assert issue.domain == DOMAIN
    assert issue.is_fixable is False
    assert issue.severity is ir.IssueSeverity.WARNING
    assert issue.translation_key == "remote_authorization_required"
    assert issue.translation_placeholders == {"vehicle": "2024 LC 500 2-DOOR COUPE"}


async def test_only_this_accounts_departed_vehicles_lose_their_repairs(
    hass, setup_vehicles, vehicle
):
    sold = FakeVehicle(set(), vin="SOLDVIN")
    vehicle.remote_display = sold.remote_display = 5
    ir.async_create_issue(
        hass,
        DOMAIN,
        "unrelated",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="unrelated",
    )
    account = await setup_vehicles([vehicle, sold])
    # A second account listing the same vehicle can't load beside this one, since its
    # entities would reuse these unique IDs, so its poll's repairs are synced directly.
    other_entry = MockConfigEntry(domain=DOMAIN, entry_id="other")
    other_entry.add_to_hass(hass)
    sync_remote_access_issues(hass, other_entry, [sold])
    account.get_vehicles.return_value = [vehicle]
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert set(issues(hass)) == {
        f"remote_access_{account.entry.entry_id}_TESTVIN_remote_activation_pending",
        "remote_access_other_SOLDVIN_remote_activation_pending",
        "unrelated",
    }


async def test_polls_update_repairs_and_failed_polls_keep_them(
    hass, setup_vehicles, vehicle, caplog
):
    vehicle.remote_display = 4
    account = await setup_vehicles([vehicle])
    expected = {
        f"remote_access_{account.entry.entry_id}_TESTVIN_remote_activation_failed": (
            "remote_activation_failed"
        )
    }
    assert issues(hass) == expected

    account.get_vehicles.side_effect = RuntimeError()
    caplog.clear()
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()

    assert not account.coordinator.last_update_success
    assert isinstance(account.coordinator.last_exception, UpdateFailed)
    assert any(
        record.name == "custom_components.toyota_na" and record.levelno == logging.ERROR
        for record in caplog.records
    )
    assert issues(hass) == expected


async def test_removing_the_account_clears_its_repairs(hass, setup_vehicles, vehicle):
    vehicle.remote_display = 10
    account = await setup_vehicles([vehicle])
    assert issues(hass)

    await hass.config_entries.async_remove(account.entry.entry_id)
    await hass.async_block_till_done()

    assert issues(hass) == {}
