# ruff: noqa: I001

import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ROOT / "custom_components"
INTEGRATION = COMPONENTS / "toyota_na"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(COMPONENTS)]
sys.modules.setdefault("custom_components", custom_components)

integration = types.ModuleType("custom_components.toyota_na")
integration.__path__ = [str(INTEGRATION)]
sys.modules.setdefault("custom_components.toyota_na", integration)

import toyota_na.vehicle.base_vehicle as upstream_base
from custom_components.toyota_na.patch_base_vehicle import (
    ApiVehicleGeneration,
    RemoteRequestCommand,
    ToyotaVehicle,
    VehicleFeatures,
)

upstream_base.ApiVehicleGeneration = ApiVehicleGeneration
upstream_base.RemoteRequestCommand = RemoteRequestCommand
upstream_base.ToyotaVehicle = ToyotaVehicle
upstream_base.VehicleFeatures = VehicleFeatures

from custom_components.toyota_na.patch_seventeen_cy_plus import (
    SeventeenCYPlusToyotaVehicle,
)
from custom_components.toyota_na.patch_seventeen_cy import SeventeenCYToyotaVehicle
from custom_components.toyota_na.patch_client import (
    get_telemetry,
    graphql_confirm_subscription,
    remote_request_17cy,
)
from custom_components.toyota_na.vehicle_helpers import (
    has_remote_subscription,
    is_electric_vehicle,
)
from custom_components.toyota_na.wake_policy import (
    CONF_WAKE_INTERVAL,
    automatic_wake_due,
    automatic_wake_interval,
)
from custom_components.toyota_na.websocket_handler import ToyotaWebSocketHandler

import toyota_na.vehicle.vehicle_generations.seventeen_cy as upstream_17cy
import toyota_na.vehicle.vehicle_generations.seventeen_cy_plus as upstream_17cyplus

upstream_17cy.SeventeenCYToyotaVehicle = SeventeenCYToyotaVehicle
upstream_17cyplus.SeventeenCYPlusToyotaVehicle = SeventeenCYPlusToyotaVehicle

from custom_components.toyota_na.patch_vehicle import get_vehicles


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
    },
    "extendedCapabilities": {
        "rearDriverDoorOpenStatus": True,
        "rearDriverDoorLockStatus": True,
        "rearPassengerDoorOpenStatus": True,
        "rearPassengerDoorLockStatus": True,
        "remoteEngineStartStop": True,
        "doorLockUnlockCapable": True,
    },
    "backdoorType": "trunk",
    "fuelType": "G",
    "evVehicle": False,
}


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


class VehicleMetadataTests(unittest.TestCase):
    def test_preserves_api_metadata_and_endpoint_family(self):
        vehicle = make_vehicle()

        self.assertEqual(vehicle.generation, ApiVehicleGeneration.MM21)
        self.assertEqual(vehicle.api_generation, "21MM")
        self.assertEqual(vehicle.endpoint_generation, "17CYPLUS")
        self.assertEqual(vehicle.brand, "L")
        self.assertEqual(vehicle.backdoor_type, "trunk")
        self.assertTrue(vehicle.subscribed)
        self.assertFalse(vehicle.electric)
        self.assertIs(vehicle.capabilities, vehicle.remote_capabilities)

    def test_commands_use_reported_capabilities(self):
        vehicle = make_vehicle()

        self.assertTrue(vehicle.supports_command(RemoteRequestCommand.DoorLock))
        self.assertTrue(vehicle.supports_command(RemoteRequestCommand.EngineStart))
        self.assertTrue(vehicle.supports_command(RemoteRequestCommand.HazardsOn))

        vehicle._remote_capabilities = {"hazardCapable": False}
        vehicle._extended_capabilities = {}
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.HazardsOn))
        self.assertTrue(vehicle.supports_command(RemoteRequestCommand.Refresh))

        vehicle._remote_capabilities = {"hazardCapable": None}
        self.assertTrue(vehicle.supports_command(RemoteRequestCommand.HazardsOn))

    def test_explicit_inactive_subscription_takes_precedence(self):
        metadata = {
            "remoteSubscriptionStatus": "INACTIVE",
            "subscriptionStatus": "SUBSCRIBED",
            "remoteSubscriptionExists": True,
        }

        self.assertFalse(has_remote_subscription(metadata))


class WakePolicyTests(unittest.TestCase):
    def test_manual_only_never_automatically_wakes(self):
        self.assertFalse(
            automatic_wake_due(
                {},
                {CONF_WAKE_INTERVAL: 0},
                2 * 3600,
                now=100_000,
            )
        )

    def test_missing_or_expired_timestamp_is_due(self):
        self.assertTrue(automatic_wake_due({}, {}, 2 * 3600, now=100_000))
        self.assertTrue(
            automatic_wake_due(
                {"last_refreshed_at": 92_800}, {}, 2 * 3600, now=100_000
            )
        )

    def test_recent_timestamp_is_not_due(self):
        self.assertFalse(
            automatic_wake_due(
                {"last_refreshed_at": 99_000}, {}, 2 * 3600, now=100_000
            )
        )

    def test_invalid_interval_uses_source_default(self):
        self.assertEqual(
            automatic_wake_interval({CONF_WAKE_INTERVAL: "never"}, 2 * 3600),
            2 * 3600,
        )


class VehicleStateTests(unittest.TestCase):
    def test_placeholder_rear_doors_do_not_create_entities(self):
        vehicle = make_vehicle()
        vehicle.apply_graphql_status(
            {
                "lastUpdateDateTime": "2026-08-13T12:00:00Z",
                "vehicleState": {
                    "doors": {
                        "rearDriverSide": {
                            "position": {"status": None},
                            "lock": {"status": None},
                        },
                        "rearPassengerSide": {
                            "position": {},
                            "lock": {},
                        },
                    },
                    "hatch": {
                        "position": {"status": None},
                        "lock": {"status": None},
                    },
                    "trunk": {"position": {"status": "close"}},
                    "moonroof": {"position": {"status": "unsupported"}},
                },
            }
        )

        self.assertNotIn(VehicleFeatures.RearDriverDoor, vehicle.features)
        self.assertNotIn(VehicleFeatures.RearPassengerDoor, vehicle.features)
        self.assertNotIn(VehicleFeatures.Moonroof, vehicle.features)
        self.assertTrue(vehicle.features[VehicleFeatures.Trunk].closed)

    def test_lock_only_rest_state_remains_position_unknown(self):
        vehicle = make_vehicle()
        vehicle._parse_vehicle_status(
            {
                "occurrenceDate": "2026-08-13T12:00:00Z",
                "vehicleStatus": [
                    {
                        "category": "Driver Side",
                        "sections": [
                            {"section": "Door", "values": [{"value": "locked"}]}
                        ],
                    }
                ],
            }
        )

        door = vehicle.features[VehicleFeatures.FrontDriverDoor]
        self.assertIsNone(door.closed)
        self.assertTrue(door.locked)

    def test_older_telemetry_cannot_overwrite_newer_window_state(self):
        vehicle = make_vehicle()
        vehicle.apply_graphql_status(
            {
                "lastUpdateDateTime": "2026-08-13T12:00:00Z",
                "vehicleState": {
                    "windows": {"driverSide": {"position": {"status": "open"}}}
                },
            }
        )
        vehicle._parse_telemetry(
            {"lastTimestamp": "2026-08-13T11:59:00Z", "driverWindow": 2}
        )

        window = vehicle.features[VehicleFeatures.FrontDriverWindow]
        self.assertFalse(window.closed)

    def test_newer_telemetry_can_update_window_state(self):
        vehicle = make_vehicle()
        vehicle.apply_graphql_status(
            {
                "lastUpdateDateTime": "2026-08-13T12:00:00Z",
                "vehicleState": {
                    "windows": {"driverSide": {"position": {"status": "open"}}}
                },
            }
        )
        vehicle._parse_telemetry(
            {"lastTimestamp": "2026-08-13T12:01:00Z", "driverWindow": 2}
        )

        window = vehicle.features[VehicleFeatures.FrontDriverWindow]
        self.assertTrue(window.closed)

    def test_location_only_push_is_applied(self):
        vehicle = make_vehicle()

        applied = vehicle.apply_graphql_status(
            {"location": {"latitude": 0.0, "longitude": 0.0}}
        )

        self.assertTrue(applied)
        location = vehicle.features[VehicleFeatures.ParkingLocation]
        self.assertEqual(location.lat, 0.0)
        self.assertEqual(location.value, 0.0)


class WebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_push_is_forwarded_immediately(self):
        received = []
        handler = ToyotaWebSocketHandler(
            object(), lambda vin, status: received.append((vin, status))
        )
        status = {"vin": "TESTVIN", "vehicleState": {"doors": {}}}

        await handler._handle_message(
            {"type": "data", "payload": {"data": {"onVehicleStatusUpdated": status}}},
            None,
            None,
        )

        self.assertEqual(received, [("TESTVIN", status)])
        self.assertEqual(handler.get_cached_status("TESTVIN"), status)

    async def test_subscription_confirmation_uses_vehicle_context(self):
        calls = []

        class Client:
            async def graphql_confirm_subscription(self, *args):
                calls.append(args)
                return {"vin": args[0]}

        handler = ToyotaWebSocketHandler(Client())
        handler._subscriptions = {"TESTVIN": "subscription"}
        handler._vehicle_contexts = {
            "TESTVIN": {"brand": "L", "backdoor_type": "trunk"}
        }

        await handler._handle_message(
            {"type": "start_ack", "id": "subscription"}, None, None
        )

        self.assertEqual(calls, [("TESTVIN", "trunk", "L")])

    async def test_subscription_uses_vehicle_brand_and_region(self):
        sent = []

        class WebSocket:
            async def send_json(self, payload):
                sent.append(payload)

        handler = ToyotaWebSocketHandler(object())
        handler._ws = WebSocket()
        handler._vehicle_contexts = {"TESTVIN": {"brand": "L", "region": "US"}}

        await handler._subscribe_vin("TESTVIN", "token", "guid")

        authorization = sent[0]["payload"]["extensions"]["authorization"]
        self.assertEqual(authorization["X-BRAND"], "L")
        self.assertEqual(authorization["X-APPBRAND"], "L")
        self.assertEqual(authorization["x-region"], "US")


class ClientMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_discovery_keeps_toyota_defaults(self):
        calls = []
        payload = {
            "modelYear": "2018",
            "modelName": "CAMRY",
            "generation": "17CY",
            "region": "US",
            "remoteSubscriptionStatus": "ACTIVE",
            "evVehicle": False,
            "vin": "TESTVIN",
        }

        class Client:
            async def get_user_vehicle_list(self):
                return [payload]

            async def get_vehicle_status_17cy(self, *args):
                calls.append(("status", args))

            async def get_telemetry(self, *args):
                calls.append(("telemetry", args))

            async def get_engine_status_17cy(self, *args):
                calls.append(("engine", args))

        vehicles = await get_vehicles(Client())

        self.assertEqual(vehicles[0].generation, ApiVehicleGeneration.CY17)
        self.assertEqual(vehicles[0].brand, "T")
        self.assertIn(("status", ("TESTVIN", "T", "US")), calls)
        self.assertIn(("telemetry", ("TESTVIN", "US", "17CY", "T")), calls)

    async def test_rest_wins_without_timestamps_but_push_fills_missing_state(self):
        class WebSocketHandler:
            def get_cached_status(self, vin):
                return {
                    "vin": vin,
                    "vehicleState": {
                        "windows": {
                            "driverSide": {"position": {"status": "close"}},
                            "passengerSide": {"position": {"status": "open"}},
                        }
                    },
                }

        class Client:
            _ws_handler = WebSocketHandler()

            async def get_telemetry(self, *args):
                return {}

            async def get_vehicle_status_17cyplus(self, *args):
                return {
                    "vehicleStatus": [
                        {
                            "category": "Driver Side",
                            "sections": [
                                {"section": "Window", "values": [{"value": "open"}]}
                            ],
                        }
                    ]
                }

            async def get_engine_status_17cyplus(self, *args):
                return None

        vehicle = make_vehicle(Client())

        await vehicle.update()

        self.assertFalse(vehicle.features[VehicleFeatures.FrontDriverWindow].closed)
        self.assertFalse(vehicle.features[VehicleFeatures.FrontPassengerWindow].closed)

    async def test_discovery_preserves_metadata_for_shared_vehicle_class(self):
        calls = []
        payload = dict(LEXUS_21MM_COUPE, vin="TESTVIN")

        class Client:
            async def get_user_vehicle_list(self):
                return [payload]

            async def get_telemetry(self, *args):
                calls.append(("telemetry", args))

            async def get_vehicle_status_17cyplus(self, *args):
                calls.append(("status", args))

            async def get_engine_status_17cyplus(self, *args):
                calls.append(("engine", args))

        vehicles = await get_vehicles(Client())

        self.assertEqual(len(vehicles), 1)
        self.assertEqual(vehicles[0].generation, ApiVehicleGeneration.MM21)
        self.assertEqual(vehicles[0].brand, "L")
        self.assertEqual(vehicles[0].backdoor_type, "trunk")
        self.assertIn(("telemetry", ("TESTVIN", "US", "17CYPLUS", "L")), calls)
        self.assertIn(("status", ("TESTVIN", "L", "US")), calls)

    async def test_telemetry_uses_reported_vehicle_headers(self):
        calls = []

        class Client:
            async def api_get(self, *args):
                calls.append(args)
                return {}

        await get_telemetry(Client(), "TESTVIN", "US", "17CYPLUS", "L")

        self.assertEqual(calls[0][1]["X-BRAND"], "L")
        self.assertEqual(calls[0][1]["X-APPBRAND"], "L")
        self.assertEqual(calls[0][1]["x-region"], "US")

    async def test_legacy_command_uses_reported_vehicle_headers(self):
        calls = []

        class Auth:
            async def get_guid(self):
                return "guid"

            def get_device_id(self):
                return "device"

        class Client:
            auth = Auth()

            async def api_post(self, *args):
                calls.append(args)
                return {}

        await remote_request_17cy(Client(), "TESTVIN", "DL", 1, "L", "CA")

        self.assertEqual(calls[0][2]["X-BRAND"], "L")
        self.assertEqual(calls[0][2]["X-APPBRAND"], "L")
        self.assertEqual(calls[0][2]["x-region"], "CA")

    async def test_graphql_confirmation_uses_trunk_and_lexus_brand(self):
        calls = []

        class Client:
            async def graphql_request(self, *args):
                calls.append(args)
                return {}

        await graphql_confirm_subscription(Client(), "TESTVIN", "trunk", "L")

        _, _, variables, brand = calls[0]
        self.assertEqual(variables, {"vin": "TESTVIN", "backdoorType": "trunk"})
        self.assertEqual(brand, "L")


if __name__ == "__main__":
    unittest.main()
