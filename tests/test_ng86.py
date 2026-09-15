"""NG86 discovery and commands use the vehicle's routed API context."""

import types
import unittest
from unittest.mock import AsyncMock

import test_vehicle_behavior as behavior

from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures
from custom_components.toyota_na.patch_vehicle import get_vehicles


class NG86Tests(unittest.IsolatedAsyncioTestCase):
    async def test_ng86_uses_routed_state_engine_commands_and_refresh(self):
        metadata = {
            **behavior.LEXUS_21MM_COUPE, "vin": "TESTNG86", "generation": "NG86",
            "brand": "T", "region": "CA", "modelName": "TEST VEHICLE",
        }
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[metadata]),
            get_telemetry=AsyncMock(return_value={}),
            get_vehicle_status_route=AsyncMock(return_value={"vehicleStatus": [{
                "category": "Driver Side", "sections": [{
                    "section": "Door", "values": [{"value": "closed"}, {"value": "locked"}],
                }],
            }]}),
            get_engine_status_route=AsyncMock(return_value={"status": "started"}),
            send_refresh_request_route=AsyncMock(), remote_request_route=AsyncMock(),
            graphql_pre_wake=AsyncMock(),
        )
        vehicle, = await get_vehicles(client)
        self.assertEqual(ApiVehicleGeneration.NG86, vehicle.generation)
        self.assertFalse(vehicle.uses_appsync)
        self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].locked)
        self.assertTrue(vehicle.features[VehicleFeatures.RemoteStartStatus].on)
        client.get_vehicle_status_route.assert_awaited_once_with("TESTNG86", "NG86", "CA", "T")
        client.get_engine_status_route.assert_awaited_once_with("TESTNG86", "NG86", "CA", "T")
        await vehicle.send_command(RemoteRequestCommand.DoorLock)
        client.remote_request_route.assert_awaited_once_with("TESTNG86", "NG86", "door-lock", "CA", "T")
        await vehicle.poll_vehicle_refresh()
        client.send_refresh_request_route.assert_awaited_once_with("TESTNG86", "NG86", "CA", "T")
        client.graphql_pre_wake.assert_not_awaited()

    async def test_ng86_requires_reported_command_support_and_gr86_remains_distinct(self):
        vehicle = behavior.make_vehicle()
        vehicle._generation = ApiVehicleGeneration.NG86
        vehicle._remote_capabilities = {}
        vehicle._extended_capabilities = {}
        for command in (RemoteRequestCommand.DoorLock, RemoteRequestCommand.EngineStart, RemoteRequestCommand.VehicleFinder):
            self.assertFalse(vehicle.supports_command(command))
        client = types.SimpleNamespace(get_user_vehicle_list=AsyncMock(return_value=[
            {**behavior.LEXUS_21MM_COUPE, "vin": "TESTGR86", "generation": "GR86"},
            {**behavior.LEXUS_21MM_COUPE, "vin": "TESTPRE17", "generation": "PRE17CY"},
        ]))
        self.assertEqual([], await get_vehicles(client))
