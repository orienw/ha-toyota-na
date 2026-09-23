"""Climate reservations preserve options, local times, and reported state."""

import asyncio
from copy import deepcopy
from datetime import datetime
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

from toyota_na.exceptions import LoginError

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import climate_schedule_helpers, patch_base_vehicle, sensor, switch
from custom_components.toyota_na.climate_schedule_helpers import build_climate_schedule, climate_schedule_matches, local_climate_schedule


ZONE = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 9, 21, 12, tzinfo=ZONE)
SCHEDULE = {
    "reservationNo": 1, "status": "active", "reservationType": "REPETITION",
    "date": "09-22-2026", "time": "06:30", "days": ["Tuesday", "Thursday"],
    "settingType": "CUSTOM", "temperature": 22.5, "temperatureUnit": "c",
    "acOptions": {"frontDefogger": "on", "steeringHeater": "off", "frontDriverSeatHeater": "on"},
    "ventilationOptions": {"frontPassengerSeatVentilation": "on"},
}
SETTINGS = {
    "returnCode": "ONE-RES-10000", "temperatureUnit": "c",
    "minTemp": 18, "maxTemp": 30, "tempInterval": 0.5,
    "airConditioningReservation": [SCHEDULE],
}


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
                body = build_climate_schedule(SETTINGS, None, {
                    "time": local_time, "days": ["Monday", "Wednesday"], "temperature": 22.5,
                }, zone, now=NOW.astimezone(zone))
                self.assertEqual(expected_time, body["time"])
                self.assertEqual(expected_days, body["days"])
                self.assertEqual("22.5", body["temperature"])
                self.assertNotIn("status", body)
                local = local_climate_schedule(body, zone, now=NOW)
                self.assertEqual(local_time, local["time"])
                self.assertEqual(["Monday", "Wednesday"], local["days"])

    def test_repeating_schedules_follow_the_current_offset_after_a_clock_change(self):
        january = datetime(2026, 1, 12, 9, tzinfo=ZONE)
        july = datetime(2026, 7, 13, 9, tzinfo=ZONE)
        body = build_climate_schedule(SETTINGS, None, {
            "time": "07:00", "days": ["Monday"], "temperature": 22,
        }, ZONE, now=january)
        saved = {**body, "reservationNo": 2, "status": "active"}
        self.assertEqual(("15:00", "01-12-2026"), (saved["time"], saved["date"]))
        for utc_time, now, local_time, days in (
            ("15:00", january, "07:00", ["Monday"]), ("15:00", july, "08:00", ["Monday"]),
            ("07:30", january, "23:30", ["Sunday"]), ("07:30", july, "00:30", ["Monday"]),
        ):
            with self.subTest(utc_time=utc_time, now=now):
                local = local_climate_schedule({**saved, "time": utc_time}, ZONE, now=now)
                self.assertEqual((local_time, days, None), (local["time"], local["days"], local["date"]))
        self.assertEqual("14:00", build_climate_schedule(SETTINGS, saved, {"time": "07:00"}, ZONE, now=july)["time"])
        self.assertEqual("15:00", build_climate_schedule(SETTINGS, saved, {"days": ["Monday", "Friday"]}, ZONE, now=july)["time"])

    def test_one_time_dates_use_the_offset_on_the_selected_date(self):
        for selected_date, expected_date, expected_time in (
            ("2026-09-22", "09-23-2026", "06:30"),
            ("2026-12-22", "12-23-2026", "07:30"),
        ):
            body = build_climate_schedule(SETTINGS, None, {
                "time": "23:30", "date": selected_date, "temperature": 22,
            }, ZONE, now=NOW)
            self.assertEqual(expected_date, body["date"])
            self.assertEqual(expected_time, body["time"])
            self.assertEqual("ONE_TIME", body["reservationType"])
            self.assertNotIn("days", body)
            self.assertEqual(selected_date, local_climate_schedule(body, ZONE)["date"])

    def test_invalid_input_is_rejected_without_changing_the_existing_schedule(self):
        original = deepcopy(SCHEDULE)
        for changes in (
            {"enabled": "false"}, {"time": "25:00"}, {"time": "10:00:30"}, {"time": None},
            {"days": []}, {"days": ["Unknown"]}, {"date": "invalid"},
            {"date": "2026-09-23", "days": ["Monday"]}, {"date": "2020-01-01"},
            {"temperature": True}, {"temperature": float("nan")}, {"temperature": 32},
            {"temperature": 22.25}, {"unknown": True},
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
            build_climate_schedule(SETTINGS, None, {
                "date": "2027-03-14", "time": "02:30", "temperature": 22,
            }, ZONE, now=NOW)
        expired = {**SCHEDULE, "reservationType": "ONE_TIME", "date": "01-01-2020", "status": "inactive"}
        with self.assertRaisesRegex(ValueError, "future"):
            build_climate_schedule(SETTINGS, expired, {"enabled": True}, ZONE, now=NOW)
        self.assertEqual("inactive", build_climate_schedule(SETTINGS, expired, {"enabled": False}, ZONE, now=NOW)["status"])

    def test_new_schedule_requires_time_temperature_and_date_or_days(self):
        for changes in ({}, {"time": "08:00", "days": ["Monday"]}, {"time": "08:00", "temperature": 22}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "new climate schedule needs"):
                build_climate_schedule(SETTINGS, None, changes, ZONE, now=NOW)

    def test_repeating_schedule_without_a_date_or_temperature_can_be_toggled(self):
        existing = {**SCHEDULE, "date": None, "temperature": None, "time": "6:30"}
        body = build_climate_schedule(SETTINGS, existing, {"enabled": False}, ZONE, now=NOW)
        self.assertNotIn("temperature", body)
        self.assertTrue(climate_schedule_matches({**existing, "status": "inactive"}, body))
        self.assertEqual("23:30", local_climate_schedule(existing, ZoneInfo("Etc/GMT+7"))["time"])


class ClimateScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        clock = patch.object(climate_schedule_helpers, "datetime", FrozenDateTime)
        clock.start()
        self.addCleanup(clock.stop)
        self.server = deepcopy(SETTINGS)

        async def read(*args):
            return deepcopy(self.server)

        async def save(vin, generation, body, region, brand, *, identifier, delete):
            schedules = self.server["airConditioningReservation"]
            if delete:
                schedules[:] = [item for item in schedules if item["reservationNo"] != identifier]
                return identifier
            saved = {"status": "active", **deepcopy(body)}
            if "temperature" in saved:
                saved["temperature"] = float(saved["temperature"])
            if identifier is None:
                identifier = max((item["reservationNo"] for item in schedules), default=0) + 1
                schedules.append({**saved, "reservationNo": identifier})
            else:
                next(item for item in schedules if item["reservationNo"] == identifier).update(saved)
            return identifier

        self.client = types.SimpleNamespace(
            get_climate_schedules=AsyncMock(side_effect=read), save_climate_schedule=AsyncMock(side_effect=save),
        )
        self.vehicle = behavior.make_vehicle(self.client)
        self.vehicle._extended_capabilities["scheduleReservation"] = True
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle])
        self.hass = ha.FakeHass(self.coordinator)
        self.hass.config = types.SimpleNamespace(time_zone=str(ZONE))
        await self.vehicle.update_climate_schedules()

    async def test_create_edit_and_delete_confirm_reported_state_on_each_generation(self):
        for generation in ("17CY", "17CYPLUS", "21MM", "24MM", "26BEV"):
            with self.subTest(generation=generation):
                self.vehicle._generation = patch_base_vehicle.ApiVehicleGeneration(generation)
                await self.vehicle.update_climate_schedule(zone=ZONE, time="08:00", days=["Monday"], temperature=22)
                self.assertEqual([1, 2], [item["reservationNo"] for item in self.vehicle.climate_schedules["airConditioningReservation"]])
                await self.vehicle.update_climate_schedule(2, zone=ZONE, temperature=23)
                self.assertEqual(23, self.vehicle.climate_schedules["airConditioningReservation"][1]["temperature"])
                await self.vehicle.update_climate_schedule(2, zone=ZONE, delete=True)
                self.assertEqual([SCHEDULE], self.vehicle.climate_schedules["airConditioningReservation"])

    async def test_poll_populates_schedules_and_propagates_expired_authentication(self):
        self.vehicle.climate_schedules.clear()
        await self.vehicle.update()
        self.assertEqual([SCHEDULE], self.vehicle.climate_schedules["airConditioningReservation"])
        self.client.get_climate_schedules.side_effect = LoginError()
        with self.assertRaises(LoginError):
            await self.vehicle.update()

    async def test_create_can_confirm_a_new_schedule_when_response_omits_its_id(self):
        save = self.client.save_climate_schedule.side_effect

        async def save_without_id(*args, **kwargs):
            await save(*args, **kwargs)
            return None

        self.client.save_climate_schedule.side_effect = save_without_id
        await self.vehicle.update_climate_schedule(zone=ZONE, time="08:00", days=["Monday"], temperature=22)
        self.assertEqual(2, len(self.vehicle.climate_schedules["airConditioningReservation"]))

    async def test_switch_reads_fresh_options_and_preserves_them(self):
        self.server["airConditioningReservation"][0]["acOptions"]["steeringHeater"] = "on"
        entity = switch.ToyotaClimateScheduleSwitch("1", self.coordinator, "Climate Schedule 1", self.vehicle.vin)
        entity.hass = self.hass
        await entity.async_turn_off()
        self.assertFalse(entity.is_on)
        body = self.client.save_climate_schedule.call_args.args[2]
        self.assertEqual("on", body["acOptions"]["steeringHeater"])
        self.assertEqual(SCHEDULE["ventilationOptions"], body["ventilationOptions"])
        self.assertEqual("23:30", entity.extra_state_attributes["time"])
        self.assertEqual(["Monday", "Wednesday"], entity.extra_state_attributes["days"])
        self.assertFalse(self.hass.tasks)

    async def test_capabilities_gate_writes_and_reads_preserve_visible_schedules_without_subscription(self):
        self.vehicle._has_remote_subscription = False
        await self.vehicle.update_climate_schedules()
        self.assertEqual([SCHEDULE], self.vehicle.climate_schedules["airConditioningReservation"])
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
        self.vehicle._has_remote_subscription = True
        self.vehicle._feature_flags = {"remoteClimate": 2}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
        self.vehicle._extended_capabilities["scheduleReservation"] = False
        self.client.get_climate_schedules.reset_mock()
        await self.vehicle.update_climate_schedules()
        self.client.get_climate_schedules.assert_not_awaited()
        self.client.save_climate_schedule.assert_not_awaited()

    async def test_missing_or_malformed_responses_preserve_cached_schedules_and_block_writes(self):
        for result in (
            None, {}, {"airConditioningReservation": None}, {**SETTINGS, "airConditioningReservation": {}},
            {**SETTINGS, "airConditioningReservation": [None]}, {**SETTINGS, "returnCode": "FAILED"},
        ):
            with self.subTest(result=result):
                self.client.get_climate_schedules.side_effect = None
                self.client.get_climate_schedules.return_value = result
                with self.assertRaisesRegex(RuntimeError, "current climate schedules"):
                    await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
                self.assertEqual([SCHEDULE], self.vehicle.climate_schedules["airConditioningReservation"])
        self.client.save_climate_schedule.assert_not_awaited()

    async def test_successful_responses_without_a_list_allow_deleting_the_last_and_creating_the_first(self):
        read = self.client.get_climate_schedules.side_effect

        async def read_null_when_empty(*args):
            result = await read(*args)
            return {**result, "airConditioningReservation": result["airConditioningReservation"] or None}

        self.client.get_climate_schedules.side_effect = read_null_when_empty
        await self.vehicle.update_climate_schedule(1, zone=ZONE, delete=True)
        self.assertEqual([], self.vehicle.climate_schedules["airConditioningReservation"])
        await self.vehicle.update_climate_schedule(zone=ZONE, time="08:00", days=["Monday"], temperature=22)
        self.assertEqual([1], [item["reservationNo"] for item in self.vehicle.climate_schedules["airConditioningReservation"]])
        self.client.get_climate_schedules.side_effect = None
        self.client.get_climate_schedules.return_value = {
            key: value for key, value in SETTINGS.items() if key != "airConditioningReservation"
        }
        await self.vehicle.update_climate_schedules()
        self.assertEqual([], self.vehicle.climate_schedules["airConditioningReservation"])

    async def test_reservations_without_an_id_are_skipped(self):
        self.server["airConditioningReservation"].insert(0, {"reservationNo": None, "status": "active"})
        await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
        schedules = self.vehicle.climate_schedules["airConditioningReservation"]
        self.assertEqual([(1, "inactive")], [(item["reservationNo"], item["status"]) for item in schedules])

    async def test_deleted_schedule_cannot_be_edited(self):
        self.server["airConditioningReservation"].clear()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
        self.client.save_climate_schedule.assert_not_awaited()

    async def test_unconfirmed_write_times_out_without_inventing_state_or_replaying_the_write(self):
        self.client.save_climate_schedule.side_effect = None
        elapsed = 0
        delays = []

        async def sleep(delay):
            nonlocal elapsed
            elapsed += delay
            delays.append(delay)

        with (
            patch.object(patch_base_vehicle.asyncio, "get_running_loop", return_value=types.SimpleNamespace(time=lambda: elapsed)),
            patch.object(patch_base_vehicle.asyncio, "sleep", side_effect=sleep),
            self.assertRaisesRegex(RuntimeError, "accepted.*did not return"),
        ):
            await self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False)
        self.assertEqual([5, 10, 20, 30, 25], delays)
        self.client.save_climate_schedule.assert_awaited_once()
        self.assertEqual("active", self.vehicle.climate_schedules["airConditioningReservation"][0]["status"])

    async def test_poll_and_write_share_a_lock_after_vehicle_replacement(self):
        started, release = asyncio.Event(), asyncio.Event()
        save = self.client.save_climate_schedule.side_effect

        async def delayed_save(*args, **kwargs):
            started.set()
            await release.wait()
            return await save(*args, **kwargs)

        self.client.save_climate_schedule.side_effect = delayed_save
        replacement = behavior.make_vehicle(self.client)
        replacement._extended_capabilities["scheduleReservation"] = True
        replacement.inherit_state(self.vehicle)
        write = asyncio.create_task(self.vehicle.update_climate_schedule(1, zone=ZONE, enabled=False))
        await started.wait()
        poll = asyncio.create_task(replacement.update_climate_schedules())
        await asyncio.sleep(0)
        self.assertFalse(poll.done())
        release.set()
        await asyncio.gather(write, poll)
        self.assertIs(self.vehicle.climate_schedules, replacement.climate_schedules)
        self.assertEqual("inactive", replacement.climate_schedules["airConditioningReservation"][0]["status"])

    async def test_sensor_and_switch_discovery_remove_deleted_switch_and_allow_id_reuse(self):
        entry = ha.ConfigEntry()
        added = []
        registry = self.hass.entity_registry

        def add_switches(entities, update):
            for entity in entities:
                entity.hass = self.hass
                added.append(entity)
                entity_id = f"switch.schedule_{len(added)}"
                registry.entries[entity_id] = types.SimpleNamespace(
                    entity_id=entity_id, unique_id=entity.unique_id, domain="switch", platform=ha.DOMAIN,
                    config_entry_id=entry.entry_id,
                )

        await switch.async_setup_entry(self.hass, entry, add_switches)
        sensors = []
        await sensor.async_setup_entry(self.hass, entry, lambda entities, update: sensors.extend(entities))
        count = next(entity for entity in sensors if entity.sensor_name == "Climate Schedules")
        count.hass = self.hass
        self.assertEqual(1, count.native_value)
        self.assertEqual("23:30", count.extra_state_attributes["schedules"][0]["time"])
        self.assertEqual(0.5, count.extra_state_attributes["temperature_step"])
        await self.vehicle.update_climate_schedule(1, zone=ZONE, delete=True)
        self.coordinator.notify_listeners()
        self.assertEqual(["switch.schedule_1"], registry.removed)
        self.assertEqual(0, count.native_value)
        self.server["airConditioningReservation"] = [deepcopy(SCHEDULE)]
        await self.vehicle.update_climate_schedules()
        self.coordinator.notify_listeners()
        self.coordinator.notify_listeners()
        self.assertEqual(2, len(added))
        self.assertEqual(added[0].unique_id, added[1].unique_id)

    async def test_services_resolve_timezone_map_fields_and_surface_errors(self):
        handlers = {}
        self.hass.services = types.SimpleNamespace(async_register=lambda domain, name, handler: handlers.update({name: handler}))
        entry = ha.ConfigEntry()
        self.hass.async_get_entry = lambda entry_id: entry
        device = types.SimpleNamespace(config_entries={entry.entry_id}, identifiers={(ha.DOMAIN, self.vehicle.vin)})
        self.hass.device_registry = types.SimpleNamespace(async_get=lambda device_id: device)
        await ha.integration_runtime.async_setup(self.hass, {})
        self.vehicle.update_climate_schedule = AsyncMock()
        notify = self.coordinator.async_set_updated_data = Mock()
        await handlers["set_climate_schedule"](types.SimpleNamespace(
            service="set_climate_schedule", data={"vehicle": "device", "schedule_id": 1, "start_time": "08:00", "temperature": 22},
        ))
        self.vehicle.update_climate_schedule.assert_awaited_once_with(1, zone=ZONE, delete=False, time="08:00", temperature=22)
        notify.assert_called_once_with(self.coordinator.data)
        for error, expected in ((ValueError("Invalid date"), ha.exceptions.ServiceValidationError), (RuntimeError("Toyota is unavailable"), ha.exceptions.HomeAssistantError)):
            self.vehicle.update_climate_schedule.side_effect = error
            with self.assertRaises(expected) as raised:
                await handlers["delete_climate_schedule"](types.SimpleNamespace(
                    service="delete_climate_schedule", data={"vehicle": "device", "schedule_id": 1},
                ))
            self.assertEqual(str(error), str(raised.exception))
        self.assertFalse(self.hass.tasks)
        self.assertEqual({}, entry.data)
