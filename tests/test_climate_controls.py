"""Saved climate controls and preservation of unrelated preferences."""

import asyncio
from copy import deepcopy
import types
import unittest
from unittest.mock import AsyncMock, Mock

from aiohttp import ClientConnectionError
from toyota_na.exceptions import AuthError

import test_button as ha
import test_vehicle_behavior as behavior

from custom_components.toyota_na import number, select, switch
from custom_components.toyota_na.climate_helpers import climate_parameters


def operation(category, *parameters):
    return {
        "categoryName": category, "available": True,
        "acParameters": [
            {"name": name, "available": available, "enabled": enabled}
            for name, available, enabled in parameters
        ],
    }


SETTINGS = {
    "temperature": 22.0, "temperatureUnit": "C", "minTemp": 18, "maxTemp": 30,
    "tempInterval": 0.5, "settingsOn": True, "isCustomerSettings": True,
    "airFlowVolume": 2, "minAirFlow": 0, "maxAirFlow": 5,
    "extendedRuntime": {"available": True, "enabled": False},
    "acOperations": [
        operation("airflow", ("upperBody", True, True), ("feet", True, False),
                  ("upperBodyFeet", False, False), ("frontDefrostFeet", True, False)),
        operation("seatHeat", ("frontDriver", True, True), ("frontPassenger", True, False)),
        operation("seatVent", ("ventFrontDriver", True, False),
                  ("ventFrontPassenger", False, False), ("ventRearDriver", True, True)),
        operation("defrost", ("frontDefrost", True, True), ("rearDefrost", True, False)),
        operation("steeringHeaterCat", ("steeringWheel", True, True)),
        operation("airCirculate", ("insideAirCirculate", True, False)),
        operation("futureSetting", ("futureParameter", True, True)),
    ],
    "futureMetadata": {"preserve": True},
}


class ClimateControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = deepcopy(SETTINGS)

        async def read(*args):
            await asyncio.sleep(0)
            return deepcopy(self.server)

        async def write(vin, generation, settings, region, brand):
            await asyncio.sleep(0)
            self.server = deepcopy(settings)

        self.client = types.SimpleNamespace(
            get_climate_settings=AsyncMock(side_effect=read),
            update_climate_settings=AsyncMock(side_effect=write),
        )
        self.vehicle = behavior.make_vehicle(self.client)
        self.vehicle._extended_capabilities = {"climateCapable": True}
        await self.vehicle.update_climate()
        self.coordinator = ha.DataUpdateCoordinator([self.vehicle])
        self.entities = {}

        def add(entities, update):
            self.entities.update({entity.sensor_name: entity for entity in entities})

        for platform in (number, select, switch):
            await platform.async_setup_entry(ha.FakeHass(self.coordinator), ha.ConfigEntry(), add)

    def test_controls_follow_reported_seats_options_and_fan_bounds(self):
        self.assertEqual(self.entities["Driver Seat Climate"].options, ["Off", "Heat", "Ventilate"])
        self.assertEqual(self.entities["Passenger Seat Climate"].options, ["Off", "Heat"])
        self.assertEqual(self.entities["Rear Driver Seat Climate"].options, ["Off", "Ventilate"])
        self.assertNotIn("Rear Passenger Seat Climate", self.entities)
        self.assertEqual(self.entities["Climate Airflow"].options, [
            "Upper body", "Feet", "Windshield and feet",
        ])
        fan = self.entities["Climate Fan Speed"]
        self.assertEqual((fan.native_value, fan.native_min_value, fan.native_max_value, fan.native_step), (2, 0, 5, 1))

    async def test_airflow_change_preserves_seats_defrosters_and_unknown_settings(self):
        await self.entities["Climate Airflow"].async_select_option("Feet")
        self.assertEqual(self.entities["Climate Airflow"].current_option, "Feet")
        self.assertEqual(self.server["acOperations"][1:], SETTINGS["acOperations"][1:])
        self.assertEqual(self.server["futureMetadata"], SETTINGS["futureMetadata"])
        parameters = climate_parameters(self.server, "airflow")
        self.assertFalse(parameters["upperBody"]["enabled"])
        self.assertTrue(parameters["feet"]["enabled"])
        self.assertFalse(parameters["frontDefrostFeet"]["enabled"])

    async def test_seat_mode_disables_opposite_setting_and_preserves_other_seats(self):
        seat = self.entities["Driver Seat Climate"]
        await seat.async_select_option("Ventilate")
        self.assertEqual(seat.current_option, "Ventilate")
        self.assertFalse(climate_parameters(self.server, "seatHeat")["frontDriver"]["enabled"])
        self.assertTrue(climate_parameters(self.server, "seatVent")["ventFrontDriver"]["enabled"])
        self.assertTrue(climate_parameters(self.server, "seatVent")["ventRearDriver"]["enabled"])
        await seat.async_select_option("Off")
        self.assertEqual(seat.current_option, "Off")
        self.assertFalse(climate_parameters(self.server, "seatVent")["ventFrontDriver"]["enabled"])

    async def test_switches_and_fan_save_independent_preferences(self):
        await self.entities["Front Defroster"].async_turn_off()
        await self.entities["Rear Defroster"].async_turn_on()
        await self.entities["Steering Wheel Heat"].async_turn_off()
        await self.entities["Recirculate Air"].async_turn_on()
        await self.entities["Longer Climate Runtime"].async_turn_on()
        await self.entities["Climate Fan Speed"].async_set_native_value(4.0)
        self.assertFalse(self.entities["Front Defroster"].is_on)
        self.assertTrue(self.entities["Rear Defroster"].is_on)
        self.assertFalse(self.entities["Steering Wheel Heat"].is_on)
        self.assertTrue(self.entities["Recirculate Air"].is_on)
        self.assertTrue(self.server["extendedRuntime"]["enabled"])
        self.assertEqual(self.server["airFlowVolume"], 4)
        self.assertIs(type(self.server["airFlowVolume"]), int)
        self.assertEqual(self.server["temperature"], 22)

    async def test_concurrent_nested_changes_preserve_each_other(self):
        await asyncio.gather(
            self.entities["Climate Airflow"].async_select_option("Feet"),
            self.entities["Driver Seat Climate"].async_select_option("Ventilate"),
            self.entities["Longer Climate Runtime"].async_turn_on(),
        )
        self.assertTrue(climate_parameters(self.server, "airflow")["feet"]["enabled"])
        self.assertTrue(climate_parameters(self.server, "seatVent")["ventFrontDriver"]["enabled"])
        self.assertTrue(self.server["extendedRuntime"]["enabled"])

    async def test_fresh_capability_loss_rejects_save_without_changing_cached_preferences(self):
        before = deepcopy(self.vehicle.climate_settings)
        self.server["acOperations"][3]["available"] = False
        with self.assertRaises(ha.exceptions.ServiceValidationError):
            await self.entities["Front Defroster"].async_turn_off()
        self.client.update_climate_settings.assert_not_awaited()
        self.assertEqual(self.vehicle.climate_settings, before)
        await self.vehicle.update_climate()
        self.assertFalse(self.entities["Front Defroster"].available)

    async def test_fan_and_unavailable_seat_modes_are_rejected_before_write(self):
        for value in (-1, 6, 2.5, float("nan")):
            with self.subTest(value=value), self.assertRaises(ha.exceptions.ServiceValidationError):
                await self.entities["Climate Fan Speed"].async_set_native_value(value)
        with self.assertRaises(ha.exceptions.ServiceValidationError):
            await self.entities["Passenger Seat Climate"].async_select_option("Ventilate")
        self.client.update_climate_settings.assert_not_awaited()

    async def test_expected_write_failures_preserve_messages_and_reported_state(self):
        changed = Mock()
        self.coordinator.async_add_listener(changed)
        actions = (
            (self.entities["Climate Fan Speed"].async_set_native_value, (4,)),
            (self.entities["Climate Airflow"].async_select_option, ("Feet",)),
            (self.entities["Driver Seat Climate"].async_select_option, ("Off",)),
            (self.entities["Use Climate Settings"].async_turn_off, ()),
            (self.entities["Front Defroster"].async_turn_off, ()),
        )
        for error in (
            RuntimeError("Toyota rejected the change."), ClientConnectionError("Toyota connection failed."),
            AuthError("Toyota session expired."), asyncio.TimeoutError(),
        ):
            self.client.update_climate_settings.side_effect = error
            for action, args in actions:
                with self.subTest(error=type(error), action=action), self.assertRaises(ha.exceptions.HomeAssistantError) as raised:
                    await action(*args)
                self.assertIs(type(raised.exception), ha.exceptions.HomeAssistantError)
                self.assertEqual(str(error) or "The Toyota request timed out.", str(raised.exception))
                self.assertIs(error, raised.exception.__cause__)
        self.assertEqual(SETTINGS, self.vehicle.climate_settings)
        changed.assert_not_called()

    async def test_unavailable_controls_raise_validation_errors_without_writing(self):
        self.vehicle._has_remote_subscription = False
        for action, args in (
            (self.entities["Climate Fan Speed"].async_set_native_value, (4,)),
            (self.entities["Climate Airflow"].async_select_option, ("Feet",)),
            (self.entities["Use Climate Settings"].async_turn_off, ()),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(ha.exceptions.ServiceValidationError, "unavailable"):
                await action(*args)
        self.client.update_climate_settings.assert_not_awaited()

    async def test_missing_climate_response_is_an_operational_error(self):
        self.client.get_climate_settings.side_effect = None
        self.client.get_climate_settings.return_value = {}
        with self.assertRaisesRegex(ha.exceptions.HomeAssistantError, "Toyota did not return climate settings") as raised:
            await self.entities["Climate Fan Speed"].async_set_native_value(4)
        self.assertIs(type(raised.exception), ha.exceptions.HomeAssistantError)
        self.client.update_climate_settings.assert_not_awaited()

    async def test_cancellation_and_unexpected_errors_are_not_translated(self):
        for error in (asyncio.CancelledError(), KeyError("broken payload")):
            self.client.update_climate_settings.side_effect = error
            with self.subTest(error=type(error)), self.assertRaises(type(error)) as raised:
                await self.entities["Climate Fan Speed"].async_set_native_value(4)
            self.assertIs(error, raised.exception)

    async def test_disabled_custom_settings_can_be_configured_before_enabling(self):
        self.server["settingsOn"] = False
        await self.vehicle.update_climate()
        await self.entities["Driver Seat Climate"].async_select_option("Ventilate")
        self.assertFalse(self.server["settingsOn"])
        self.assertEqual(self.entities["Driver Seat Climate"].current_option, "Ventilate")

    async def test_fan_uses_vehicle_bounds_and_defaults_for_omitted_limits(self):
        self.server["minAirFlow"] = None
        self.server.pop("maxAirFlow")
        await self.vehicle.update_climate()
        fan = self.entities["Climate Fan Speed"]
        self.assertEqual((fan.native_min_value, fan.native_max_value), (1, 7))
        await fan.async_set_native_value(7)
        self.assertEqual(self.server["airFlowVolume"], 7)
