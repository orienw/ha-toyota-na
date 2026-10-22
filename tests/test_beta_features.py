"""Generation, feature availability, and electric vehicle controls."""

import types
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest
from common import (
    TWENTY_FOUR_MM_PHEV,
    entity_id,
    make_17cy_vehicle,
    make_24mm_vehicle,
    make_vehicle,
    settle,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from toyota_na.exceptions import LoginError
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric

from custom_components.toyota_na.charging_helpers import CHARGE_SETTINGS
from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    VehicleFeatures,
)
from custom_components.toyota_na.patch_client import get_vehicle_status_21mm
from custom_components.toyota_na.patch_vehicle import get_vehicles

CLIMATE_SETTINGS = {
    "temperature": 22.0,
    "temperatureUnit": "C",
    "minTemp": 18.0,
    "maxTemp": 30.0,
    "tempInterval": 0.5,
    "settingsOn": True,
    "isCustomerSettings": True,
    "acOperations": [{"type": "frontDefogger", "value": "off"}],
    "extendedRuntime": {"enabled": False},
}


async def push(hass, account):
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()


async def count_polls(hass, account):
    """Count the account polls once pending work and any deferred refresh have run."""
    await settle(hass)
    return account.get_vehicles.await_count


def entities(hass, entry, *domains):
    return {
        item.unique_id: item.entity_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain in domains
    }


def duplicates(caplog, domain):
    """Entities Home Assistant rejected for reusing a unique ID in a domain."""
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == f"homeassistant.components.{domain}"
        and "does not generate unique IDs" in record.getMessage()
    ]


def test_feature_states_and_legacy_fallback(subtests):
    vehicle = make_vehicle()
    for flags, available in (
        (None, True),
        ({}, False),
        ({"remoteCommands": 1}, True),
        ({"remoteCommands": 0}, False),
        ({"remoteCommands": 2}, False),
        ({"remoteCommands": None}, False),
        ({"remoteCommands": True}, False),
    ):
        with subtests.test(flags=flags):
            vehicle._feature_flags = flags
            assert vehicle.supports_command(RemoteRequestCommand.EngineStart) == available
    vehicle._feature_flags = None
    vehicle._has_remote_subscription = False
    assert not vehicle.supports_command(RemoteRequestCommand.EngineStart)


@pytest.mark.parametrize(
    "generation",
    [
        generation
        for generation in ApiVehicleGeneration
        if generation != ApiVehicleGeneration.PRE17CY
    ],
)
def test_climate_start_uses_capabilities_and_generation(generation):
    vehicle = make_17cy_vehicle() if generation == ApiVehicleGeneration.CY17 else make_vehicle()
    vehicle._generation = generation
    vehicle._remote_capabilities = {"estartStopCapable": False}
    vehicle._extended_capabilities = {"remoteEConnectCapable": True}
    assert vehicle.supports_command(RemoteRequestCommand.EngineStart)
    vehicle._extended_capabilities = {}
    vehicle._legacy_capabilities = [{"name": "evremoteservice"}]
    assert vehicle.supports_command(RemoteRequestCommand.EngineStart) == (
        generation == ApiVehicleGeneration.CY17
    )


async def test_26bev_discovery_routes_poll_commands_refresh_and_push(hass, setup_vehicles):
    metadata = {**TWENTY_FOUR_MM_PHEV, "vin": "SYNTHETIC26BEV", "generation": "26BEV"}
    client = types.SimpleNamespace(
        get_user_vehicle_list=AsyncMock(return_value=[metadata]),
        get_telemetry=AsyncMock(return_value={}),
        graphql_get_vehicle_status=AsyncMock(return_value={}),
        remote_request_24mm=AsyncMock(),
        graphql_pre_wake=AsyncMock(),
        graphql_confirm_subscription=AsyncMock(),
        graphql_refresh_status=AsyncMock(),
        auth=types.SimpleNamespace(get_guid=AsyncMock(return_value="guid")),
    )
    (vehicle,) = await get_vehicles(client)
    assert vehicle.api_generation == "26BEV"
    assert vehicle.endpoint_generation == "17CYPLUS"
    client.graphql_get_vehicle_status.assert_awaited_once_with("SYNTHETIC26BEV", "hatch", "CA")
    await vehicle.send_command(RemoteRequestCommand.EngineStart)
    client.remote_request_24mm.assert_awaited_once_with("SYNTHETIC26BEV", "engine-start", "CA")
    await vehicle.poll_vehicle_refresh()
    client.graphql_refresh_status.assert_awaited_once_with("SYNTHETIC26BEV", "CA")

    account = await setup_vehicles([vehicle])

    account.websocket.start.assert_awaited_once_with(
        {"SYNTHETIC26BEV": {"region": "CA", "backdoor_type": "hatch"}}
    )


@pytest.mark.parametrize("generation", [ApiVehicleGeneration.MM24, ApiVehicleGeneration.BEV26])
async def test_unsubscribed_appsync_ev_can_read_but_cannot_command_or_wake(
    hass, setup_vehicles, generation
):
    client = types.SimpleNamespace(
        get_telemetry=AsyncMock(return_value={}),
        graphql_get_vehicle_status=AsyncMock(
            return_value={
                "electric": {
                    "battery": {"chargeRemainingAmount": {"value": 63, "unit": "%"}},
                }
            }
        ),
    )
    vehicle = make_24mm_vehicle(client)
    vehicle._generation = generation
    vehicle._has_remote_subscription = False
    vehicle._feature_flags = {"evBattery": 1, "remoteCommands": 0, "vehicleState": 0}
    await vehicle.update()
    account = await setup_vehicles([vehicle])
    battery = entity_id(hass, "sensor", "TESTVIN24.EV Battery Level")
    assert hass.states.get(battery).state == "63"
    assert not vehicle.supports_command(RemoteRequestCommand.EngineStart)
    assert not vehicle.supports_command(RemoteRequestCommand.Refresh)
    assert vehicle.vin in account.websocket.start.await_args.args[0]

    vehicle._feature_flags["evBattery"] = 2
    client.graphql_get_vehicle_status.reset_mock()
    client.graphql_get_vehicle_status.return_value["electric"]["battery"]["chargeRemainingAmount"][
        "value"
    ] = 64
    await vehicle.update()
    client.graphql_get_vehicle_status.assert_awaited_once()
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(battery).state == "64"
    assert vehicle.vin in account.websocket.update_vehicle_contexts.await_args.args[0]


async def test_app_feature_flags_do_not_hide_observed_sensor_values(hass, setup_vehicles, subtests):
    vehicle = make_vehicle()
    # Home Assistant only adds the battery sensor for an electric vehicle.
    vehicle._has_electric = True
    vehicle.features[VehicleFeatures.ChargeLevel] = ToyotaNumeric(80, "%")
    account = await setup_vehicles([vehicle])
    sensor = entity_id(hass, "sensor", "TESTVIN.EV Battery Level")
    assert hass.states.get(sensor).state == "80"
    for flags in ({}, {"evBattery": 0}, {"evBattery": 2}):
        with subtests.test(flags=flags):
            vehicle._feature_flags = flags
            await push(hass, account)
            assert hass.states.get(sensor).state == "80"


async def test_app_maintenance_does_not_hide_observed_lock_state(hass, setup_vehicles):
    vehicle = make_vehicle()
    vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(
        closed=True,
        locked=True,
    )
    account = await setup_vehicles([vehicle])
    lock = entity_id(hass, "lock", "TESTVIN.")
    assert hass.states.get(lock).state == "locked"
    vehicle._feature_flags = {"vehicleState": 2, "remoteCommands": 1}
    await push(hass, account)
    assert hass.states.get(lock).state == "locked"
    vehicle._feature_flags["vehicleState"] = 1
    await push(hass, account)
    assert hass.states.get(lock).state == "locked"


async def test_21mm_zero_status_locked_values_reach_lock_controls(hass, setup_vehicles, subtests):
    class Client:
        get_vehicle_status_21mm = get_vehicle_status_21mm
        api_get = AsyncMock()
        get_telemetry = AsyncMock(return_value=None)
        get_engine_status_21mm = AsyncMock(return_value=None)
        remote_request_21mm = AsyncMock()

    client = Client()
    vehicle = make_vehicle(client)
    account = await setup_vehicles([vehicle])
    lock = entity_id(hass, "lock", "TESTVIN.")

    for value, flag, expected in (
        ("locked", 0, True),
        ("unlocked", 1, False),
        ("locked", 0, True),
    ):
        with subtests.test(value=value, flag=flag):
            client.api_get.return_value = {
                "status": {
                    "vehicleStatus": [
                        {
                            "category": category,
                            "sections": [
                                {
                                    "section": "Door",
                                    "values": [
                                        {"value": "closed", "status": 0},
                                        {"value": value, "status": flag},
                                    ],
                                }
                            ],
                        }
                        for category in ("Driver Side", "Passenger Side")
                    ]
                }
            }
            await vehicle.update()
            await push(hass, account)

            reported = "locked" if expected else "unlocked"
            assert hass.states.get(lock).state == reported
            assert vehicle.features[VehicleFeatures.FrontDriverDoor].closed
            assert vehicle.features[VehicleFeatures.FrontPassengerDoor].closed
            await hass.services.async_call(
                "lock", "unlock" if expected else "lock", {"entity_id": lock}, blocking=True
            )
            await hass.async_block_till_done()
            client.remote_request_21mm.assert_awaited_with(
                vehicle.vin,
                "door-unlock" if expected else "door-lock",
                "US",
            )
            # Once the command's refresh finishes, the lock is no longer locking or unlocking.
            assert hass.states.get(lock).state == reported


async def test_legacy_reads_and_entities_do_not_require_remote_subscription(hass, setup_vehicles):
    client = types.SimpleNamespace(
        get_telemetry=AsyncMock(return_value={}),
        get_engine_status_17cy=AsyncMock(return_value={"status": "off"}),
        get_electric_status=AsyncMock(
            return_value={
                "vehicleInfo": {
                    "chargeInfo": {"chargeRemainingAmount": 71, "plugStatus": 40},
                }
            }
        ),
    )
    vehicle = make_17cy_vehicle(client)
    vehicle._has_remote_subscription = False
    vehicle._feature_flags = {"evBattery": 0, "evVehicleStatus": 2}
    await vehicle.update()
    client.get_engine_status_17cy.assert_awaited_once()
    vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(
        closed=True,
        locked=True,
    )
    await setup_vehicles([vehicle])
    assert hass.states.get(entity_id(hass, "sensor", "TESTVIN.EV Battery Level")).state == "71"
    assert entity_id(hass, "binary_sensor", "TESTVIN.Front Driver Door") is not None
    assert not vehicle.supports_command(RemoteRequestCommand.ChargeStop)


def test_subscription_changes_preserve_last_known_vehicle_observations():
    previous = make_24mm_vehicle()
    previous.features[VehicleFeatures.ChargeLevel] = ToyotaNumeric(62, "%")
    vehicle = make_24mm_vehicle()
    vehicle._has_remote_subscription = False
    assert vehicle.inherit_state(previous)
    assert vehicle.features[VehicleFeatures.ChargeLevel].value == 62
    assert not vehicle.supports_command(RemoteRequestCommand.EngineStart)


@pytest.mark.parametrize("factory", [make_17cy_vehicle, make_24mm_vehicle])
async def test_expired_login_during_optional_vehicle_reads_reaches_reauthentication(factory):
    client = types.SimpleNamespace(get_telemetry=AsyncMock(side_effect=LoginError()))
    vehicle = factory(client)
    vehicle._has_remote_subscription = False
    with pytest.raises(LoginError):
        await vehicle.update()


async def test_services_enforce_live_feature_and_charging_state(hass, setup_vehicles, subtests):
    client = types.SimpleNamespace(remote_request_24mm=AsyncMock())
    vehicle = make_24mm_vehicle(client)
    vehicle._feature_flags = {"remoteCommands": 1, "evVehicleStatus": 1, "vehicleState": 2}
    vehicle.features[VehicleFeatures.ChargingState] = ToyotaNumeric("charging", "")
    account = await setup_vehicles([vehicle])
    device_id = next(
        iter(er.async_entries_for_config_entry(er.async_get(hass), account.entry.entry_id))
    ).device_id
    polls = await count_polls(hass, account)

    for service in ("charge_start", "refresh"):
        with subtests.test(service=service), pytest.raises(ServiceValidationError):
            await hass.services.async_call(DOMAIN, service, {"vehicle": device_id}, blocking=True)
    client.remote_request_24mm.assert_not_awaited()
    assert await count_polls(hass, account) == polls

    await hass.services.async_call(DOMAIN, "charge_stop", {"vehicle": device_id}, blocking=True)
    client.remote_request_24mm.assert_awaited_once_with(vehicle.vin, "charge-stop", "CA")
    assert await count_polls(hass, account) == polls + 1
    vehicle._feature_flags["remoteCommands"] = 2
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "charge_stop", {"vehicle": device_id}, blocking=True)
    assert client.remote_request_24mm.await_count == 1


def test_reported_stolen_turns_off_commands_and_setting_writes(subtests):
    vehicle = make_24mm_vehicle()
    vehicle._extended_capabilities = {**vehicle.extended_capabilities, "climateCapable": True}
    vehicle._charge_settings["schedules"] = []
    field = next(iter(CHARGE_SETTINGS))
    for remote_display, offered in (
        (None, True),
        (7, True),
        (1, True),
        (9, True),
        (10, False),
        (11, False),
    ):
        with subtests.test(remote_display=remote_display):
            vehicle._remote_display = remote_display
            assert vehicle.stolen == (not offered)
            assert [
                vehicle.supports_command(RemoteRequestCommand.DoorLock),
                vehicle.supports_command(RemoteRequestCommand.Refresh),
                vehicle.supports_climate_settings,
                vehicle.supports_climate_schedules,
                vehicle.supports_charge_setting(field),
                vehicle.supports_charge_schedules,
            ] == [offered] * 6


async def test_stolen_vehicle_commands_return_after_recovery(hass, setup_vehicles):
    vehicle = make_vehicle()
    vehicle._remote_display = 10
    account = await setup_vehicles([vehicle])
    assert entities(hass, account.entry, "button", "lock") == {}
    vehicle._remote_display = 7
    await push(hass, account)
    controls = entities(hass, account.entry, "button", "lock")
    assert {"TESTVIN.Flash Hazards", "TESTVIN.Refresh Status", "TESTVIN."} <= set(controls)
    assert all(hass.states.get(control).state != "unavailable" for control in controls.values())
    vehicle._remote_display = 11
    await push(hass, account)
    assert all(hass.states.get(control).state == "unavailable" for control in controls.values())


async def test_stolen_vehicle_keeps_showing_its_data(hass, setup_vehicles):
    vehicle = make_vehicle()
    vehicle._parse_telemetry({"fuelLevel": 61, "lastTimestamp": "2026-09-21T07:00:00Z"})
    account = await setup_vehicles([vehicle])
    fuel = entity_id(hass, "sensor", "TESTVIN.Fuel Level")
    vehicle._remote_display = 10
    await push(hass, account)
    assert hass.states.get(fuel).state == "61"


async def test_charging_buttons_follow_state_without_duplicate_entities(
    hass, setup_vehicles, subtests, caplog
):
    vehicle = make_24mm_vehicle()
    account = await setup_vehicles([vehicle])
    charge_names = ("Charge Now", "Resume Charging", "Stop Charging")

    def available_buttons():
        return [
            name
            for name in charge_names
            if (button := entity_id(hass, "button", f"TESTVIN24.{name}"))
            and hass.states.get(button).state != "unavailable"
        ]

    for state, expected in (
        ("charge_now", "Charge Now"),
        ("charging", "Stop Charging"),
        ("resume_charging", "Resume Charging"),
        ("unavailable", None),
        ("charge_now", "Charge Now"),
    ):
        with subtests.test(state=state):
            vehicle._parse_graphql_electric_status({"charging": {"chargingState": state}})
            await push(hass, account)
            assert available_buttons() == ([expected] if expected else [])
    # Home Assistant rejects a button added twice as a duplicate unique ID.
    assert duplicates(caplog, "button") == []


@pytest.mark.parametrize(
    "generation",
    [
        ApiVehicleGeneration.CY17,
        ApiVehicleGeneration.CY17PLUS,
        ApiVehicleGeneration.MM21,
        ApiVehicleGeneration.MM24,
        ApiVehicleGeneration.BEV26,
    ],
)
async def test_charging_state_selects_only_applicable_commands(generation, subtests):
    client = types.SimpleNamespace(
        electric_command=AsyncMock(return_value={}), remote_request_24mm=AsyncMock()
    )
    vehicle = (
        make_17cy_vehicle(client)
        if generation == ApiVehicleGeneration.CY17
        else make_24mm_vehicle(client)
    )
    vehicle._generation = generation
    for state, command, wire_command in (
        ("36", RemoteRequestCommand.ChargeStart, "immediate-charge"),
        ("charge_now", RemoteRequestCommand.ChargeStart, "immediate-charge"),
        ("resume_charging", RemoteRequestCommand.ChargeResume, "resume-charge"),
        ("charging", RemoteRequestCommand.ChargeStop, "charge-stop"),
    ):
        with subtests.test(state=state):
            if vehicle.uses_appsync:
                vehicle._parse_graphql_electric_status({"charging": {"chargingState": state}})
            else:
                vehicle._parse_electric_status(
                    {"vehicleInfo": {"chargeInfo": {"plugStatus": state}}}
                )
            expected = command == RemoteRequestCommand.ChargeStart or vehicle.uses_appsync
            assert vehicle.supports_command(command) == expected
            if expected:
                await vehicle.send_command(command)
                if vehicle.uses_appsync:
                    client.remote_request_24mm.assert_awaited_with(
                        vehicle.vin, wire_command, vehicle.region
                    )
                else:
                    client.electric_command.assert_awaited_with(
                        vehicle.vin,
                        generation.value,
                        wire_command,
                        vehicle.region,
                        vehicle.brand,
                    )
            else:
                with pytest.raises(ValueError):
                    await vehicle.send_command(command)
    for state in (
        "40",
        "56",
        "45",
        "60",
        "no_controls",
        "unavailable",
        "external_power_active",
        "unexpected",
    ):
        vehicle.features[VehicleFeatures.ChargingState] = ToyotaNumeric(state, "")
        for command in (
            RemoteRequestCommand.ChargeStart,
            RemoteRequestCommand.ChargeResume,
            RemoteRequestCommand.ChargeStop,
        ):
            assert not vehicle.supports_command(command), (generation, state, command)


def test_older_charging_observations_cannot_reenable_commands():
    vehicle = make_24mm_vehicle()
    vehicle._parse_graphql_electric_status(
        {
            "charging": {
                "chargingState": "no_controls",
                "lastUpdateDateTime": "2026-09-14T19:01:00Z",
            }
        }
    )
    vehicle._parse_graphql_electric_status(
        {
            "charging": {
                "chargingState": "charge_now",
                "lastUpdateDateTime": "2026-09-14T19:00:00Z",
            }
        }
    )
    assert not vehicle.supports_command(RemoteRequestCommand.ChargeStart)


@pytest.fixture
def climate():
    client = types.SimpleNamespace(
        get_climate_settings=AsyncMock(return_value=deepcopy(CLIMATE_SETTINGS)),
        update_climate_settings=AsyncMock(),
    )
    vehicle = make_vehicle(client)
    vehicle._extended_capabilities = {
        **vehicle._extended_capabilities,
        "climateCapable": True,
    }
    return client, vehicle


async def test_partial_update_preserves_latest_unrelated_preferences(climate):
    client, vehicle = climate
    vehicle._climate_settings = {**CLIMATE_SETTINGS, "acOperations": []}
    await vehicle.update_climate_settings(temperature=23.5)
    expected = {**CLIMATE_SETTINGS, "temperature": 23.5}
    client.update_climate_settings.assert_awaited_once_with("TESTVIN", "21MM", expected, "US", "L")
    assert vehicle.climate_settings == expected
    assert client.get_climate_settings.return_value == CLIMATE_SETTINGS


async def test_temperature_range_and_step_are_checked_before_writing(climate, subtests):
    client, vehicle = climate
    for temperature in (17, 31, 22.25, float("nan"), float("inf")):
        with (
            subtests.test(temperature=temperature),
            pytest.raises(ValueError, match="finite number|range and step"),
        ):
            await vehicle.update_climate_settings(temperature=temperature)
    client.update_climate_settings.assert_not_awaited()


async def test_unavailable_climate_does_not_read_or_write(climate):
    client, vehicle = climate
    vehicle._feature_flags = {"remoteClimate": 2}
    await vehicle.update_climate()
    with pytest.raises(ValueError):
        await vehicle.update_climate_settings(temperature=23)
    client.get_climate_settings.assert_not_awaited()
    client.update_climate_settings.assert_not_awaited()


async def test_native_controls_follow_vehicle_units_bounds_and_availability(
    hass, setup_vehicles, climate
):
    client, vehicle = climate
    await vehicle.update_climate()
    account = await setup_vehicles([vehicle])
    assert set(entities(hass, account.entry, "number", "switch")) == {
        "TESTVIN.Climate Temperature",
        "TESTVIN.Use Climate Settings",
    }
    temperature = entity_id(hass, "number", "TESTVIN.Climate Temperature")
    enabled = entity_id(hass, "switch", "TESTVIN.Use Climate Settings")
    state = hass.states.get(temperature)
    assert (
        float(state.state),
        state.attributes["min"],
        state.attributes["max"],
        state.attributes["step"],
        state.attributes["unit_of_measurement"],
    ) == (22, 18, 30, 0.5, "°C")
    await hass.services.async_call(
        "number", "set_value", {"entity_id": temperature, "value": 23}, blocking=True
    )
    assert float(hass.states.get(temperature).state) == 23
    await hass.services.async_call("switch", "turn_off", {"entity_id": enabled}, blocking=True)
    assert hass.states.get(enabled).state == "off"
    vehicle._feature_flags = {"remoteClimate": 2}
    await push(hass, account)
    assert hass.states.get(temperature).state == "unavailable"
    assert hass.states.get(enabled).state == "unavailable"
    # Home Assistant skips an unavailable entity, so the switch never reaches the vehicle.
    writes = client.update_climate_settings.await_count
    await hass.services.async_call("switch", "turn_on", {"entity_id": enabled}, blocking=True)
    assert client.update_climate_settings.await_count == writes


async def test_save_remains_visible_when_poll_replaces_vehicle(hass, setup_vehicles, climate):
    client, vehicle = climate
    await vehicle.update_climate()
    replacement = make_vehicle(client)
    replacement._extended_capabilities = dict(vehicle._extended_capabilities)
    replacement.inherit_state(vehicle)
    account = await setup_vehicles([vehicle])
    account.get_vehicles.return_value = [replacement]
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert account.coordinator.data == [replacement]

    await vehicle.update_climate_settings(temperature=24)
    await push(hass, account)

    temperature = entity_id(hass, "number", "TESTVIN.Climate Temperature")
    assert float(hass.states.get(temperature).state) == 24
