"""Vehicle controls, door and location entities, and polling in a real Home Assistant."""

import logging

import pytest
from common import FakeVehicle, account_entry, entity_id, settle
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    async_capture_events,
)
from toyota_na.vehicle.entity_types.ToyotaLocation import ToyotaLocation
from toyota_na.vehicle.entity_types.ToyotaLockableOpening import ToyotaLockableOpening
from toyota_na.vehicle.entity_types.ToyotaNumeric import ToyotaNumeric
from toyota_na.vehicle.entity_types.ToyotaOpening import ToyotaOpening

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    VehicleFeatures,
)
from custom_components.toyota_na.wake_policy import (
    CONF_WAKE_INTERVAL,
    LAST_VEHICLE_WAKES,
    LAST_WAKE_AT,
)

LC_CONTROLS = ["Remote Start", "Remote Stop", "Flash Hazards", "Find Vehicle", "Refresh Status"]
STALE_TRUNK = ["binary_sensor.testvin_trunk", "binary_sensor.testvin_trunk_door_lock"]


def registry_entries(hass, entry, domain):
    return [
        item
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain == domain
    ]


async def update(hass, account):
    account.coordinator.async_set_updated_data(account.coordinator.data)
    await hass.async_block_till_done()


def add_registry_entries(hass, entities):
    """Register entities an earlier version created, before setup runs."""
    registry = er.async_get(hass)
    for unique_id, entity in entities.items():
        registry.async_get_or_create(
            "binary_sensor", DOMAIN, unique_id, suggested_object_id=entity.split(".")[1]
        )
    return async_capture_events(hass, er.EVENT_ENTITY_REGISTRY_UPDATED)


def removed(events):
    return [event.data["entity_id"] for event in events if event.data["action"] == "remove"]


class TestButtons:
    @pytest.fixture
    async def controls(self, hass, setup_vehicles):
        vehicle = FakeVehicle(
            {
                RemoteRequestCommand.DoorLock,
                RemoteRequestCommand.DoorUnlock,
                RemoteRequestCommand.EngineStart,
                RemoteRequestCommand.EngineStop,
                RemoteRequestCommand.HazardsOn,
                RemoteRequestCommand.VehicleFinder,
            }
        )
        account = await setup_vehicles([vehicle])
        await settle(hass)
        return vehicle, account

    async def press(self, hass, name):
        await hass.services.async_call(
            "button",
            "press",
            {"entity_id": entity_id(hass, "button", f"TESTVIN.{name}")},
            blocking=True,
        )
        await hass.async_block_till_done()

    async def test_creates_only_supported_lc_controls(self, hass, controls):
        _, account = controls
        buttons = registry_entries(hass, account.entry, "button")
        assert [item.unique_id for item in buttons] == [f"TESTVIN.{name}" for name in LC_CONTROLS]
        assert [item.original_name for item in buttons] == LC_CONTROLS
        assert all(item.has_entity_name for item in buttons)
        assert hass.states.get(buttons[0].entity_id).attributes["friendly_name"] == (
            "2024 LC 500 2-DOOR COUPE Remote Start"
        )

    async def test_command_button_does_not_issue_an_extra_vehicle_wake(self, hass, controls):
        vehicle, account = controls
        polls = account.get_vehicles.await_count
        await self.press(hass, "Remote Start")
        await settle(hass)

        assert vehicle.sent == [RemoteRequestCommand.EngineStart]
        assert vehicle.refresh_requests == 0
        assert account.get_vehicles.await_count == polls + 1
        data = account.entry.data
        assert LAST_WAKE_AT in data
        assert data[LAST_VEHICLE_WAKES] == [{"vin": "TESTVIN", "timestamp": data[LAST_WAKE_AT]}]

    async def test_refresh_button_explicitly_requests_vehicle_status(self, hass, controls):
        vehicle, account = controls
        polls = account.get_vehicles.await_count
        await self.press(hass, "Refresh Status")
        await settle(hass)

        assert vehicle.refresh_requests == 1
        assert account.get_vehicles.await_count == polls + 1
        assert LAST_WAKE_AT in account.entry.data
        assert account.entry.data[LAST_VEHICLE_WAKES][0]["vin"] == "TESTVIN"

    async def test_lock_command_does_not_issue_an_extra_vehicle_wake(self, hass, controls):
        vehicle, account = controls
        polls = account.get_vehicles.await_count
        await hass.services.async_call(
            "lock", "lock", {"entity_id": entity_id(hass, "lock", "TESTVIN.")}, blocking=True
        )
        await settle(hass)

        assert vehicle.sent == [RemoteRequestCommand.DoorLock]
        assert vehicle.refresh_requests == 0
        assert account.get_vehicles.await_count == polls + 1
        assert LAST_WAKE_AT in account.entry.data

    async def test_primary_lock_ignores_other_lockable_openings(self, hass, controls, subtests):
        vehicle, account = controls
        lock = entity_id(hass, "lock", "TESTVIN.")
        doors = (
            VehicleFeatures.FrontDriverDoor,
            VehicleFeatures.FrontPassengerDoor,
            VehicleFeatures.RearDriverDoor,
            VehicleFeatures.RearPassengerDoor,
        )
        for door in doors:
            for locked in (True, False):
                with subtests.test(door=door, locked=locked):
                    vehicle.features = {
                        key: ToyotaLockableOpening(closed=True, locked=True) for key in doors
                    }
                    vehicle.features[door] = ToyotaLockableOpening(closed=True, locked=locked)
                    for key in (
                        VehicleFeatures.Trunk,
                        VehicleFeatures.Hood,
                        VehicleFeatures.GlassHatch,
                    ):
                        vehicle.features[key] = ToyotaLockableOpening(
                            closed=True, locked=not locked
                        )
                    await update(hass, account)
                    assert hass.states.get(lock).state == ("locked" if locked else "unlocked")

    async def test_primary_lock_is_unknown_without_reported_door_locks(
        self, hass, controls, subtests
    ):
        vehicle, account = controls
        lock = entity_id(hass, "lock", "TESTVIN.")
        for door in (
            None,
            ToyotaOpening(True),
            ToyotaOpening(False),
            ToyotaLockableOpening(closed=True, locked=None),
        ):
            for cargo_locked in (True, False):
                with subtests.test(door=door, cargo_locked=cargo_locked):
                    vehicle.features = {
                        VehicleFeatures.FrontDriverDoor: door,
                        VehicleFeatures.Trunk: ToyotaLockableOpening(
                            closed=True, locked=cargo_locked
                        ),
                    }
                    await update(hass, account)
                    assert hass.states.get(lock).state == "unknown"

    async def test_lock_availability_tracks_capabilities(self, hass, controls):
        vehicle, account = controls
        [lock] = registry_entries(hass, account.entry, "lock")
        assert lock.has_entity_name
        assert lock.original_name is None
        assert lock.unique_id == "TESTVIN."
        assert hass.states.get(lock.entity_id).attributes["friendly_name"] == (
            "2024 LC 500 2-DOOR COUPE"
        )

        vehicle.supported.remove(RemoteRequestCommand.DoorUnlock)
        await update(hass, account)

        assert hass.states.get(lock.entity_id).state == "unavailable"


class TestBinarySensorCleanup:
    @pytest.mark.parametrize("closed", [True, False])
    async def test_position_only_door_does_not_report_lock_state(
        self, hass, setup_vehicles, closed
    ):
        vehicle = FakeVehicle(set())
        vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaOpening(closed)
        account = await setup_vehicles([vehicle])
        lock = entity_id(hass, "binary_sensor", "TESTVIN.Front Driver Door Lock")
        door = entity_id(hass, "binary_sensor", "TESTVIN.Front Driver Door")
        assert hass.states.get(lock).attributes["device_class"] == "lock"
        assert hass.states.get(door).attributes["device_class"] == "door"
        assert hass.states.get(lock).state == "unavailable"
        assert hass.states.get(door).state == ("off" if closed else "on")

        vehicle.features[VehicleFeatures.FrontDriverDoor] = ToyotaLockableOpening(
            closed=None, locked=True
        )
        await update(hass, account)

        assert hass.states.get(lock).state == "off"
        assert hass.states.get(door).state == "unavailable"

    async def test_tailgate_removes_stale_trunk_entities(self, hass, setup_vehicles):
        vehicle = FakeVehicle(set())
        vehicle.backdoor_type = "tailgate"
        events = add_registry_entries(
            hass, dict(zip(("TESTVIN.Trunk", "TESTVIN.Trunk Door Lock"), STALE_TRUNK))
        )

        await setup_vehicles([vehicle])

        assert removed(events) == STALE_TRUNK

    @pytest.mark.parametrize("backdoor_type", ["tailgate", "Tailgate"])
    async def test_tailgate_replaces_trunk_entities_with_tailgate(
        self, hass, setup_vehicles, backdoor_type
    ):
        vehicle = FakeVehicle(set())
        vehicle.backdoor_type = backdoor_type
        vehicle.features[VehicleFeatures.Trunk] = ToyotaLockableOpening(closed=False, locked=False)
        events = add_registry_entries(
            hass,
            {
                "TESTVIN.Trunk": STALE_TRUNK[0],
                "TESTVIN.Trunk Door Lock": STALE_TRUNK[1],
                "TESTVIN.Tailgate": "binary_sensor.testvin_tailgate",
            },
        )

        account = await setup_vehicles([vehicle])

        assert removed(events) == STALE_TRUNK
        sensors = {
            item.original_name: item
            for item in registry_entries(hass, account.entry, "binary_sensor")
        }
        assert set(sensors) == {"Tailgate", "Tailgate Lock"}
        assert sensors["Tailgate"].unique_id == "TESTVIN.Tailgate"
        assert sensors["Tailgate Lock"].unique_id == "TESTVIN.Tailgate Lock"
        assert hass.states.get(sensors["Tailgate"].entity_id).state == "on"
        assert hass.states.get(sensors["Tailgate Lock"].entity_id).state == "on"

    @pytest.mark.parametrize("backdoor_type", ["trunk", "hatch", None])
    async def test_other_backdoors_keep_trunk_entities(self, hass, setup_vehicles, backdoor_type):
        vehicle = FakeVehicle(set())
        vehicle.backdoor_type = backdoor_type
        vehicle.features[VehicleFeatures.Trunk] = ToyotaLockableOpening(closed=True, locked=True)
        events = add_registry_entries(hass, {"TESTVIN.Trunk": STALE_TRUNK[0]})

        account = await setup_vehicles([vehicle])

        assert removed(events) == []
        assert {
            item.unique_id for item in registry_entries(hass, account.entry, "binary_sensor")
        } == {
            "TESTVIN.Trunk",
            "TESTVIN.Trunk Door Lock",
        }


class TestNumericSensors:
    @pytest.mark.parametrize(
        ("value", "unit", "expected"),
        [
            (240, "kPa", 34.809058),
            (240, "kpa", 34.809058),
            (2.4, "bar", 34.809058),
            (2.4, "BAR", 34.809058),
            (0, "kPa", 0),
        ],
    )
    async def test_pressure_sensor_converts_reported_units_to_psi(
        self, hass, setup_vehicles, value, unit, expected
    ):
        vehicle = FakeVehicle(set())
        vehicle.features[VehicleFeatures.SpareTirePressure] = ToyotaNumeric(value, unit)
        await setup_vehicles([vehicle])

        state = hass.states.get(entity_id(hass, "sensor", "TESTVIN.Spare Tire Pressure"))
        assert float(state.state) == pytest.approx(expected)
        assert state.attributes["unit_of_measurement"] == "psi"

    async def test_native_and_missing_pressure_values_do_not_need_conversion(
        self, hass, setup_vehicles, subtests
    ):
        vehicle = FakeVehicle(set())
        vehicle.features[VehicleFeatures.SpareTirePressure] = ToyotaNumeric(35, "psi")
        account = await setup_vehicles([vehicle])
        pressure = entity_id(hass, "sensor", "TESTVIN.Spare Tire Pressure")
        for value, unit in ((35, "psi"), (35, "PSI"), (35, ""), (35, None), (None, "kPa")):
            with subtests.test(value=value, unit=unit):
                vehicle.features[VehicleFeatures.SpareTirePressure] = ToyotaNumeric(value, unit)
                await update(hass, account)
                # A converted value would read 35.0.
                assert hass.states.get(pressure).state == ("unknown" if value is None else "35")
        vehicle.features[VehicleFeatures.SpareTirePressure] = ToyotaNumeric(35, "atm")
        await update(hass, account)
        assert hass.states.get(pressure).state == "unknown"


class TestDeviceTracker:
    async def test_unsubscribed_vehicle_exposes_both_location_entities(self, hass, setup_vehicles):
        vehicle = FakeVehicle(set())
        vehicle.subscribed = False
        location = ToyotaLocation(34.05, -118.25)
        vehicle.features = {
            VehicleFeatures.ParkingLocation: location,
            VehicleFeatures.RealTimeLocation: location,
        }

        account = await setup_vehicles([vehicle])

        assert [
            (item.original_name, item.disabled_by is None)
            for item in registry_entries(hass, account.entry, "device_tracker")
        ] == [("Last Parked Location", True), ("Current Location", False)]


class TestCoordinatorUpdate:
    async def test_poll_reconciles_only_current_push_capable_vehicles(
        self, hass, setup_vehicles, caplog
    ):
        vehicles = []
        for vin, generation, subscribed in (
            ("NEWVIN", ApiVehicleGeneration.MM21, True),
            ("OTHERNEWVIN", ApiVehicleGeneration.MM24, True),
            ("EXPIREDVIN", ApiVehicleGeneration.MM24, False),
            ("LEGACYVIN", ApiVehicleGeneration.CY17, True),
        ):
            vehicle = FakeVehicle(set(), vin=vin)
            vehicle.generation = generation
            vehicle.subscribed = subscribed
            vehicle.region = "CA"
            vehicles.append(vehicle)
        account = await setup_vehicles(vehicles)
        # Polls during setup already pushed the contexts once.
        account.websocket.update_vehicle_contexts.reset_mock()
        caplog.clear()

        await account.coordinator.async_refresh()
        await hass.async_block_till_done()

        assert account.coordinator.data == vehicles
        account.websocket.update_vehicle_contexts.assert_awaited_once_with(
            {
                "NEWVIN": {"region": "CA", "backdoor_type": "trunk"},
                "OTHERNEWVIN": {"region": "CA", "backdoor_type": "trunk"},
                "EXPIREDVIN": {"region": "CA", "backdoor_type": "trunk"},
            }
        )
        assert not [
            record
            for record in caplog.records
            if record.name.startswith("custom_components.toyota_na")
            and record.levelno >= logging.WARNING
        ]

    async def test_automatic_wakes_schedule_one_followup_poll(self, hass, setup_vehicles):
        vehicles = [FakeVehicle(set(), vin="FIRSTVIN"), FakeVehicle(set(), vin="SECONDVIN")]
        account = await setup_vehicles(vehicles)
        await settle(hass)
        # Neither vehicle has a recorded wake, so the next poll wakes both.
        hass.config_entries.async_update_entry(
            account.entry, options={CONF_WAKE_INTERVAL: 2 * 3600}
        )
        polls = account.get_vehicles.await_count

        await account.coordinator.async_refresh()
        await hass.async_block_till_done()
        # A second follow-up would have waited out the cooldown of the first.
        await settle(hass)

        assert account.coordinator.data == vehicles
        assert [vehicle.refresh_requests for vehicle in vehicles] == [1, 1]
        # This poll and one follow-up.
        assert account.get_vehicles.await_count == polls + 2


class TestOptionsFlow:
    async def open(self, hass, *, options=None):
        entry = account_entry(options=options or {})
        entry.add_to_hass(hass)
        return entry, await hass.config_entries.options.async_init(entry.entry_id)

    async def test_default_preserves_existing_two_hour_behavior(self, hass):
        _, result = await self.open(hass)

        assert result["data_schema"]({}) == {CONF_WAKE_INTERVAL: str(2 * 3600)}

    async def test_manual_only_is_saved(self, hass):
        entry, result = await self.open(hass)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_WAKE_INTERVAL: "0"}
        )

        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == ""
        assert result["data"] == {CONF_WAKE_INTERVAL: 0}
        assert entry.options == {CONF_WAKE_INTERVAL: 0}

    @pytest.mark.parametrize("interval", [0, 12 * 3600])
    async def test_saved_interval_is_selected_when_reopened(self, hass, interval):
        _, result = await self.open(hass, options={CONF_WAKE_INTERVAL: interval})

        assert result["data_schema"]({}) == {CONF_WAKE_INTERVAL: str(interval)}
