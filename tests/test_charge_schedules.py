"""Schedule writes preserve unrelated data and confirm reported state."""

import json
import types
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, patch

import aiohttp
import pytest
from common import entity_id, make_17cy_vehicle, make_24mm_vehicle, set_up
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.toyota_na import patch_base_vehicle, patch_client
from custom_components.toyota_na.charging_helpers import build_charge_schedule
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, VehicleFeatures

SCHEDULE = {
    "settingId": 1,
    "enabled": True,
    "startTime": "23:00",
    "endTime": "07:00",
    "daysOfTheWeek": ["Monday", "Wednesday"],
    "status": "Active",
    "nextChargeSettingId": 2,
}
SCHEDULE_SWITCH = "TESTVIN24.Charge Schedule 1"


class Cloud:
    """Toyota's copy of one vehicle's charge schedules, changed by each save."""

    def make_vehicle(self, generation=ApiVehicleGeneration.MM24):
        self.schedules = [deepcopy(SCHEDULE)]

        async def graphql(*args):
            return {
                "electric": {
                    "charging": {"chargeSettings": {"schedules": deepcopy(self.schedules)}}
                }
            }

        async def electric(*args, **kwargs):
            return {
                "vehicleInfo": {
                    "timerChargeInfo": deepcopy(self.schedules),
                    "maxNoOfChargeSchedules": 3,
                }
            }

        async def save(vin, generation, body, region, brand, *, delete):
            identifier = body.get("settingId")
            if delete:
                self.schedules = [
                    item for item in self.schedules if item["settingId"] != identifier
                ]
            elif identifier is None:
                self.schedules.append({**deepcopy(body), "settingId": 2})
            else:
                next(item for item in self.schedules if item["settingId"] == identifier).update(
                    body
                )

        self.client = types.SimpleNamespace(
            graphql_get_vehicle_status=AsyncMock(side_effect=graphql),
            get_electric_status=AsyncMock(side_effect=electric),
            save_charge_schedule=AsyncMock(side_effect=save),
        )
        vehicle = (
            make_17cy_vehicle(self.client)
            if generation == ApiVehicleGeneration.CY17
            else make_24mm_vehicle(self.client)
        )
        vehicle._generation = generation
        vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": 1}
        vehicle._store_charge_schedules(deepcopy(self.schedules), None)
        return vehicle

    def timestamped_reads(self, vehicle, timestamps):
        async def read(*args, **kwargs):
            timestamp = timestamps.pop(0) if len(timestamps) > 1 else timestamps[0]
            if vehicle.uses_appsync:
                return {
                    "electric": {
                        "charging": {
                            "chargeSettings": {
                                "lastUpdateDateTime": timestamp,
                                "schedules": deepcopy(self.schedules),
                            }
                        }
                    }
                }
            return {
                "vehicleInfo": {
                    "acquisitionDatetime": timestamp,
                    "timerChargeInfo": deepcopy(self.schedules),
                    "maxNoOfChargeSchedules": 3,
                }
            }

        request = (
            self.client.graphql_get_vehicle_status
            if vehicle.uses_appsync
            else self.client.get_electric_status
        )
        request.side_effect = read


@pytest.fixture
def cloud():
    return Cloud()


@contextmanager
def fake_clock():
    """Let each read-back sleep advance the clock instead of waiting, and record it."""
    elapsed = 0
    sleeps = []

    async def sleep(delay):
        nonlocal elapsed
        sleeps.append(delay)
        elapsed += delay

    with (
        patch.object(
            patch_base_vehicle.asyncio,
            "get_running_loop",
            return_value=types.SimpleNamespace(time=lambda: elapsed),
        ),
        patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=sleep),
    ):
        yield sleeps


async def push(hass, account):
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()


def switch_unique_ids(hass, entry):
    return {
        item.unique_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == "switch"
    }


def duplicates(caplog, domain):
    """Entities Home Assistant rejected for reusing a unique ID in a domain."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == f"homeassistant.components.{domain}"
        and "does not generate unique IDs" in record.getMessage()
    ]


@pytest.mark.parametrize("primary_time", ["2026-09-27T12:00:00+00:00", None])
async def test_ev_fallback_preserves_primary_schedules_and_source_times(cloud, primary_time):
    legacy_time = "2026-09-27T11:00:00+00:00"
    vehicle = cloud.make_vehicle(ApiVehicleGeneration.MM21)
    primary = {
        "vehicleInfo": {
            "timerChargeInfo": [],
            "maxNoOfChargeSchedules": 0,
            "acquisitionDatetime": primary_time,
        }
    }
    legacy = {
        "vehicleInfo": {
            "chargeInfo": {"chargeRemainingAmount": 80},
            "acquisitionDatetime": legacy_time,
            "timerChargeInfo": [deepcopy(SCHEDULE)],
        }
    }
    before = deepcopy((primary, legacy))
    cloud.client.api_get = AsyncMock(side_effect=[primary, legacy])
    cloud.client.get_electric_status = types.MethodType(
        patch_client.get_electric_status, cloud.client
    )
    assert await vehicle._read_charge_schedules() == []
    assert vehicle.charge_settings["schedules"] == []
    assert vehicle.charge_settings["maxNoOfChargeSchedules"] == 0
    assert vehicle.charge_settings["_schedules_updated_at"] == (
        datetime.fromisoformat(primary_time) if primary_time else None
    )
    feature = VehicleFeatures.ChargeLevel
    assert vehicle.features[feature].value == 80
    assert vehicle._feature_timestamps[(feature, "value")] == datetime.fromisoformat(legacy_time)
    assert (primary, legacy) == before


@pytest.mark.parametrize("fallback", [{}, TimeoutError()], ids=["dict", "TimeoutError"])
async def test_ev_fallback_failure_keeps_valid_primary_schedules(cloud, fallback):
    vehicle = cloud.make_vehicle(ApiVehicleGeneration.MM21)
    primary = {
        "vehicleInfo": {
            "timerChargeInfo": [deepcopy(SCHEDULE)],
            "maxNoOfChargeSchedules": 3,
        }
    }
    cloud.client.api_get = AsyncMock(side_effect=[primary, fallback])
    cloud.client.get_electric_status = types.MethodType(
        patch_client.get_electric_status, cloud.client
    )
    assert await vehicle._read_charge_schedules() == [SCHEDULE]
    assert vehicle.charge_settings["maxNoOfChargeSchedules"] == 3
    assert cloud.client.api_get.await_count == 2


async def test_switch_preserves_times_days_and_other_schedules(hass, setup_vehicles, cloud):
    vehicle = cloud.make_vehicle()
    cloud.schedules.append({**deepcopy(SCHEDULE), "settingId": 2, "enabled": False})
    before = deepcopy(cloud.schedules[1])
    await setup_vehicles([vehicle])
    switch = entity_id(hass, "switch", SCHEDULE_SWITCH)

    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    await hass.async_block_till_done()

    state = hass.states.get(switch)
    assert state.state == "off"
    assert before == cloud.schedules[1]
    body = cloud.client.save_charge_schedule.call_args.args[2]
    assert {
        "settingId": 1,
        "enabled": False,
        "startTime": "23:00",
        "endTime": "07:00",
        "daysOfTheWeek": ["Monday", "Wednesday"],
    } == body
    assert state.attributes["startTime"] == "23:00"


@pytest.mark.parametrize(
    "generation",
    [
        ApiVehicleGeneration.CY17,
        ApiVehicleGeneration.MM21,
        ApiVehicleGeneration.MM24,
        ApiVehicleGeneration.BEV26,
    ],
)
async def test_create_and_delete_confirm_state_on_each_transport(cloud, generation):
    vehicle = cloud.make_vehicle(generation)
    await vehicle.update_charge_schedule(
        startTime="10:00:00", endTime="12:00", daysOfTheWeek=["Sunday"]
    )
    assert len(vehicle.charge_settings["schedules"]) == 2
    assert cloud.client.save_charge_schedule.call_args.args[1] == generation.value
    body = cloud.client.save_charge_schedule.call_args.args[2]
    assert "settingId" not in body
    assert body["startTime"] == "10:00"
    await vehicle.update_charge_schedule(2, delete=True)
    assert [item["settingId"] for item in vehicle.charge_settings["schedules"]] == [1]


async def test_legacy_requires_explicit_multiday_support(hass, setup_vehicles, cloud):
    vehicle = cloud.make_vehicle(ApiVehicleGeneration.MM21)
    vehicle._feature_flags = None
    assert not vehicle.supports_charge_schedules
    vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": 1}
    assert vehicle.supports_charge_schedules
    vehicle._has_remote_subscription = False
    assert not vehicle.supports_charge_schedules

    await setup_vehicles([vehicle])

    state = hass.states.get(entity_id(hass, "sensor", "TESTVIN24.Charge Schedules"))
    assert state.state == "1"
    assert state.attributes["schedules"] == [SCHEDULE]


async def test_invalid_or_deleted_schedule_cannot_be_sent(cloud, subtests):
    vehicle = cloud.make_vehicle()
    for identifier, changes in (
        (1, {"enabled": "false"}),
        (1.5, {"enabled": False}),
        (1, {"startTime": "25:00"}),
        (1, {"startTime": "10:00:30"}),
        (1, {"daysOfTheWeek": []}),
        (1, {"daysOfTheWeek": ["Unknown"]}),
        (None, {"enabled": True}),
        (99, {"enabled": True}),
    ):
        with subtests.test(identifier=identifier, changes=changes), pytest.raises(ValueError):
            await vehicle.update_charge_schedule(identifier, **changes)
    cloud.client.save_charge_schedule.assert_not_awaited()


@pytest.mark.parametrize(
    "generation",
    [
        ApiVehicleGeneration.CY17,
        ApiVehicleGeneration.MM21,
        ApiVehicleGeneration.MM24,
        ApiVehicleGeneration.BEV26,
    ],
)
async def test_schedule_access_uses_multiday_instead_of_remote_command_flag(cloud, generation):
    vehicle = cloud.make_vehicle(generation)
    vehicle._feature_flags = {"remoteCommands": 2, "multiDayCharging": 1}
    assert vehicle.supports_charge_schedules
    await vehicle.update_charge_schedule(1, enabled=False)
    assert not vehicle.charge_settings["schedules"][0]["enabled"]
    cloud.client.save_charge_schedule.reset_mock()
    for value in (0, 2, None, True):
        vehicle._feature_flags = {"remoteCommands": 1, "multiDayCharging": value}
        assert not vehicle.supports_charge_schedules
        with pytest.raises(ValueError, match="unavailable"):
            await vehicle.update_charge_schedule(1, enabled=True)
    cloud.client.save_charge_schedule.assert_not_awaited()


async def test_deleted_schedule_becomes_unavailable(hass, setup_vehicles, cloud):
    vehicle = cloud.make_vehicle()
    account = await setup_vehicles([vehicle])
    switch = entity_id(hass, "switch", SCHEDULE_SWITCH)
    changes = async_capture_events(hass, EVENT_STATE_CHANGED)

    await vehicle.update_charge_schedule(1, delete=True)
    await push(hass, account)

    # The switch reports the schedule gone before the stale entity is removed.
    assert [
        event.data["new_state"] and event.data["new_state"].state
        for event in changes
        if event.data["entity_id"] == switch
    ] == ["unavailable", None]


async def test_schedule_switch_reports_validation_and_operation_failures(
    hass, setup_vehicles, cloud
):
    vehicle = cloud.make_vehicle()
    account = await setup_vehicles([vehicle])
    switch = entity_id(hass, "switch", SCHEDULE_SWITCH)
    data = dict(account.entry.data)

    async def turn_off():
        await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)

    cloud.client.save_charge_schedule.side_effect = RuntimeError(
        "Toyota rejected the schedule change."
    )
    with pytest.raises(HomeAssistantError, match="Toyota rejected the schedule change"):
        await turn_off()
    assert hass.states.get(switch).state == "on"
    assert account.entry.data == data
    cloud.schedules.clear()
    with pytest.raises(ServiceValidationError, match="no longer exists"):
        await turn_off()
    # Home Assistant skips an unavailable entity, so the switch never reaches the vehicle.
    reads = cloud.client.graphql_get_vehicle_status.await_count
    await turn_off()
    assert cloud.client.graphql_get_vehicle_status.await_count == reads


async def test_missing_or_partial_schedule_response_is_an_operational_error(cloud, subtests):
    vehicle = cloud.make_vehicle()
    for status in ({}, {"electric": {"charging": {"chargeSettings": {"schedules": [None]}}}}):
        with subtests.test(status=status):
            cloud.client.graphql_get_vehicle_status.side_effect = None
            cloud.client.graphql_get_vehicle_status.return_value = status
            with pytest.raises(
                RuntimeError, match="Toyota did not return current charge schedules"
            ):
                await vehicle.update_charge_schedule(1, enabled=False)
            assert vehicle.charge_settings["schedules"] == [SCHEDULE]
    cloud.client.save_charge_schedule.assert_not_awaited()


async def test_partial_schedule_list_does_not_confirm_a_delete(cloud):
    vehicle = cloud.make_vehicle()
    cloud.client.save_charge_schedule.side_effect = None
    graphql = cloud.client.graphql_get_vehicle_status.side_effect

    async def partial_after_save(*args):
        if cloud.client.save_charge_schedule.await_count:
            return {"electric": {"charging": {"chargeSettings": {"schedules": [None]}}}}
        return await graphql(*args)

    cloud.client.graphql_get_vehicle_status.side_effect = partial_after_save
    with fake_clock(), pytest.raises(RuntimeError, match="current charge schedules"):
        await vehicle.update_charge_schedule(1, delete=True)
    cloud.client.save_charge_schedule.assert_awaited_once()
    assert vehicle.charge_settings["schedules"] == [SCHEDULE]


@pytest.mark.parametrize("generation", [ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24])
async def test_schedule_read_back_without_its_id_does_not_confirm_a_delete(cloud, generation):
    vehicle = cloud.make_vehicle(generation)
    cloud.client.save_charge_schedule.side_effect = None
    save = cloud.client.save_charge_schedule

    async def unidentified_after_save(*args, **kwargs):
        schedules = (
            [{**deepcopy(SCHEDULE), "settingId": None}]
            if save.await_count
            else deepcopy(cloud.schedules)
        )
        if vehicle.uses_appsync:
            return {"electric": {"charging": {"chargeSettings": {"schedules": schedules}}}}
        return {"vehicleInfo": {"timerChargeInfo": schedules, "maxNoOfChargeSchedules": 3}}

    request = (
        cloud.client.graphql_get_vehicle_status
        if vehicle.uses_appsync
        else cloud.client.get_electric_status
    )
    request.side_effect = unidentified_after_save
    with fake_clock(), pytest.raises(RuntimeError, match="accepted.*did not return"):
        await vehicle.update_charge_schedule(1, delete=True)
    save.assert_awaited_once()


@pytest.mark.parametrize("generation", [ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24])
async def test_schedule_already_without_an_id_does_not_block_a_delete(cloud, generation):
    vehicle = cloud.make_vehicle(generation)
    cloud.schedules.append({**deepcopy(SCHEDULE), "settingId": None})
    with patch.object(patch_base_vehicle.asyncio, "sleep", AsyncMock()) as sleep:
        await vehicle.update_charge_schedule(1, delete=True)
    sleep.assert_not_awaited()
    assert [item["settingId"] for item in vehicle.charge_settings["schedules"]] == [None]


async def test_failed_read_back_is_retried_until_the_change_appears(cloud):
    vehicle = cloud.make_vehicle()
    graphql = cloud.client.graphql_get_vehicle_status.side_effect
    failures = [{}]

    async def fail_once_after_save(*args):
        if cloud.client.save_charge_schedule.await_count and failures:
            return failures.pop()
        return await graphql(*args)

    cloud.client.graphql_get_vehicle_status.side_effect = fail_once_after_save
    with patch.object(patch_base_vehicle.asyncio, "sleep", AsyncMock()) as sleep:
        await vehicle.update_charge_schedule(1, enabled=False)
    sleep.assert_awaited_once_with(5)
    assert not vehicle.charge_settings["schedules"][0]["enabled"]
    cloud.client.save_charge_schedule.assert_awaited_once()


@pytest.mark.parametrize("stale", ["2026-09-14T14:00:00Z", None])
@pytest.mark.parametrize("generation", [ApiVehicleGeneration.MM21, ApiVehicleGeneration.MM24])
async def test_stale_read_back_does_not_confirm_a_change_on_each_transport(
    cloud, generation, stale
):
    vehicle = cloud.make_vehicle(generation)
    cloud.timestamped_reads(vehicle, ["2026-09-14T15:00:00Z", stale, "2026-09-14T16:00:00Z"])
    with patch.object(patch_base_vehicle.asyncio, "sleep", AsyncMock()) as sleep:
        await vehicle.update_charge_schedule(1, enabled=False)
    sleep.assert_awaited_once_with(5)
    assert not vehicle.charge_settings["schedules"][0]["enabled"]
    assert (
        vehicle.charge_settings["_schedules_updated_at"].isoformat() == "2026-09-14T16:00:00+00:00"
    )


async def test_stale_empty_list_does_not_confirm_a_delete(cloud):
    vehicle = cloud.make_vehicle()
    cloud.timestamped_reads(vehicle, ["2026-09-14T15:00:00Z", "2026-09-14T14:00:00Z"])
    with fake_clock(), pytest.raises(RuntimeError, match="accepted.*did not return"):
        await vehicle.update_charge_schedule(1, delete=True)
    assert cloud.schedules == []
    assert vehicle.charge_settings["schedules"] == [SCHEDULE]
    assert vehicle.features[VehicleFeatures.ChargeScheduleCount].value == 1


async def test_unconfirmed_change_does_not_set_state_optimistically(cloud):
    vehicle = cloud.make_vehicle()
    cloud.client.save_charge_schedule.side_effect = None
    with (
        patch.object(patch_base_vehicle, "SCHEDULE_UPDATE_TIMEOUT", 0),
        pytest.raises(RuntimeError, match="accepted.*did not return"),
    ):
        await vehicle.update_charge_schedule(1, enabled=False)
    assert vehicle.charge_settings["schedules"][0]["enabled"]


async def test_lagging_schedule_readback_backs_off_within_ninety_seconds(cloud):
    vehicle = cloud.make_vehicle()
    cloud.client.save_charge_schedule.side_effect = None
    with (
        fake_clock() as sleeps,
        pytest.raises(RuntimeError, match="accepted.*did not return"),
    ):
        await vehicle.update_charge_schedule(1, enabled=False)
    assert sleeps == [5, 10, 20, 30, 25]
    assert sum(sleeps) == 90
    assert cloud.client.graphql_get_vehicle_status.await_count == 6
    cloud.client.save_charge_schedule.assert_awaited_once()
    assert vehicle.charge_settings["schedules"][0]["enabled"]


async def test_readback_stops_as_soon_as_toyota_reports_the_saved_change(cloud):
    vehicle = cloud.make_vehicle()
    cloud.client.save_charge_schedule.side_effect = None

    async def saved_after_delay(delay):
        cloud.schedules[0]["enabled"] = False

    with patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=saved_after_delay) as sleep:
        await vehicle.update_charge_schedule(1, enabled=False)
    sleep.assert_awaited_once_with(5)
    assert cloud.client.graphql_get_vehicle_status.await_count == 3
    assert not vehicle.charge_settings["schedules"][0]["enabled"]


async def test_reported_capacity_blocks_create_without_blocking_edits(cloud):
    vehicle = cloud.make_vehicle(ApiVehicleGeneration.MM21)
    cloud.schedules.extend({**deepcopy(SCHEDULE), "settingId": identifier} for identifier in (2, 3))
    with pytest.raises(ValueError, match="schedule limit"):
        await vehicle.update_charge_schedule(
            startTime="10:00", endTime="12:00", daysOfTheWeek=["Sunday"]
        )
    cloud.client.save_charge_schedule.assert_not_awaited()
    await vehicle.update_charge_schedule(1, enabled=False)
    assert not vehicle.charge_settings["schedules"][0]["enabled"]


async def test_failed_write_preserves_reported_schedule(cloud):
    vehicle = cloud.make_vehicle()
    cloud.client.save_charge_schedule.side_effect = RuntimeError("Vehicle is unavailable")
    with pytest.raises(RuntimeError, match="Vehicle is unavailable"):
        await vehicle.update_charge_schedule(1, enabled=False)
    assert vehicle.charge_settings["schedules"] == [SCHEDULE]


def test_older_or_missing_schedules_preserve_newer_readings(cloud, subtests):
    vehicle = cloud.make_vehicle()
    for timestamp, schedules in (
        ("2026-09-14T15:00:00Z", [SCHEDULE]),
        ("2026-09-14T14:00:00Z", []),
        ("2026-09-14T16:00:00Z", None),
        ("2026-09-14T17:00:00Z", [None]),
        ("2026-09-14T18:00:00Z", [SCHEDULE, None]),
    ):
        vehicle.apply_graphql_status(
            {
                "electric": {
                    "charging": {
                        "chargeSettings": {
                            "lastUpdateDateTime": timestamp,
                            "schedules": schedules,
                        }
                    }
                },
            }
        )
    assert vehicle.charge_settings["schedules"] == [SCHEDULE]
    assert vehicle.features[VehicleFeatures.ChargeScheduleCount].value == 1
    for generation in (ApiVehicleGeneration.CY17, ApiVehicleGeneration.MM21):
        with subtests.test(generation=generation):
            vehicle = cloud.make_vehicle(generation)
            vehicle._parse_electric_status(
                {
                    "vehicleInfo": {
                        "acquisitionDatetime": "2026-09-14T15:00:00Z",
                        "timerChargeInfo": [None],
                    }
                }
            )
            assert vehicle.charge_settings["schedules"] == [SCHEDULE]


@pytest.fixture
def vehicle():
    vehicle = make_24mm_vehicle(types.SimpleNamespace())
    vehicle._feature_flags = {"multiDayCharging": 1}
    vehicle._store_charge_schedules([deepcopy(SCHEDULE)], None)
    return vehicle


async def test_deleted_schedule_is_removed_and_reused_id_is_discovered(
    hass, setup_vehicles, vehicle, caplog
):
    account = await setup_vehicles([vehicle])
    switch = entity_id(hass, "switch", SCHEDULE_SWITCH)
    assert switch is not None

    vehicle._store_charge_schedules([], None)
    await push(hass, account)
    assert entity_id(hass, "switch", SCHEDULE_SWITCH) is None
    assert hass.states.get(switch) is None
    assert switch_unique_ids(hass, account.entry) == set()

    vehicle._store_charge_schedules([deepcopy(SCHEDULE)], None)
    await push(hass, account)
    await push(hass, account)
    assert switch_unique_ids(hass, account.entry) == {SCHEDULE_SWITCH}
    assert hass.states.get(entity_id(hass, "switch", SCHEDULE_SWITCH)).state == "on"
    # Home Assistant rejects a switch added twice as a duplicate unique ID.
    assert duplicates(caplog, "switch") == []


async def test_setup_removes_disabled_deleted_schedules_within_this_vehicle_and_entry(
    hass, setup_vehicles, vehicle, caplog
):
    account = await setup_vehicles([vehicle])
    entry = account.entry
    # Stale entities are registered while the entry is unloaded, then it sets up again.
    assert await hass.config_entries.async_unload(entry.entry_id)
    other_account = MockConfigEntry(domain=DOMAIN, entry_id="other-account")
    other_account.add_to_hass(hass)
    registry = er.async_get(hass)

    def register(
        unique_id, *, domain="switch", platform=DOMAIN, config_entry=entry, disabled=False
    ):
        return registry.async_get_or_create(
            domain,
            platform,
            unique_id,
            config_entry=config_entry,
            disabled_by=er.RegistryEntryDisabler.USER if disabled else None,
        ).entity_id

    unique_id = f"{vehicle.vin}.Charge Schedule 2"
    stale = register(unique_id, disabled=True)
    retained = [
        # The registry keeps one entity per unique ID, so another account's is schedule 3.
        register(f"{vehicle.vin}.Charge Schedule 3", config_entry=other_account),
        register(unique_id, platform="other-integration"),
        register(unique_id, domain="sensor"),
        register("OTHER-VIN.Charge Schedule 2"),
        register(f"{vehicle.vin}.Use Climate Settings"),
    ]
    before = set(registry.entities)

    await set_up(hass, entry, caplog)

    assert before - set(registry.entities) == {stale}
    assert all(registry.async_get(entity) is not None for entity in retained)


async def test_missing_or_incomplete_schedule_lists_do_not_remove_entities(
    hass, setup_vehicles, vehicle, subtests
):
    account = await setup_vehicles([vehicle])
    for schedules in (None, {}, [None], [{}], [{"settingId": None}]):
        with subtests.test(schedules=schedules):
            vehicle.charge_settings["schedules"] = schedules
            await push(hass, account)
            assert switch_unique_ids(hass, account.entry) == {SCHEDULE_SWITCH}


async def test_failed_refresh_or_missing_vehicle_does_not_remove_entities(
    hass, setup_vehicles, vehicle
):
    account = await setup_vehicles([vehicle])
    vehicle._store_charge_schedules([], None)

    account.get_vehicles.side_effect = RuntimeError("Toyota is unavailable")
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not account.coordinator.last_update_success
    assert switch_unique_ids(hass, account.entry) == {SCHEDULE_SWITCH}

    account.get_vehicles.side_effect = None
    account.get_vehicles.return_value = []
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert account.coordinator.last_update_success
    assert switch_unique_ids(hass, account.entry) == {SCHEDULE_SWITCH}

    account.get_vehicles.return_value = [vehicle]
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert switch_unique_ids(hass, account.entry) == set()


async def test_older_or_missing_schedule_payload_and_subscription_loss_preserve_switches(
    hass, setup_vehicles, vehicle, caplog
):
    vehicle._store_charge_schedules([deepcopy(SCHEDULE)], "2026-09-21T15:00:00Z")
    account = await setup_vehicles([vehicle])
    vehicle._store_charge_schedules([], "2026-09-21T14:00:00Z")
    vehicle._store_charge_schedules(None, "2026-09-21T16:00:00Z")
    vehicle._has_remote_subscription = False
    await push(hass, account)
    assert switch_unique_ids(hass, account.entry) == {SCHEDULE_SWITCH}
    assert duplicates(caplog, "switch") == []


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE"])
@pytest.mark.parametrize("generation", ["24MM", "26BEV"])
@pytest.mark.parametrize(
    "identifiers",
    [
        {"appRequestNo": 42},
        {"correlationId": "42"},
        {"appRequestNo": 42, "correlationId": "other"},
    ],
)
@pytest.mark.parametrize("fields", [{}, {"appRequestNo": None}, {"appRequestNo": "42"}])
async def test_schedule_writes_match_response_ids_when_callbacks_include_them(
    generation, method, identifiers, fields
):
    callback = {"vin": "TESTVIN24", "status": "COMPLETED", **fields}
    websocket = _CallbackWebSocket(
        [
            {"vin": "OTHER", "status": "COMPLETED", **fields},
            {"vin": "TESTVIN24", "appRequestNo": 41, "status": "COMPLETED"},
            {"vin": "TESTVIN24", "appRequestNo": 41, "status": "ERROR"},
            callback,
        ]
    )
    client = types.SimpleNamespace(
        auth=_Auth(),
        api_request=AsyncMock(return_value={"returnCode": "ONE-RES-10000", **identifiers}),
    )
    body = {key: value for key, value in SCHEDULE.items() if method != "POST" or key != "settingId"}
    with patch.object(
        patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)
    ):
        result = await patch_client.save_charge_schedule(
            client, "TESTVIN24", generation, body, delete=method == "DELETE"
        )
    assert result == callback
    assert websocket.callbacks == []
    client.api_request.assert_awaited_once()
    assert client.api_request.call_args.args[0] == method


async def test_routed_schedule_subscribes_before_write_and_waits_for_callback():
    websocket = _WebSocket()

    async def request(method, endpoint, headers, **kwargs):
        assert websocket.stage == 2
        assert method == "POST"
        assert endpoint == "https://onecdn.telematicsct.com/v1/remote/route/charging"
        assert headers["X-GENERATION"] == "26BEV"
        assert kwargs["json"]["startTime"] == "23:00"
        return {"returnCode": "ONE-RES-10000", "appRequestNo": 42}

    client = types.SimpleNamespace(auth=_Auth(), api_request=AsyncMock(side_effect=request))
    with patch.object(
        patch_client.aiohttp, "ClientSession", return_value=_WebSocketSession(websocket)
    ):
        result = await patch_client.save_charge_schedule(
            client,
            "TESTVIN24",
            "26BEV",
            build_charge_schedule([], startTime="23:00", endTime="07:00", daysOfTheWeek=["Monday"]),
        )
    assert result["status"] == "COMPLETED"
    assert websocket.stage == 4


@pytest.fixture
async def services(hass, setup_vehicles):
    vehicle = make_24mm_vehicle()
    vehicle.update_charge_schedule = AsyncMock()
    account = await setup_vehicles([vehicle])
    device_id = next(
        iter(er.async_entries_for_config_entry(er.async_get(hass), account.entry.entry_id))
    ).device_id
    return vehicle, account, device_id


async def test_services_resolve_device_and_map_schedule_fields(hass, services):
    vehicle, account, device_id = services
    polls = account.get_vehicles.await_count
    await hass.services.async_call(
        DOMAIN,
        "set_charge_schedule",
        {
            "vehicle": device_id,
            "schedule_id": 1,
            "enabled": False,
            "start_time": "23:00",
            "days": ["Monday"],
        },
        blocking=True,
    )
    vehicle.update_charge_schedule.assert_awaited_once_with(
        1, delete=False, enabled=False, startTime="23:00", daysOfTheWeek=["Monday"]
    )
    await hass.services.async_call(
        DOMAIN, "delete_charge_schedule", {"vehicle": device_id, "schedule_id": 1}, blocking=True
    )
    vehicle.update_charge_schedule.assert_awaited_with(1, delete=True)
    await hass.async_block_till_done()
    # Schedule changes don't poll Toyota afterwards.
    assert account.get_vehicles.await_count == polls


async def test_schedule_services_preserve_failure_messages_with_ha_exception_types(
    hass, services, subtests
):
    vehicle, account, device_id = services
    data = dict(account.entry.data)
    for service in ("set_charge_schedule", "delete_charge_schedule"):
        for error, expected_type in (
            (ValueError("This charge schedule no longer exists."), ServiceValidationError),
            (
                RuntimeError(
                    "Toyota accepted the schedule change but did not return the updated schedule."
                ),
                HomeAssistantError,
            ),
        ):
            vehicle.update_charge_schedule.side_effect = error
            with subtests.test(service=service, error=type(error)):
                with pytest.raises(expected_type) as raised:
                    await hass.services.async_call(
                        DOMAIN, service, {"vehicle": device_id, "schedule_id": 1}, blocking=True
                    )
                assert str(raised.value) == str(error)
                assert raised.value.__cause__ is error
    assert account.entry.data == data


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


class _CallbackWebSocket(_WebSocket):
    def __init__(self, callbacks):
        super().__init__()
        self.callbacks = list(callbacks)

    async def receive(self):
        if self.stage < 2:
            return await super().receive()
        return _Message(
            {
                "type": "data",
                "id": self.subscription_id,
                "payload": {"data": {"onPostRemoteCallback": self.callbacks.pop(0)}},
            }
        )


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
