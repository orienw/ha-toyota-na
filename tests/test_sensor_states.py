"""Readable sensor states retain the existing entity IDs."""

import pytest
from common import FakeVehicle, entity_id
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.helpers import entity_registry as er
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric

from custom_components.toyota_na.patch_base_vehicle import VehicleFeatures as F

SENSORS = (
    "Plug Status",
    "Connector Status",
    "Last Update Timestamp",
    "Last Tire Pressure Update Timestamp",
    "Speed",
    "Fuel Level",
)


def sensor_unique_ids(hass, entry):
    return {
        entry.unique_id
        for entry in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if entry.domain == "sensor"
    }


@pytest.fixture
async def sensors(hass, setup_vehicles):
    vehicle = FakeVehicle(set())
    vehicle.electric = True
    vehicle.features = {
        F.PlugStatus: ToyotaNumeric(40, ""),
        F.ConnectorStatus: ToyotaNumeric(5, ""),
        F.LastTimeStamp: ToyotaNumeric(1789473600, ""),
        F.LastTirePressureTimeStamp: ToyotaNumeric(1789470000, ""),
        F.Speed: ToyotaNumeric(100, "km/h"),
        F.FuelLevel: ToyotaNumeric(79, "%"),
    }
    account = await setup_vehicles([vehicle])

    assert sensor_unique_ids(hass, account.entry) == {f"TESTVIN.{name}" for name in SENSORS}
    registry = er.async_get(hass)
    ids = {name: entity_id(hass, "sensor", f"TESTVIN.{name}") for name in SENSORS}
    for name in SENSORS:
        assert registry.async_get(ids[name]).disabled_by is None

    async def update(feature, value, unit=""):
        vehicle.features[feature] = ToyotaNumeric(value, unit)
        account.coordinator.async_set_updated_data(account.coordinator.data)
        await hass.async_block_till_done()

    return vehicle, ids, update


async def test_plug_states_cover_legacy_and_appsync_values(hass, sensors, subtests):
    _, ids, update = sensors
    for raw, expected in (
        (12, "unplugged"),
        (36, "waiting"),
        (40, "charging"),
        (45, "charge_complete"),
        (56, "fast_charging"),
        (60, "fast_charge_complete"),
        ("PLUGGED_IN", "plugged_in"),
        ("charging", "charging"),
        ("charge_now", "waiting"),
        ("resume_charging", "paused"),
        ("external_power_active", "power_supply"),
        ("external_power_active_hybrid", "power_supply"),
        ("no_controls", "unplugged"),
        ("unavailable", "unplugged"),
        (987, None),
        (None, None),
    ):
        with subtests.test(raw=raw):
            await update(F.PlugStatus, raw)
            state = hass.states.get(ids["Plug Status"])
            assert state.state == (expected or "unknown")
            assert state.attributes["raw_value"] == raw
            if expected is not None:
                assert expected in state.attributes["options"]
    assert "state_class" not in state.attributes
    assert "unit_of_measurement" not in state.attributes
    assert state.attributes["device_class"] == SensorDeviceClass.ENUM


async def test_connector_states_leave_unrecognized_codes_unknown(hass, sensors, subtests):
    _, ids, update = sensors
    for raw, expected in (
        (2, "disconnected"),
        (4, "unlocked"),
        (5, "locked"),
        ("connected", "connected"),
        ("LOCKED", "locked"),
        (987, None),
    ):
        with subtests.test(raw=raw):
            await update(F.ConnectorStatus, raw)
            state = hass.states.get(ids["Connector Status"])
            assert state.state == (expected or "unknown")
            assert state.attributes["raw_value"] == raw
    assert state.attributes["device_class"] == SensorDeviceClass.ENUM
    assert "state_class" not in state.attributes


async def test_timestamps_are_timezone_aware(hass, sensors, subtests):
    _, ids, _ = sensors
    for name, hour in (
        ("Last Update Timestamp", 12),
        ("Last Tire Pressure Update Timestamp", 11),
    ):
        with subtests.test(name=name):
            state = hass.states.get(ids[name])
            assert state.state == f"2026-09-15T{hour}:00:00+00:00"
            assert state.attributes["device_class"] == SensorDeviceClass.TIMESTAMP
            assert "unit_of_measurement" not in state.attributes
            assert "state_class" not in state.attributes


async def test_fuel_level_clamps_display_without_changing_raw_value(hass, sensors, subtests):
    vehicle, ids, update = sensors
    for raw, expected in (
        (-1, "0"),
        (0, "0"),
        (79, "79"),
        (79.5, "79.5"),
        (100, "100"),
        (104.0, "100"),
        (None, "unknown"),
        ("75", "75"),
    ):
        with subtests.test(raw=raw):
            await update(F.FuelLevel, raw, "%")
            state = hass.states.get(ids["Fuel Level"])
            assert state.state == expected
            assert vehicle.features[F.FuelLevel].value == raw
    assert state.attributes["unit_of_measurement"] == "%"


async def test_speed_uses_reported_units_with_legacy_fallback(hass, sensors, subtests):
    _, ids, update = sensors
    for unit, expected in (("km/h", "km/h"), ("mph", "mph"), ("", "km/h")):
        with subtests.test(unit=unit):
            await update(F.Speed, 100, unit)
            state = hass.states.get(ids["Speed"])
            assert state.state == "100"
            assert state.attributes["unit_of_measurement"] == expected
            assert state.attributes["device_class"] == SensorDeviceClass.SPEED


@pytest.fixture
async def remote_access(hass, setup_vehicles):
    vehicle = FakeVehicle(set())
    vehicle.remote_display = 7
    other = FakeVehicle(set(), vin="OTHERVIN")
    account = await setup_vehicles([vehicle, other])
    assert sensor_unique_ids(hass, account.entry) == {"TESTVIN.Remote Access"}

    async def refresh():
        account.coordinator.async_set_updated_data(account.coordinator.data)
        await hass.async_block_till_done()

    return vehicle, other, account, entity_id(hass, "sensor", "TESTVIN.Remote Access"), refresh


async def test_states_follow_the_app_banners(hass, remote_access, subtests):
    vehicle, _, _, sensor, refresh = remote_access
    for raw, expected in (
        (0, "unsupported"),
        (1, "authorization_required"),
        (2, "subscription_cancelled"),
        (3, "subscription_cancelled"),
        (4, "activation_failed"),
        (5, "activation_pending"),
        (6, "activation_error"),
        (7, "active"),
        (8, "subscription_expired"),
        (9, "subscription_expired"),
        (10, "stolen"),
        (11, "stolen_immobilizer"),
        (12, None),
        (-1, None),
    ):
        with subtests.test(raw=raw):
            vehicle.remote_display = raw
            await refresh()
            state = hass.states.get(sensor)
            assert state.state == (expected or "unknown")
            assert state.attributes["raw_value"] == raw
            if expected is not None:
                assert expected in state.attributes["options"]
    assert state.attributes["device_class"] == SensorDeviceClass.ENUM
    assert er.async_get(hass).async_get(sensor).entity_category is EntityCategory.DIAGNOSTIC


async def test_unavailable_when_the_vehicle_list_drops_the_state(hass, remote_access):
    vehicle, _, _, sensor, refresh = remote_access
    vehicle.remote_display = None
    await refresh()

    state = hass.states.get(sensor)
    assert state.state == "unavailable"
    assert "raw_value" not in state.attributes


async def test_added_once_a_vehicle_reports_the_state(hass, remote_access):
    _, other, account, _, refresh = remote_access
    other.remote_display = 1
    await refresh()

    assert sensor_unique_ids(hass, account.entry) == {
        "TESTVIN.Remote Access",
        "OTHERVIN.Remote Access",
    }
    assert hass.states.get(entity_id(hass, "sensor", "OTHERVIN.Remote Access")).state == (
        "authorization_required"
    )
