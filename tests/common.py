"""Test doubles and helpers for running the integration in a real Home Assistant."""

import json
import logging
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import DATA_INSTANCES
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.toyota_na.const import DOMAIN
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    ToyotaVehicle,
)
from custom_components.toyota_na.patch_seventeen_cy import SeventeenCYToyotaVehicle
from custom_components.toyota_na.patch_seventeen_cy_plus import SeventeenCYPlusToyotaVehicle
from custom_components.toyota_na.vehicle_helpers import (
    has_remote_subscription,
    is_electric_vehicle,
)

EMAIL = "owner@example.com"
STATUS_24MM = json.loads((Path(__file__).parent / "fixtures/vehicle_24mm.json").read_text())

LEXUS_21MM_COUPE = {
    "modelYear": "2024",
    "modelName": "LC 500 2-DOOR COUPE",
    "generation": "21MM",
    "brand": "L",
    "region": "US",
    "remoteSubscriptionStatus": "ACTIVE",
    "subscriptionStatus": "SUBSCRIBED",
    "remoteSubscriptionExists": True,
    "remoteServiceCapabilities": {
        "estartStopCapable": True,
        "dlockUnlockCapable": True,
        "powerWindowCapable": False,
        "trunkCommandCapable": False,
        "hornCommandCapable": False,
        "estartEnabled": True,
        "estopEnabled": True,
        "hazardCapable": True,
        "vehicleFinderCapable": True,
    },
    "extendedCapabilities": {
        "rearDriverDoorOpenStatus": True,
        "rearDriverDoorLockStatus": True,
        "rearPassengerDoorOpenStatus": True,
        "rearPassengerDoorLockStatus": True,
        "remoteEngineStartStop": True,
        "doorLockUnlockCapable": True,
        "vehicleFinder": True,
        "lastParkedCapable": True,
    },
    "backdoorType": "trunk",
    "fuelType": "G",
    "evVehicle": False,
}

TWENTY_FOUR_MM_PHEV = {
    "modelYear": "2026",
    "modelName": "RAV4 PLUG-IN HYBRID",
    "generation": "24MM",
    "brand": "T",
    "region": "CA",
    "remoteSubscriptionStatus": None,
    "subscriptionStatus": "subscribed",
    "remoteSubscriptionExists": True,
    "remoteServiceCapabilities": {
        "estartStopCapable": True,
        "dlockUnlockCapable": True,
    },
    "extendedCapabilities": {
        "remoteEngineStartStop": True,
        "doorLockUnlockCapable": True,
    },
    "backdoorType": "hatch",
    "fuelType": "I",
    "evVehicle": False,
}


class FakeVehicle:
    """A vehicle whose features and commands each test sets directly."""

    climate_schedules = {}
    health = {}
    remote_display = None
    notifications = None
    supports_climate_settings = False
    supports_climate_schedules = False
    supports_charge_schedules = False

    _feature_flags = None
    uses_appsync = True
    feature_enabled = ToyotaVehicle.feature_enabled
    # Setup starts a status push only for the generations that support it.
    generation = None

    def __init__(self, supported, vin="TESTVIN"):
        self.vin = vin
        self.subscribed = True
        self.supported = set(supported)
        self.sent = []
        self.refresh_requests = 0
        self.features = {}
        self.electric = False
        self.region = "US"
        self.backdoor_type = "trunk"
        self.model_year = "2024"
        self.model_name = "LC 500 2-DOOR COUPE"
        self.brand = "L"
        self.climate_settings = {}
        self.charge_settings = {}

    def supports_command(self, command):
        if command == RemoteRequestCommand.Refresh:
            return self.subscribed
        return command in self.supported

    @property
    def can_receive_status(self):
        return True

    async def send_command(self, command):
        self.sent.append(command)

    async def poll_vehicle_refresh(self):
        self.refresh_requests += 1

    def supports_charge_setting(self, field):
        return False


def make_vehicle(client=None):
    return SeventeenCYPlusToyotaVehicle(
        client=client or object(),
        has_remote_subscription=has_remote_subscription(LEXUS_21MM_COUPE),
        has_electric=is_electric_vehicle(LEXUS_21MM_COUPE),
        model_name=LEXUS_21MM_COUPE["modelName"],
        model_year=LEXUS_21MM_COUPE["modelYear"],
        vin="TESTVIN",
        region=LEXUS_21MM_COUPE["region"],
        generation=ApiVehicleGeneration(LEXUS_21MM_COUPE["generation"]),
        brand=LEXUS_21MM_COUPE["brand"],
        backdoor_type=LEXUS_21MM_COUPE["backdoorType"],
        remote_capabilities=LEXUS_21MM_COUPE["remoteServiceCapabilities"],
        extended_capabilities=LEXUS_21MM_COUPE["extendedCapabilities"],
    )


def make_17cy_vehicle(client=None):
    return SeventeenCYToyotaVehicle(
        client or object(),
        True,
        True,
        "PRIUS PRIME",
        "2018",
        "TESTVIN",
        "CA",
    )


def make_24mm_vehicle(client=None):
    return SeventeenCYPlusToyotaVehicle(
        client=client or object(),
        has_remote_subscription=has_remote_subscription(TWENTY_FOUR_MM_PHEV),
        has_electric=is_electric_vehicle(TWENTY_FOUR_MM_PHEV),
        model_name=TWENTY_FOUR_MM_PHEV["modelName"],
        model_year=TWENTY_FOUR_MM_PHEV["modelYear"],
        vin="TESTVIN24",
        region=TWENTY_FOUR_MM_PHEV["region"],
        generation=ApiVehicleGeneration(TWENTY_FOUR_MM_PHEV["generation"]),
        brand=TWENTY_FOUR_MM_PHEV["brand"],
        backdoor_type=TWENTY_FOUR_MM_PHEV["backdoorType"],
        remote_capabilities=TWENTY_FOUR_MM_PHEV["remoteServiceCapabilities"],
        extended_capabilities=TWENTY_FOUR_MM_PHEV["extendedCapabilities"],
    )


class FakeClient:
    """Toyota's cloud with one 24MM vehicle. Every other read returns nothing."""

    def __init__(self, auth):
        self.auth = auth
        self.calls = {}
        self.get_user_vehicle_list = AsyncMock(
            return_value=[{**TWENTY_FOUR_MM_PHEV, "vin": "TESTVIN24"}]
        )

    async def graphql_get_vehicle_status(self, vin, backdoor_type, region):
        return STATUS_24MM

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return self.calls.setdefault(name, AsyncMock(return_value=None))


class FakeWebSocket:
    def __init__(self, client, on_status):
        self.on_status = on_status
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.update_vehicle_contexts = AsyncMock()

    def get_cached_status(self, vin):
        return None


def account_entry(*, data=None, **kwargs):
    now = time.time()
    tokens = {
        "access_token": "test-access",
        "refresh_token": "test-refresh",
        "id_token": "test-id",
        "expires_at": now + 3600,
        "updated_at": now,
        "guid": "test-guid",
        "clock": "unix",
    }
    return MockConfigEntry(
        domain=DOMAIN,
        title=EMAIL,
        unique_id=f"{DOMAIN}:{EMAIL}",
        data={
            "tokens": tokens,
            "device_id": "test-device",
            "email": EMAIL,
            "username": EMAIL,
            **(data or {}),
        },
        **kwargs,
    )


async def set_up(hass: HomeAssistant, entry: MockConfigEntry, caplog) -> None:
    """Set up the entry, failing on errors Home Assistant only logs, like a platform's."""
    start = len(caplog.records)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    errors = [r.getMessage() for r in caplog.records[start:] if r.levelno >= logging.ERROR]
    assert not errors


async def settle(hass: HomeAssistant) -> None:
    """Run pending work, then any refresh the coordinator's 10 s cooldown deferred."""
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=11))
    await hass.async_block_till_done()


def entity_id(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


def get_entity(hass: HomeAssistant, entity_id: str) -> Entity:
    """Return the live entity object, for internals Home Assistant doesn't expose."""
    return hass.data[DATA_INSTANCES][entity_id.split(".")[0]].get_entity(entity_id)
