"""Climate reservations preserve options, local times, and reported state."""

import asyncio
import json
import types
import unittest
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from common import entity_id, make_vehicle
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from toyota_na.exceptions import LoginError

from custom_components.toyota_na import climate_schedule_helpers, patch_base_vehicle
from custom_components.toyota_na.climate_schedule_helpers import (
    build_climate_schedule,
    climate_schedule_matches,
    local_climate_schedule,
)
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration

ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 21, 12, tzinfo=ZONE)
SCHEDULE = {
    "reservationNo": 1,
    "status": "active",
    "reservationType": "REPETITION",
    "date": "09-22-2026",
    "time": "06:30",
    "days": ["Tuesday", "Thursday"],
    "settingType": "CUSTOM",
    "temperature": 22.5,
    "temperatureUnit": "c",
    "acOptions": {"frontDefogger": "on", "steeringHeater": "off", "frontDriverSeatHeater": "on"},
    "ventilationOptions": {"frontPassengerSeatVentilation": "on"},
}
SETTINGS = {
    "returnCode": "ONE-RES-10000",
    "temperatureUnit": "c",
    "minTemp": 18,
    "maxTemp": 30,
    "tempInterval": 0.5,
    "airConditioningReservation": [SCHEDULE],
}
SHOWN = local_climate_schedule(SCHEDULE, ZONE)


def make_schedule_vehicle(client):
    vehicle = make_vehicle(client)
    vehicle._has_electric = True
    vehicle._extended_capabilities = {**vehicle.extended_capabilities, "climateCapable": True}
    return vehicle


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz)


class ClimateScheduleFormatTests(unittest.TestCase):
    def test_repeating_times_and_days_roll_over_in_both_directions(self):
        for zone, local_time, expected_time, expected_days in (
            (ZONE, "23:30", "06:30", ["Tuesday", "Thursday"]),
            (ZoneInfo("Asia/Tokyo"), "01:30", "16:30", ["Sunday", "Tuesday"]),
        ):
            with self.subTest(zone=zone):
                body = build_climate_schedule(
                    SETTINGS,
                    None,
                    {
                        "time": local_time,
                        "days": ["Monday", "Wednesday"],
                        "temperature": 22.5,
                    },
                    zone,
                    now=NOW.astimezone(zone),
                )
                self.assertEqual(expected_time, body["time"])
                self.assertEqual(expected_days, body["days"])
                self.assertEqual("22.5", body["temperature"])
                self.assertNotIn("status", body)
                self.assertEqual({"frontDefogger": "off", "rearDefogger": "off"}, body["acOptions"])
                self.assertEqual({}, body["ventilationOptions"])
                local = local_climate_schedule(body, zone, now=NOW)
                self.assertEqual(local_time, local["time"])
                self.assertEqual(["Monday", "Wednesday"], local["days"])

    def test_repeating_schedules_keep_the_saved_date_offset_after_a_clock_change(self):
        january = datetime(2026, 1, 12, 9, tzinfo=ZONE)
        july = datetime(2026, 7, 13, 9, tzinfo=ZONE)
        body = build_climate_schedule(
            SETTINGS,
            None,
            {
                "time": "07:00",
                "days": ["Monday"],
                "temperature": 22,
            },
            ZONE,
            now=january,
        )
        saved = {**body, "reservationNo": 2, "status": "active"}
        self.assertEqual(("15:00", "01-12-2026"), (saved["time"], saved["date"]))
        for utc_time, now, local_time, days in (
            ("15:00", january, "07:00", ["Monday"]),
            ("15:00", july, "07:00", ["Monday"]),
            ("07:30", january, "23:30", ["Sunday"]),
            ("07:30", july, "23:30", ["Sunday"]),
        ):
            with self.subTest(utc_time=utc_time, now=now):
                local = local_climate_schedule({**saved, "time": utc_time}, ZONE, now=now)
                self.assertEqual(
                    (local_time, days, None), (local["time"], local["days"], local["date"])
                )
        # Setting the time again saves it at the current offset; changing only the days keeps it.
        for changes, expected in (
            ({"time": "07:00"}, ("14:00", "07-13-2026")),
            ({"time": "06:00"}, ("13:00", "07-13-2026")),
            ({"days": ["Monday", "Friday"]}, ("15:00", "01-12-2026")),
        ):
            with self.subTest(changes=changes):
                edited = build_climate_schedule(SETTINGS, saved, changes, ZONE, now=july)
                self.assertEqual(expected, (edited["time"], edited["date"]))
        one_time = {**saved, "reservationType": "ONE_TIME", "date": "01-01-2020", "days": None}
        for changes, expected in (
            ({"days": ["Monday"]}, ("REPETITION", "07-13-2026", "14:00", ["Monday"])),
            ({"date": "2026-12-01"}, ("ONE_TIME", "12-01-2026", "15:00", None)),
        ):
            with self.subTest(changes=changes):
                edited = build_climate_schedule(
                    SETTINGS, one_time if "days" in changes else saved, changes, ZONE, now=july
                )
                self.assertEqual(
                    expected,
                    tuple(edited.get(key) for key in ("reservationType", "date", "time", "days")),
                )
        # Toyota's app converts a repeating schedule without a date on today's date.
        undated = {**saved, "date": None}
        for now, local_time in ((january, "07:00"), (july, "08:00")):
            with self.subTest(undated=now):
                self.assertEqual(local_time, local_climate_schedule(undated, ZONE, now=now)["time"])
        edited = build_climate_schedule(SETTINGS, undated, {"time": "07:00"}, ZONE, now=july)
        self.assertEqual(("14:00", "07-13-2026"), (edited["time"], edited["date"]))

    def test_repeating_schedule_confirms_only_the_time_and_days_it_shows(self):
        july = datetime(2026, 7, 13, 9, tzinfo=ZONE)
        # Saved in January, 14:00 UTC shows 06:00; set to 07:00 in July it is 14:00 UTC on a new date.
        saved = {**SCHEDULE, "date": "01-12-2026", "time": "14:00", "days": ["Monday"]}
        self.assertEqual("06:00", local_climate_schedule(saved, ZONE, now=july)["time"])
        body = build_climate_schedule(SETTINGS, saved, {"time": "07:00"}, ZONE, now=july)
        self.assertEqual(("14:00", "07-13-2026"), (body["time"], body["date"]))
        self.assertFalse(climate_schedule_matches(saved, body, ZONE))
        self.assertTrue(climate_schedule_matches({**saved, **body}, body, ZONE))
        # Without a reported date the time alone confirms, as the reservation converts on today's date.
        self.assertTrue(climate_schedule_matches({**saved, "date": None}, body, ZONE, now=july))
        self.assertFalse(
            climate_schedule_matches({**saved, "date": None, "time": "15:00"}, body, ZONE, now=july)
        )
        # Changing only the days keeps the saved start, which still confirms.
        edited = build_climate_schedule(
            SETTINGS, saved, {"days": ["Monday", "Friday"]}, ZONE, now=july
        )
        self.assertEqual(("14:00", "01-12-2026"), (edited["time"], edited["date"]))
        self.assertTrue(climate_schedule_matches({**saved, **edited}, edited, ZONE))
        self.assertFalse(climate_schedule_matches(saved, edited, ZONE))
        # Unchanged settings confirm even though the read is the schedule before the change.
        same = build_climate_schedule(SETTINGS, saved, {"days": ["Monday"]}, ZONE, now=july)
        self.assertTrue(climate_schedule_matches(saved, same, ZONE))
        # Setting the 06:00 it shows again saves 13:00 UTC, which the schedule before does not confirm.
        reset = build_climate_schedule(SETTINGS, saved, {"time": "06:00"}, ZONE, now=july)
        self.assertEqual(("13:00", "07-13-2026"), (reset["time"], reset["date"]))
        self.assertFalse(climate_schedule_matches(saved, reset, ZONE))
        self.assertTrue(climate_schedule_matches({**saved, **reset}, reset, ZONE))

    def test_repeating_schedule_confirms_when_toyota_reports_it_differently(self):
        july = datetime(2026, 7, 13, 9, tzinfo=ZONE)
        saved = {**SCHEDULE, "date": "01-12-2026", "time": "14:00", "days": ["Monday"]}
        body = build_climate_schedule(SETTINGS, saved, {"time": "07:00"}, ZONE, now=july)
        warmer = build_climate_schedule(SETTINGS, saved, {"temperature": 23}, ZONE, now=july)
        created = build_climate_schedule(
            SETTINGS, None, {"time": "07:00", "days": ["Monday"], "temperature": 22}, ZONE, now=july
        )
        for name, reported, desired in (
            ("another date", {**saved, **body, "date": "07-20-2026"}, body),
            ("unpadded date", {**saved, **body, "date": "7-13-2026"}, body),
            ("empty date", {**saved, **body, "date": ""}, body),
            ("seconds", {**saved, **body, "time": "14:00:00"}, body),
            ("setting type case", {**saved, **body, "settingType": "custom"}, body),
            (
                "no setting type",
                {key: value for key, value in {**saved, **body}.items() if key != "settingType"},
                body,
            ),
            (
                "temperature on another date",
                {**saved, "temperature": 23.0, "date": "02-02-2026"},
                warmer,
            ),
            (
                "created on another date",
                {**created, "reservationNo": 2, "status": "active", "date": "07-20-2026"},
                created,
            ),
        ):
            with self.subTest(name):
                self.assertTrue(climate_schedule_matches(reported, desired, ZONE, now=july))
        self.assertFalse(
            climate_schedule_matches({**saved, **body, "time": "15:00:00"}, body, ZONE)
        )
        self.assertFalse(climate_schedule_matches({**saved, "date": "02-02-2026"}, warmer, ZONE))
        # A date that would show another time does not confirm, whatever else changed.
        both = build_climate_schedule(
            SETTINGS, saved, {"time": "07:00", "temperature": 23}, ZONE, now=july
        )
        self.assertFalse(climate_schedule_matches({**saved, "temperature": 23.0}, both, ZONE))
        self.assertFalse(
            climate_schedule_matches(
                {**created, "reservationNo": 2, "status": "active", "date": "12-07-2026"},
                created,
                ZONE,
            )
        )
        self.assertFalse(
            climate_schedule_matches(
                {key: value for key, value in saved.items() if key != "ventilationOptions"},
                body,
                ZONE,
            )
        )

    def test_repeating_schedule_without_a_date_confirms_the_time_it_shows_today(self):
        # On the day clocks move back, 07:30 UTC on Monday shows as Monday 00:30
        # today, while Sunday 23:30 is 07:30 UTC on Monday after the change.
        november = datetime(2026, 11, 1, 12, tzinfo=ZONE)
        undated = {**SCHEDULE, "date": None, "time": "07:30", "days": ["Monday"]}
        self.assertEqual(
            ("00:30", ["Monday"]),
            tuple(
                local_climate_schedule(undated, ZONE, now=november)[key] for key in ("time", "days")
            ),
        )
        body = build_climate_schedule(
            SETTINGS, undated, {"time": "23:30", "days": ["Sunday"]}, ZONE, now=november
        )
        self.assertEqual(
            ("11-02-2026", "07:30", ["Monday"]), (body["date"], body["time"], body["days"])
        )
        self.assertFalse(climate_schedule_matches(undated, body, ZONE, now=november))
        self.assertTrue(
            climate_schedule_matches({**undated, "date": body["date"]}, body, ZONE, now=november)
        )

    def test_out_of_range_reservation_dates_do_not_raise(self):
        july = datetime(2026, 7, 13, 9, tzinfo=ZONE)
        saved = {**SCHEDULE, "date": "07-13-2026", "time": "07:30", "days": ["Monday"]}
        body = build_climate_schedule(SETTINGS, saved, {"enabled": False}, ZONE, now=july)
        ancient = {**body, "reservationNo": 1, "date": "01-01-0001", "time": "00:00"}
        self.assertFalse(climate_schedule_matches(ancient, body, ZONE, now=july))
        self.assertIsNone(local_climate_schedule(ancient, ZONE, now=july)["time"])
        self.assertEqual(
            "07:00",
            local_climate_schedule({**saved, **body, "time": "14:00:00"}, ZONE, now=july)["time"],
        )

    def test_repeating_schedules_saved_on_clock_change_days(self):
        with self.assertRaisesRegex(ValueError, "clocks move forward"):
            build_climate_schedule(
                SETTINGS,
                None,
                {
                    "time": "02:30",
                    "days": ["Monday"],
                    "temperature": 22,
                },
                ZONE,
                now=datetime(2026, 3, 8, 12, tzinfo=ZONE),
            )
        # 09:30 UTC is the second 01:30 on the day the clocks move back.
        saved = {**SCHEDULE, "date": "11-01-2026", "time": "09:30", "days": ["Sunday"]}
        local = local_climate_schedule(saved, ZONE, now=NOW)
        self.assertEqual(("01:30", ["Sunday"]), (local["time"], local["days"]))
        body = build_climate_schedule(
            SETTINGS, saved, {"days": ["Sunday", "Monday"]}, ZONE, now=NOW
        )
        self.assertEqual(
            ("09:30", "11-01-2026", ["Sunday", "Monday"]),
            (body["time"], body["date"], body["days"]),
        )
        # Entering the repeated time uses its first occurrence, as the app does.
        fall_back = datetime(2026, 11, 1, 12, tzinfo=ZONE)
        self.assertEqual(
            "08:30",
            build_climate_schedule(SETTINGS, saved, {"time": "01:30"}, ZONE, now=fall_back)["time"],
        )

    def test_one_time_dates_use_the_offset_on_the_selected_date(self):
        for selected_date, expected_date, expected_time in (
            ("2026-09-22", "09-23-2026", "06:30"),
            ("2026-12-22", "12-23-2026", "07:30"),
        ):
            body = build_climate_schedule(
                SETTINGS,
                None,
                {
                    "time": "23:30",
                    "date": selected_date,
                    "temperature": 22,
                },
                ZONE,
                now=NOW,
            )
            self.assertEqual(expected_date, body["date"])
            self.assertEqual(expected_time, body["time"])
            self.assertEqual("ONE_TIME", body["reservationType"])
            self.assertNotIn("days", body)
            self.assertEqual(selected_date, local_climate_schedule(body, ZONE)["date"])

    def test_invalid_input_is_rejected_without_changing_the_existing_schedule(self):
        original = deepcopy(SCHEDULE)
        for changes in (
            {"enabled": "false"},
            {"time": "25:00"},
            {"time": "10:00:30"},
            {"time": None},
            {"days": []},
            {"days": ["Unknown"]},
            {"date": "invalid"},
            {"date": "2026-09-23", "days": ["Monday"]},
            {"date": "2020-01-01"},
            {"temperature": True},
            {"temperature": float("nan")},
            {"temperature": 32},
            {"temperature": 22.25},
            {"unknown": True},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_climate_schedule(SETTINGS, SCHEDULE, changes, ZONE, now=NOW)
        self.assertEqual(original, SCHEDULE)

    def test_missing_metadata_is_an_operational_error_but_does_not_block_toggling(self):
        with self.assertRaisesRegex(RuntimeError, "temperature range"):
            build_climate_schedule({}, SCHEDULE, {"temperature": 22}, ZONE, now=NOW)
        body = build_climate_schedule({}, SCHEDULE, {"enabled": False}, ZONE, now=NOW)
        self.assertEqual("inactive", body["status"])
        self.assertEqual(SCHEDULE["acOptions"], body["acOptions"])
        self.assertEqual(SCHEDULE["time"], body["time"])
        self.assertNotIn("reservationNo", body)

    def test_clock_change_gap_and_expired_reservations_cannot_be_enabled(self):
        with self.assertRaisesRegex(ValueError, "clocks move forward"):
            build_climate_schedule(
                SETTINGS,
                None,
                {
                    "date": "2027-03-14",
                    "time": "02:30",
                    "temperature": 22,
                },
                ZONE,
                now=NOW,
            )
        expired = {
            **SCHEDULE,
            "reservationType": "ONE_TIME",
            "date": "01-01-2020",
            "status": "inactive",
        }
        with self.assertRaisesRegex(ValueError, "future"):
            build_climate_schedule(SETTINGS, expired, {"enabled": True}, ZONE, now=NOW)
        self.assertEqual(
            "inactive",
            build_climate_schedule(SETTINGS, expired, {"enabled": False}, ZONE, now=NOW)["status"],
        )

    def test_new_schedule_requires_time_temperature_and_date_or_days(self):
        for changes in (
            {},
            {"time": "08:00", "days": ["Monday"]},
            {"time": "08:00", "temperature": 22},
        ):
            with (
                self.subTest(changes=changes),
                self.assertRaisesRegex(ValueError, "new climate schedule needs"),
            ):
                build_climate_schedule(SETTINGS, None, changes, ZONE, now=NOW)

    def test_repeating_schedule_without_a_date_or_temperature_can_be_toggled(self):
        existing = {**SCHEDULE, "date": None, "temperature": None, "time": "6:30"}
        body = build_climate_schedule(SETTINGS, existing, {"enabled": False}, ZONE, now=NOW)
        self.assertNotIn("temperature", body)
        self.assertTrue(climate_schedule_matches({**existing, "status": "inactive"}, body, ZONE))
        self.assertEqual("23:30", local_climate_schedule(existing, ZoneInfo("Etc/GMT+7"))["time"])


class Cloud:
    """Toyota's climate schedule endpoints, saving to an in-memory list."""

    def __init__(self):
        self.server = deepcopy(SETTINGS)
        self.get_climate_schedules = AsyncMock(side_effect=self.read)
        self.save_climate_schedule = AsyncMock(side_effect=self.save)

    async def read(self, *args):
        return deepcopy(self.server)

    async def save(self, vin, generation, body, region, brand, *, identifier, delete):
        schedules = self.server["airConditioningReservation"]
        if delete:
            schedules[:] = [item for item in schedules if item["reservationNo"] != identifier]
            return {"returnCode": "ONE-RES-10000"}
        saved = {"status": "active", **deepcopy(body)}
        if "temperature" in saved:
            saved["temperature"] = float(saved["temperature"])
        if identifier is None:
            identifier = max((item["reservationNo"] for item in schedules), default=0) + 1
            schedules.append({**saved, "reservationNo": identifier})
        else:
            next(item for item in schedules if item["reservationNo"] == identifier).update(saved)
        return {"returnCode": "ONE-RES-10000", "reservationNo": identifier}


class FakeClock:
    """asyncio as the vehicle sees it, where read-back waits only advance a counter."""

    def __init__(self):
        self.elapsed = 0
        self.delays = []

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def get_running_loop(self):
        return types.SimpleNamespace(time=lambda: self.elapsed)

    async def sleep(self, delay):
        self.elapsed += delay
        self.delays.append(delay)


@contextmanager
def fake_clock():
    # Patch the vehicle module's asyncio rather than asyncio itself, which
    # Home Assistant is running on.
    clock = FakeClock()
    with patch.object(patch_base_vehicle, "asyncio", clock):
        yield clock


@contextmanager
def raises(error, match=None):
    """Expect a service call to fail with this vehicle error, as Home Assistant reports it."""
    expected = ServiceValidationError if issubclass(error, ValueError) else HomeAssistantError
    with pytest.raises(expected, match=match) as raised:
        yield raised
    assert type(raised.value) is expected
    assert isinstance(raised.value.__cause__, error)


class Schedules:
    """A vehicle's climate schedules as Home Assistant shows and changes them."""

    def __init__(self, hass, cloud, vehicle, account):
        self.hass = hass
        self.cloud = cloud
        self.vehicle = vehicle
        self.account = account
        self.sensor = entity_id(hass, "sensor", "TESTVIN.Climate Schedules")
        self.device_id = er.async_get(hass).async_get(self.sensor).device_id

    def switch(self, number):
        return entity_id(self.hass, "switch", f"TESTVIN.Climate Schedule {number}")

    async def call(self, service, **data):
        await self.hass.services.async_call(
            DOMAIN, service, {"vehicle": self.device_id, **data}, blocking=True
        )
        await self.hass.async_block_till_done()

    async def set(self, schedule_id=None, **fields):
        if schedule_id is not None:
            fields["schedule_id"] = schedule_id
        await self.call("set_climate_schedule", **fields)

    async def delete(self, schedule_id):
        await self.call("delete_climate_schedule", schedule_id=schedule_id)

    def turn_off(self, number):
        # The switch writes its state before the call returns.
        return self.hass.services.async_call(
            "switch", "turn_off", {"entity_id": self.switch(number)}, blocking=True
        )

    async def push(self):
        self.account.coordinator.async_set_updated_data(self.account.coordinator.data)
        await self.hass.async_block_till_done()

    async def shown(self):
        """Return the schedules the sensor shows once the vehicle's state is next written."""
        await self.push()
        return self.hass.states.get(self.sensor).attributes["schedules"]


@pytest.fixture
async def schedules(hass, setup_vehicles):
    await hass.config.async_set_time_zone(str(ZONE))
    with patch.object(climate_schedule_helpers, "datetime", FrozenDateTime):
        cloud = Cloud()
        vehicle = make_schedule_vehicle(cloud)
        await vehicle.update_climate_schedules()
        account = await setup_vehicles([vehicle])
        yield Schedules(hass, cloud, vehicle, account)


class TestClimateSchedules:
    async def test_create_edit_and_delete_confirm_reported_state_on_each_generation(
        self, schedules, subtests
    ):
        for generation in ("17CYPLUS", "21MM", "24MM", "26BEV"):
            with subtests.test(generation=generation):
                schedules.vehicle._generation = ApiVehicleGeneration(generation)
                await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
                assert [item["reservationNo"] for item in await schedules.shown()] == [1, 2]
                await schedules.set(2, temperature=23)
                assert (await schedules.shown())[1]["temperature"] == 23
                await schedules.delete(2)
                assert await schedules.shown() == [SHOWN]
                for request in (
                    schedules.cloud.get_climate_schedules,
                    schedules.cloud.save_climate_schedule,
                ):
                    assert request.call_args.args[1] == generation

    async def test_poll_populates_schedules_and_propagates_expired_authentication(
        self, hass, schedules
    ):
        vehicle, account = schedules.vehicle, schedules.account

        async def get_vehicles(client):
            # Like the real account read, which updates each vehicle it returns.
            await vehicle.update()
            return [vehicle]

        account.get_vehicles.side_effect = get_vehicles
        vehicle.climate_schedules.clear()
        await account.coordinator.async_refresh()
        await hass.async_block_till_done()
        assert hass.states.get(schedules.sensor).state == "1"
        assert hass.states.get(schedules.sensor).attributes["schedules"] == [SHOWN]

        schedules.cloud.get_climate_schedules.side_effect = LoginError()
        await account.coordinator.async_refresh()
        await hass.async_block_till_done()
        assert isinstance(account.coordinator.last_exception.__cause__, LoginError)
        assert account.entry.async_get_active_flows(hass, {SOURCE_REAUTH})

    async def test_create_can_confirm_a_new_schedule_when_response_omits_its_id(
        self, hass, schedules
    ):
        save = schedules.cloud.save_climate_schedule.side_effect

        async def save_without_id(*args, **kwargs):
            await save(*args, **kwargs)
            return {}

        schedules.cloud.save_climate_schedule.side_effect = save_without_id
        await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
        assert hass.states.get(schedules.sensor).state == "2"

    async def test_create_confirms_when_toyota_omits_options_that_are_off(self, hass, schedules):
        cloud = schedules.cloud
        save = cloud.save_climate_schedule.side_effect

        async def save_without_off_options(vin, generation, body, *args, **kwargs):
            result = await save(vin, generation, body, *args, **kwargs)
            created = cloud.server["airConditioningReservation"][-1]
            created.update(acOptions={}, ventilationOptions=None)
            return result

        cloud.save_climate_schedule.side_effect = save_without_off_options
        await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
        assert hass.states.get(schedules.sensor).state == "2"

    async def test_create_confirms_without_a_reported_status_or_success_code(self, hass, schedules):
        cloud = schedules.cloud
        save = cloud.save_climate_schedule.side_effect

        async def save_without_status(*args, **kwargs):
            result = await save(*args, **kwargs)
            cloud.server["airConditioningReservation"][-1].pop("status")
            return {"reservationNo": result["reservationNo"]}

        cloud.save_climate_schedule.side_effect = save_without_status
        await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
        assert hass.states.get(schedules.sensor).state == "2"
        # Available, and off like Toyota's app shows a schedule that isn't reported active.
        assert hass.states.get(schedules.switch(2)).state == "off"

    async def test_disabling_confirms_when_toyota_omits_the_status(self, hass, schedules):
        cloud = schedules.cloud
        save = cloud.save_climate_schedule.side_effect

        async def save_without_status(*args, **kwargs):
            result = await save(*args, **kwargs)
            cloud.server["airConditioningReservation"][0].pop("status")
            return result

        cloud.save_climate_schedule.side_effect = save_without_status
        await schedules.turn_off(1)
        assert hass.states.get(schedules.switch(1)).state == "off"

    async def test_rejected_change_reports_toyotas_message_after_read_back(self, schedules):
        schedules.cloud.save_climate_schedule.side_effect = None
        schedules.cloud.save_climate_schedule.return_value = {
            "returnCode": "FAILED",
            "message": "Request failed",
        }
        with fake_clock(), raises(RuntimeError, "^Request failed$"):
            await schedules.set(1, enabled=False)
        assert (await schedules.shown())[0]["status"] == "active"

    async def test_unapplied_time_change_is_not_confirmed_by_the_same_utc_time_on_the_old_date(
        self, schedules
    ):
        cloud = schedules.cloud
        # Saved in January, 14:00 UTC shows 06:00; setting 07:00 in September sends 14:00 UTC on today's date.
        cloud.server["airConditioningReservation"][0].update(date="01-12-2026", time="14:00")
        await schedules.vehicle.update_climate_schedules()
        cloud.save_climate_schedule.side_effect = None
        cloud.save_climate_schedule.return_value = {
            "returnCode": "ONE-RES-10000",
            "reservationNo": 1,
        }
        with fake_clock(), raises(RuntimeError, "accepted.*did not return"):
            await schedules.set(1, start_time="07:00")
        body = cloud.save_climate_schedule.call_args.args[2]
        assert (body["time"], body["date"]) == ("14:00", "09-21-2026")
        assert (await schedules.shown())[0]["time"] == "06:00"

    async def test_switch_reads_fresh_options_and_preserves_them(self, hass, schedules):
        cloud = schedules.cloud
        cloud.server["airConditioningReservation"][0]["acOptions"]["steeringHeater"] = "on"
        refreshes = schedules.account.get_vehicles.await_count
        await schedules.turn_off(1)
        state = hass.states.get(schedules.switch(1))
        assert state.state == "off"
        body = cloud.save_climate_schedule.call_args.args[2]
        assert body["acOptions"]["steeringHeater"] == "on"
        assert body["ventilationOptions"] == SCHEDULE["ventilationOptions"]
        assert state.attributes["time"] == "23:30"
        assert state.attributes["days"] == ["Monday", "Wednesday"]
        assert schedules.account.get_vehicles.await_count == refreshes

    async def test_capabilities_gate_writes_and_reads_preserve_visible_schedules_without_subscription(
        self, schedules
    ):
        vehicle, cloud = schedules.vehicle, schedules.cloud
        vehicle._has_remote_subscription = False
        await vehicle.update_climate_schedules()
        assert await schedules.shown() == [SHOWN]
        with raises(ValueError, "unavailable"):
            await schedules.set(1, enabled=False)
        vehicle._has_remote_subscription = True
        vehicle._feature_flags = {"remoteClimate": 2}
        with raises(ValueError, "unavailable"):
            await schedules.set(1, enabled=False)
        vehicle._extended_capabilities = {"climateCapable": False}
        cloud.get_climate_schedules.reset_mock()
        await vehicle.update_climate_schedules()
        cloud.get_climate_schedules.assert_not_awaited()
        cloud.save_climate_schedule.assert_not_awaited()

    async def test_schedules_follow_the_vehicles_toyotas_app_offers_them_to(
        self, hass, schedules, subtests
    ):
        vehicle, cloud = schedules.vehicle, schedules.cloud
        vehicle._feature_flags = None
        for generation, electric, offered in (
            ("24MM", False, True),
            ("26BEV", True, True),
            ("21MM", True, True),
            ("17CYPLUS", True, True),
            ("21MM", False, False),
            ("17CYPLUS", False, False),
            ("17CY", True, False),
            ("NG86", True, False),
        ):
            with subtests.test(generation=generation, electric=electric):
                vehicle._generation = ApiVehicleGeneration(generation)
                vehicle._has_electric = electric
                vehicle._extended_capabilities = {
                    "climateCapable": True,
                    "scheduleReservation": True,
                }
                cloud.get_climate_schedules.reset_mock()
                await vehicle.update_climate_schedules()
                await schedules.push()
                assert (cloud.get_climate_schedules.await_count == 1) is offered
                # The schedule's switch is available only where schedules are supported.
                assert (hass.states.get(schedules.switch(1)).state == "on") is offered

    async def test_missing_or_malformed_responses_preserve_cached_schedules_and_block_writes(
        self, schedules, subtests
    ):
        cloud = schedules.cloud
        for result in (
            None,
            {},
            {"airConditioningReservation": None},
            {**SETTINGS, "airConditioningReservation": {}},
            {**SETTINGS, "airConditioningReservation": [None]},
            {**SETTINGS, "returnCode": "FAILED", "airConditioningReservation": None},
            {**SETTINGS, "returnCode": "FAILED", "airConditioningReservation": []},
            {**SETTINGS, "returnCode": "FAILED"},
        ):
            with subtests.test(result=result):
                cloud.get_climate_schedules.side_effect = None
                cloud.get_climate_schedules.return_value = result
                with raises(RuntimeError, "current climate schedules"):
                    await schedules.set(1, enabled=False)
                assert await schedules.shown() == [SHOWN]
        cloud.save_climate_schedule.assert_not_awaited()

    async def test_successful_responses_without_a_list_allow_deleting_the_last_and_creating_the_first(
        self, hass, schedules
    ):
        cloud = schedules.cloud
        read = cloud.get_climate_schedules.side_effect

        async def read_null_when_empty(*args):
            result = await read(*args)
            return {
                **result,
                "airConditioningReservation": result["airConditioningReservation"] or None,
            }

        cloud.get_climate_schedules.side_effect = read_null_when_empty
        await schedules.delete(1)
        assert hass.states.get(schedules.sensor).state == "0"
        assert await schedules.shown() == []
        await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
        assert [item["reservationNo"] for item in await schedules.shown()] == [1]
        cloud.get_climate_schedules.side_effect = None
        cloud.get_climate_schedules.return_value = {
            key: value for key, value in SETTINGS.items() if key != "airConditioningReservation"
        }
        await schedules.vehicle.update_climate_schedules()
        assert await schedules.shown() == []

    async def test_failed_read_back_does_not_confirm_a_delete(self, schedules):
        cloud = schedules.cloud
        save = cloud.save_climate_schedule.side_effect

        async def save_then_fail_reads(*args, **kwargs):
            await save(*args, **kwargs)
            cloud.server.update(returnCode="FAILED", airConditioningReservation=[])
            return {"returnCode": "ONE-RES-10000"}

        cloud.save_climate_schedule.side_effect = save_then_fail_reads
        with fake_clock(), raises(RuntimeError, "current climate schedules"):
            await schedules.delete(1)
        assert await schedules.shown() == [SHOWN]

    async def test_failed_read_back_is_retried_until_the_change_appears(self, hass, schedules):
        cloud = schedules.cloud
        read = cloud.get_climate_schedules.side_effect
        failures = [{"returnCode": "FAILED"}]

        async def fail_once_after_save(*args):
            if cloud.save_climate_schedule.await_count and failures:
                return failures.pop()
            return await read(*args)

        cloud.get_climate_schedules.side_effect = fail_once_after_save
        with fake_clock() as clock:
            await schedules.set(start_time="08:00", days=["Monday"], temperature=22)
        assert clock.delays == [5]
        assert hass.states.get(schedules.sensor).state == "2"
        cloud.save_climate_schedule.assert_awaited_once()

    async def test_any_failed_read_back_is_retried(self, schedules, subtests):
        cloud = schedules.cloud
        read = cloud.get_climate_schedules.side_effect
        for failure in (
            TimeoutError(),
            json.JSONDecodeError("Expecting value", "", 0),
            aiohttp.ClientResponseError(None, (), status=503),
            aiohttp.ClientConnectionError(),
        ):
            with subtests.test(failure=type(failure).__name__):
                failures = [failure]

                async def fail_once_after_save(*args):
                    if cloud.save_climate_schedule.await_count and failures:
                        raise failures.pop()
                    return await read(*args)

                cloud.save_climate_schedule.reset_mock()
                cloud.get_climate_schedules.side_effect = fail_once_after_save
                with fake_clock() as clock:
                    await schedules.set(
                        1,
                        enabled=cloud.server["airConditioningReservation"][0]["status"] != "active",
                    )
                assert clock.delays == [5]
                cloud.save_climate_schedule.assert_awaited_once()

    async def test_read_back_timeouts_until_the_deadline_report_an_unconfirmed_change(
        self, schedules
    ):
        cloud = schedules.cloud
        cloud.save_climate_schedule.side_effect = None
        cloud.save_climate_schedule.return_value = {}
        read = cloud.get_climate_schedules.side_effect

        async def time_out_after_save(*args):
            if cloud.save_climate_schedule.await_count:
                raise TimeoutError()
            return await read(*args)

        cloud.get_climate_schedules.side_effect = time_out_after_save
        with fake_clock(), raises(RuntimeError, "accepted.*did not return"):
            await schedules.set(1, enabled=False)

    async def test_rejected_authorization_during_read_back_is_not_retried(self, schedules):
        cloud = schedules.cloud
        read = cloud.get_climate_schedules.side_effect

        async def reject_after_save(*args):
            if cloud.save_climate_schedule.await_count:
                raise aiohttp.ClientResponseError(None, (), status=401)
            return await read(*args)

        cloud.get_climate_schedules.side_effect = reject_after_save
        with fake_clock() as clock, raises(aiohttp.ClientResponseError):
            await schedules.set(1, enabled=False)
        assert clock.delays == []

    async def test_expired_login_during_read_back_is_not_retried(self, schedules):
        cloud = schedules.cloud
        read = cloud.get_climate_schedules.side_effect

        async def expire_after_save(*args):
            if cloud.save_climate_schedule.await_count:
                raise LoginError()
            return await read(*args)

        cloud.get_climate_schedules.side_effect = expire_after_save
        with fake_clock() as clock, raises(LoginError):
            await schedules.set(1, enabled=False)
        assert clock.delays == []

    async def test_day_names_are_read_in_any_case(self, schedules):
        cloud = schedules.cloud
        cloud.server["airConditioningReservation"][0]["days"] = ["TUESDAY", "thursday"]
        await schedules.vehicle.update_climate_schedules()
        assert (await schedules.shown())[0]["days"] == ["Monday", "Wednesday"]
        await schedules.set(1, start_time="07:00")
        assert cloud.save_climate_schedule.call_args.args[2]["days"] == ["Monday", "Wednesday"]

    async def test_reservations_without_an_id_are_skipped(self, schedules):
        schedules.cloud.server["airConditioningReservation"].insert(
            0, {"reservationNo": None, "status": "active"}
        )
        await schedules.set(1, enabled=False)
        assert [(item["reservationNo"], item["status"]) for item in await schedules.shown()] == [
            (1, "inactive")
        ]

    async def test_reservation_without_an_id_does_not_block_a_delete_unless_it_is_new(
        self, schedules
    ):
        cloud = schedules.cloud
        cloud.server["airConditioningReservation"].insert(
            0, {"reservationNo": None, "status": "active"}
        )
        await schedules.delete(1)
        assert await schedules.shown() == []
        cloud.server["airConditioningReservation"][:] = [deepcopy(SCHEDULE)]
        cloud.save_climate_schedule.reset_mock()

        async def save_without_applying(*args, **kwargs):
            cloud.server["airConditioningReservation"][0].pop("reservationNo")
            return {"returnCode": "ONE-RES-10000"}

        cloud.save_climate_schedule.side_effect = save_without_applying
        with fake_clock(), raises(RuntimeError, "accepted.*did not return"):
            await schedules.delete(1)
        cloud.save_climate_schedule.assert_awaited_once()

    async def test_deleted_schedule_cannot_be_edited(self, schedules):
        schedules.cloud.server["airConditioningReservation"].clear()
        with raises(ValueError, "no longer exists"):
            await schedules.set(1, enabled=False)
        schedules.cloud.save_climate_schedule.assert_not_awaited()

    async def test_unconfirmed_write_times_out_without_inventing_state_or_replaying_the_write(
        self, schedules
    ):
        cloud = schedules.cloud
        cloud.save_climate_schedule.side_effect = None
        cloud.save_climate_schedule.return_value = {}
        with fake_clock() as clock, raises(RuntimeError, "accepted.*did not return"):
            await schedules.set(1, enabled=False)
        assert clock.delays == [5, 10, 20, 30, 25]
        cloud.save_climate_schedule.assert_awaited_once()
        assert (await schedules.shown())[0]["status"] == "active"

    async def test_poll_skips_schedules_while_a_write_confirms_after_vehicle_replacement(
        self, hass, schedules
    ):
        vehicle, cloud, coordinator = (
            schedules.vehicle,
            schedules.cloud,
            schedules.account.coordinator,
        )
        started, release = asyncio.Event(), asyncio.Event()
        save = cloud.save_climate_schedule.side_effect

        async def delayed_save(*args, **kwargs):
            started.set()
            await release.wait()
            return await save(*args, **kwargs)

        cloud.save_climate_schedule.side_effect = delayed_save
        # The next account read builds a new vehicle that takes over the old one's state.
        replacement = make_schedule_vehicle(cloud)
        replacement.inherit_state(vehicle)
        write = hass.async_create_task(schedules.turn_off(1))
        await started.wait()
        reads = cloud.get_climate_schedules.await_count
        await asyncio.wait_for(replacement.update_climate_schedules(), 1)
        assert cloud.get_climate_schedules.await_count == reads
        release.set()
        await write
        assert vehicle.climate_schedules is replacement.climate_schedules
        coordinator.async_set_updated_data([replacement])
        await hass.async_block_till_done()
        assert hass.states.get(schedules.switch(1)).state == "off"

    async def test_poll_skips_schedules_while_a_queued_change_takes_over_the_lock(
        self, hass, schedules
    ):
        vehicle, cloud = schedules.vehicle, schedules.cloud
        release = asyncio.Event()
        save = cloud.save_climate_schedule.side_effect

        async def delayed_save(*args, **kwargs):
            await release.wait()
            return await save(*args, **kwargs)

        cloud.save_climate_schedule.side_effect = delayed_save
        lock = vehicle._climate_schedule_lock
        await lock.acquire()
        queued = hass.async_create_task(schedules.turn_off(1))
        async with asyncio.timeout(1):
            while not vehicle._climate_schedule_writes:
                await asyncio.sleep(0)
        lock.release()
        reads = cloud.get_climate_schedules.await_count
        async with asyncio.timeout(1):
            await vehicle.update_climate_schedules()
        assert cloud.get_climate_schedules.await_count == reads
        release.set()
        await queued
        assert not vehicle._climate_schedule_writes
        assert hass.states.get(schedules.switch(1)).state == "off"

    async def test_write_waits_for_a_poll_read_in_progress(self, hass, schedules):
        vehicle, cloud = schedules.vehicle, schedules.cloud
        started, release = asyncio.Event(), asyncio.Event()
        read = cloud.get_climate_schedules.side_effect

        async def delayed_read(*args):
            result = await read(*args)
            started.set()
            await release.wait()
            return result

        cloud.get_climate_schedules.side_effect = delayed_read
        poll = hass.async_create_task(vehicle.update_climate_schedules())
        await started.wait()
        cloud.get_climate_schedules.side_effect = read
        write = hass.async_create_task(schedules.turn_off(1))
        async with asyncio.timeout(1):
            while not vehicle._climate_schedule_writes:
                await asyncio.sleep(0)
        cloud.save_climate_schedule.assert_not_awaited()
        release.set()
        await asyncio.gather(poll, write)
        assert hass.states.get(schedules.switch(1)).state == "off"
        assert (await schedules.shown())[0]["status"] == "inactive"

    async def test_sensor_and_switch_discovery_remove_deleted_switch_and_allow_id_reuse(
        self, hass, schedules, caplog
    ):
        switch = schedules.switch(1)
        state = hass.states.get(schedules.sensor)
        assert state.state == "1"
        assert state.attributes["schedules"][0]["time"] == "23:30"
        assert state.attributes["temperature_step"] == 0.5
        await schedules.delete(1)
        assert schedules.switch(1) is None
        assert hass.states.get(switch) is None
        assert hass.states.get(schedules.sensor).state == "0"
        caplog.clear()
        schedules.cloud.server["airConditioningReservation"] = [deepcopy(SCHEDULE)]
        await schedules.vehicle.update_climate_schedules()
        await schedules.push()
        await schedules.push()
        # The schedule's ID comes back as the same switch, added once.
        assert schedules.switch(1) == switch
        assert hass.states.get(switch).state == "on"
        assert "does not generate unique IDs" not in caplog.text

    async def test_services_resolve_timezone_map_fields_and_surface_errors(self, hass, schedules):
        vehicle, account = schedules.vehicle, schedules.account
        data = dict(account.entry.data)
        refreshes = account.get_vehicles.await_count
        vehicle.update_climate_schedule = AsyncMock()
        changed = Mock()
        remove_listener = account.coordinator.async_add_listener(changed)
        await hass.services.async_call(
            DOMAIN,
            "set_climate_schedule",
            {
                "vehicle": schedules.device_id,
                "schedule_id": 1,
                "start_time": "08:00",
                "temperature": 22,
            },
            blocking=True,
        )
        vehicle.update_climate_schedule.assert_awaited_once_with(
            1, zone=ZONE, delete=False, time="08:00", temperature=22
        )
        changed.assert_called_once_with()
        for error, expected in (
            (ValueError("Invalid date"), ServiceValidationError),
            (RuntimeError("Toyota is unavailable"), HomeAssistantError),
        ):
            vehicle.update_climate_schedule.side_effect = error
            with pytest.raises(expected) as raised:
                await hass.services.async_call(
                    DOMAIN,
                    "delete_climate_schedule",
                    {"vehicle": schedules.device_id, "schedule_id": 1},
                    blocking=True,
                )
            assert type(raised.value) is expected
            assert str(raised.value) == str(error)
        remove_listener()
        await hass.async_block_till_done()
        assert account.get_vehicles.await_count == refreshes
        assert account.entry.data == data
