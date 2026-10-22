"""Saved climate controls and preservation of unrelated preferences."""

import asyncio
import json
import logging
import types
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
import voluptuous as vol
from aiohttp import ClientConnectionError
from common import entity_id, make_vehicle
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from toyota_na.exceptions import AuthError

from custom_components.toyota_na import patch_client
from custom_components.toyota_na.climate_helpers import climate_parameters


def operation(category, *parameters):
    return {
        "categoryName": category,
        "available": True,
        "acParameters": [
            {"name": name, "available": available, "enabled": enabled}
            for name, available, enabled in parameters
        ],
    }


SETTINGS = {
    "temperature": 22.0,
    "temperatureUnit": "C",
    "minTemp": 18,
    "maxTemp": 30,
    "tempInterval": 0.5,
    "settingsOn": True,
    "isCustomerSettings": True,
    "airFlowVolume": 2,
    "minAirFlow": 0,
    "maxAirFlow": 5,
    "extendedRuntime": {"available": True, "enabled": False},
    "acOperations": [
        operation(
            "airflow",
            ("upperBody", True, True),
            ("feet", True, False),
            ("upperBodyFeet", False, False),
            ("frontDefrostFeet", True, False),
        ),
        operation("seatHeat", ("frontDriver", True, True), ("frontPassenger", True, False)),
        operation(
            "seatVent",
            ("ventFrontDriver", True, False),
            ("ventFrontPassenger", False, False),
            ("ventRearDriver", True, True),
        ),
        operation("defrost", ("frontDefrost", True, True), ("rearDefrost", True, False)),
        operation("steeringHeaterCat", ("steeringWheel", True, True)),
        operation("airCirculate", ("insideAirCirculate", True, False)),
        operation("futureSetting", ("futureParameter", True, True)),
    ],
    "futureMetadata": {"preserve": True},
}
CONTROLS = {
    "Climate Temperature": "number",
    "Climate Fan Speed": "number",
    "Climate Airflow": "select",
    "Driver Seat Climate": "select",
    "Passenger Seat Climate": "select",
    "Rear Driver Seat Climate": "select",
    "Rear Passenger Seat Climate": "select",
    "Use Climate Settings": "switch",
    "Front Defroster": "switch",
    "Rear Defroster": "switch",
    "Steering Wheel Heat": "switch",
    "Recirculate Air": "switch",
    "Longer Climate Runtime": "switch",
}


class Cloud:
    """Toyota's climate settings endpoints over one saved settings document."""

    def __init__(self):
        self.server = deepcopy(SETTINGS)
        self.get_climate_settings = AsyncMock(side_effect=self.read)
        self.update_climate_settings = AsyncMock(side_effect=self.write)

    async def read(self, *args):
        await asyncio.sleep(0)
        return deepcopy(self.server)

    async def write(self, vin, generation, settings, region, brand):
        await asyncio.sleep(0)
        self.server = deepcopy(settings)


class Controls:
    """The vehicle's climate controls as Home Assistant shows and changes them."""

    def __init__(self, hass, cloud, vehicle, account):
        self.hass = hass
        self.cloud = cloud
        self.vehicle = vehicle
        self.account = account
        self.ids = {
            name: entity_id(hass, platform, f"TESTVIN.{name}")
            for name, platform in CONTROLS.items()
        }

    def state(self, name):
        return self.hass.states.get(self.ids[name])

    async def call(self, name, service, **data):
        domain = CONTROLS[name]
        await self.hass.services.async_call(
            domain, service, {"entity_id": self.ids[name], **data}, blocking=True
        )
        await self.hass.async_block_till_done()

    async def set_value(self, name, value):
        await self.call(name, "set_value", value=value)

    async def select_option(self, name, option):
        await self.call(name, "select_option", option=option)

    async def turn_on(self, name):
        await self.call(name, "turn_on")

    async def turn_off(self, name):
        await self.call(name, "turn_off")

    async def push(self):
        self.account.coordinator.async_set_updated_data(self.account.coordinator.data)
        await self.hass.async_block_till_done()


@pytest.fixture
async def controls(hass, setup_vehicles):
    cloud = Cloud()
    vehicle = make_vehicle(cloud)
    vehicle._extended_capabilities = {"climateCapable": True}
    await vehicle.update_climate()
    account = await setup_vehicles([vehicle])
    return Controls(hass, cloud, vehicle, account)


class TestClimateControls:
    async def test_controls_follow_reported_seats_options_and_fan_bounds(self, controls):
        assert controls.state("Driver Seat Climate").attributes["options"] == [
            "Off",
            "Heat",
            "Ventilate",
        ]
        assert controls.state("Passenger Seat Climate").attributes["options"] == ["Off", "Heat"]
        assert controls.state("Rear Driver Seat Climate").attributes["options"] == [
            "Off",
            "Ventilate",
        ]
        assert controls.ids["Rear Passenger Seat Climate"] is None
        assert controls.state("Climate Airflow").attributes["options"] == [
            "Upper body",
            "Feet",
            "Windshield and feet",
        ]
        fan = controls.state("Climate Fan Speed")
        assert (
            fan.state,
            fan.attributes["min"],
            fan.attributes["max"],
            fan.attributes["step"],
        ) == ("2", 0, 5, 1)

    async def test_airflow_change_preserves_seats_defrosters_and_unknown_settings(self, controls):
        cloud = controls.cloud
        await controls.select_option("Climate Airflow", "Feet")
        assert controls.state("Climate Airflow").state == "Feet"
        assert cloud.server["acOperations"][1:] == SETTINGS["acOperations"][1:]
        assert cloud.server["futureMetadata"] == SETTINGS["futureMetadata"]
        parameters = climate_parameters(cloud.server, "airflow")
        assert not parameters["upperBody"]["enabled"]
        assert parameters["feet"]["enabled"]
        assert not parameters["frontDefrostFeet"]["enabled"]

    async def test_seat_mode_disables_opposite_setting_and_preserves_other_seats(self, controls):
        cloud = controls.cloud
        await controls.select_option("Driver Seat Climate", "Ventilate")
        assert controls.state("Driver Seat Climate").state == "Ventilate"
        assert not climate_parameters(cloud.server, "seatHeat")["frontDriver"]["enabled"]
        assert climate_parameters(cloud.server, "seatVent")["ventFrontDriver"]["enabled"]
        assert climate_parameters(cloud.server, "seatVent")["ventRearDriver"]["enabled"]
        await controls.select_option("Driver Seat Climate", "Off")
        assert controls.state("Driver Seat Climate").state == "Off"
        assert not climate_parameters(cloud.server, "seatVent")["ventFrontDriver"]["enabled"]

    async def test_switches_and_fan_save_independent_preferences(self, controls):
        cloud = controls.cloud
        await controls.turn_off("Front Defroster")
        await controls.turn_on("Rear Defroster")
        await controls.turn_off("Steering Wheel Heat")
        await controls.turn_on("Recirculate Air")
        await controls.turn_on("Longer Climate Runtime")
        await controls.set_value("Climate Fan Speed", 4.0)
        assert controls.state("Front Defroster").state == "off"
        assert controls.state("Rear Defroster").state == "on"
        assert controls.state("Steering Wheel Heat").state == "off"
        assert controls.state("Recirculate Air").state == "on"
        assert controls.state("Longer Climate Runtime").state == "on"
        assert cloud.server["extendedRuntime"]["enabled"]
        assert cloud.server["airFlowVolume"] == 4
        assert type(cloud.server["airFlowVolume"]) is int
        assert cloud.server["temperature"] == 22

    async def test_concurrent_nested_changes_preserve_each_other(self, controls):
        cloud = controls.cloud
        await asyncio.gather(
            controls.select_option("Climate Airflow", "Feet"),
            controls.select_option("Driver Seat Climate", "Ventilate"),
            controls.turn_on("Longer Climate Runtime"),
        )
        assert climate_parameters(cloud.server, "airflow")["feet"]["enabled"]
        assert climate_parameters(cloud.server, "seatVent")["ventFrontDriver"]["enabled"]
        assert cloud.server["extendedRuntime"]["enabled"]

    async def test_fresh_capability_loss_rejects_save_without_changing_cached_preferences(
        self, controls
    ):
        cloud, vehicle = controls.cloud, controls.vehicle
        before = deepcopy(vehicle.climate_settings)
        cloud.server["acOperations"][3]["available"] = False
        with pytest.raises(ServiceValidationError):
            await controls.turn_off("Front Defroster")
        cloud.update_climate_settings.assert_not_awaited()
        assert vehicle.climate_settings == before
        await controls.push()
        assert controls.state("Front Defroster").state == "on"
        await vehicle.update_climate()
        await controls.push()
        assert controls.state("Front Defroster").state == "unavailable"

    async def test_fan_and_unavailable_seat_modes_are_rejected_before_write(
        self, controls, subtests
    ):
        cloud = controls.cloud
        # The set_value service coerces its value to a float and checks it
        # against the range before the entity sees it, so None is rejected by
        # the schema, and True and "4" become 1 and 4, which are valid speeds.
        for value, expected in (
            (-1, ServiceValidationError),
            (6, ServiceValidationError),
            (2.5, ServiceValidationError),
            (float("nan"), ServiceValidationError),
            (float("inf"), ServiceValidationError),
            (None, vol.Invalid),
            (True, 1),
            ("4", 4),
        ):
            with subtests.test(value=value):
                cloud.update_climate_settings.reset_mock()
                if expected in (1, 4):
                    await controls.set_value("Climate Fan Speed", value)
                    cloud.update_climate_settings.assert_awaited_once()
                    assert cloud.server["airFlowVolume"] == expected
                else:
                    with pytest.raises(expected):
                        await controls.set_value("Climate Fan Speed", value)
                    cloud.update_climate_settings.assert_not_awaited()
        cloud.update_climate_settings.reset_mock()
        with pytest.raises(ServiceValidationError):
            await controls.select_option("Passenger Seat Climate", "Ventilate")
        cloud.update_climate_settings.assert_not_awaited()

    async def test_invalid_climate_metadata_is_an_operational_error(self, controls, subtests):
        cloud = controls.cloud
        for name, changes in (
            ("Climate Temperature", {"minTemp": 31}),
            ("Climate Temperature", {"tempInterval": 0}),
            ("Climate Temperature", {"temperature": None}),
            ("Climate Temperature", {"temperature": float("nan")}),
            ("Climate Fan Speed", {"minAirFlow": 6}),
            ("Climate Fan Speed", {"maxAirFlow": 2.5}),
        ):
            cloud.server = {**deepcopy(SETTINGS), **changes}
            value = 23 if name == "Climate Temperature" else 4
            with subtests.test(changes=changes):
                with pytest.raises(HomeAssistantError) as raised:
                    await controls.set_value(name, value)
                assert type(raised.value) is HomeAssistantError
                assert str(raised.value) == "Toyota did not provide a valid climate range."
        cloud.update_climate_settings.assert_not_awaited()
        assert controls.vehicle.climate_settings == SETTINGS

    async def test_invalid_rest_json_is_an_operational_error(self, controls):
        error = json.JSONDecodeError("Expecting value", "invalid", 0)
        response = AsyncMock()
        response.status = 200
        response.json.side_effect = error
        response.__aenter__.return_value = response
        session = MagicMock()
        session.__aenter__.return_value = session
        session.request.return_value = response
        client = types.SimpleNamespace(_auth_headers=AsyncMock(return_value={}))

        async def request(*args):
            return await patch_client.api_request(client, "PUT", "climate-settings")

        with (
            patch.object(patch_client.aiohttp, "ClientSession", return_value=session),
            patch.object(controls.cloud, "update_climate_settings", request),
            pytest.raises(HomeAssistantError) as raised,
        ):
            await controls.set_value("Climate Fan Speed", 4)
        assert type(raised.value) is HomeAssistantError
        assert str(raised.value) == "Toyota returned an invalid response."
        assert raised.value.__cause__ is error
        session.request.assert_called_once()
        assert controls.vehicle.climate_settings == SETTINGS

    async def test_expected_write_failures_preserve_messages_and_reported_state(
        self, controls, subtests
    ):
        cloud = controls.cloud
        changed = Mock()
        remove_listener = controls.account.coordinator.async_add_listener(changed)
        actions = (
            (controls.set_value, ("Climate Fan Speed", 4)),
            (controls.select_option, ("Climate Airflow", "Feet")),
            (controls.select_option, ("Driver Seat Climate", "Off")),
            (controls.turn_off, ("Use Climate Settings",)),
            (controls.turn_off, ("Front Defroster",)),
        )
        for error, message in (
            (RuntimeError("Toyota rejected the change."), "Toyota rejected the change."),
            (ClientConnectionError("Toyota connection failed."), "Toyota connection failed."),
            (AuthError("Toyota session expired."), "Toyota session expired."),
            (TimeoutError(), "The Toyota request timed out."),
            (ClientConnectionError(), "The Toyota request failed. Try again."),
            (RuntimeError(), "The Toyota request failed. Try again."),
        ):
            cloud.update_climate_settings.side_effect = error
            for action, args in actions:
                with (
                    subtests.test(error=type(error), action=args[0]),
                    pytest.raises(HomeAssistantError) as raised,
                ):
                    await action(*args)
                assert type(raised.value) is HomeAssistantError
                assert str(raised.value) == message
                assert raised.value.__cause__ is error
        remove_listener()
        assert controls.vehicle.climate_settings == SETTINGS
        changed.assert_not_called()

    async def test_unavailable_controls_are_skipped_without_writing(
        self, controls, caplog, subtests
    ):
        controls.vehicle._has_remote_subscription = False
        # Home Assistant skips entities that are unavailable when the service
        # runs, logging a warning instead of calling them.
        for action, args in (
            (controls.set_value, ("Climate Fan Speed", 4)),
            (controls.select_option, ("Climate Airflow", "Feet")),
            (controls.turn_off, ("Use Climate Settings",)),
        ):
            with subtests.test(action=args[0]):
                caplog.clear()
                await action(*args)
                assert [
                    record.getMessage()
                    for record in caplog.records
                    if record.levelno == logging.WARNING
                ] == [
                    "Referenced entities "
                    f"{controls.ids[args[0]]} are missing or not currently available"
                ]
        controls.cloud.update_climate_settings.assert_not_awaited()
        await controls.push()
        for name in ("Climate Fan Speed", "Climate Airflow", "Use Climate Settings"):
            assert controls.state(name).state == "unavailable"

    async def test_missing_climate_response_is_an_operational_error(self, controls):
        cloud = controls.cloud
        cloud.get_climate_settings.side_effect = None
        cloud.get_climate_settings.return_value = {}
        with pytest.raises(
            HomeAssistantError, match="Toyota did not return climate settings"
        ) as raised:
            await controls.set_value("Climate Fan Speed", 4)
        assert type(raised.value) is HomeAssistantError
        cloud.update_climate_settings.assert_not_awaited()

    async def test_disabled_custom_settings_can_be_configured_before_enabling(self, controls):
        cloud = controls.cloud
        cloud.server["settingsOn"] = False
        await controls.vehicle.update_climate()
        await controls.push()
        assert controls.state("Use Climate Settings").state == "off"
        await controls.select_option("Driver Seat Climate", "Ventilate")
        assert not cloud.server["settingsOn"]
        assert controls.state("Driver Seat Climate").state == "Ventilate"

    async def test_fan_uses_vehicle_bounds_and_defaults_for_omitted_limits(self, controls):
        cloud = controls.cloud
        cloud.server["minAirFlow"] = None
        cloud.server.pop("maxAirFlow")
        await controls.vehicle.update_climate()
        await controls.push()
        fan = controls.state("Climate Fan Speed")
        assert (fan.attributes["min"], fan.attributes["max"]) == (1, 7)
        await controls.set_value("Climate Fan Speed", 7)
        assert cloud.server["airFlowVolume"] == 7
