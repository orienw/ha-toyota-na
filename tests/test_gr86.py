"""Synthetic GR86 coverage for routed status and capability-gated commands."""

import types
import unittest
from unittest.mock import AsyncMock

import test_vehicle_behavior as behavior

from custom_components.toyota_na.patch_base_vehicle import ApiVehicleGeneration, RemoteRequestCommand, VehicleFeatures


class GR86Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.metadata = {
            "vin": "TESTGR86", "generation": "GR86", "brand": "T", "region": "CA",
            "modelName": "GR86", "modelYear": "2025", "fuelType": "G",
            "remoteSubscriptionStatus": "ACTIVE",
            "remoteServiceCapabilities": {"dlockUnlockCapable": True, "estartStopCapable": False},
            "features": {"remoteCommands": 1, "vehicleState": 1},
        }
        self.client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[self.metadata]),
            get_telemetry=AsyncMock(return_value={"fuelLevel": 60}),
            get_vehicle_status_route=AsyncMock(return_value={"vehicleStatus": [{
                "category": "Driver Side", "sections": [{
                    "section": "Door", "values": [{"value": "closed"}, {"value": "locked"}],
                }],
            }]}),
            get_engine_status_route=AsyncMock(return_value={"status": "stopped"}),
            get_tire_pressure=AsyncMock(return_value={"flTirePressure": {
                "value": 32, "unit": "psi", "displayLowTirePressureWarning": False,
            }}),
            remote_request_route=AsyncMock(), send_refresh_request_route=AsyncMock(),
            graphql_get_vehicle_status=AsyncMock(), graphql_pre_wake=AsyncMock(),
        )

    async def test_discovery_reads_and_commands_keep_gr86_generation(self):
        vehicle, = await behavior.get_vehicles(self.client)
        self.assertIs(ApiVehicleGeneration.GR86, vehicle.generation)
        self.assertFalse(vehicle.uses_appsync)
        self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].locked)
        self.assertFalse(vehicle.features[VehicleFeatures.RemoteStartStatus].on)
        self.assertEqual(60, vehicle.features[VehicleFeatures.FuelLevel].value)
        self.assertEqual(32, vehicle.features[VehicleFeatures.FrontDriverTire].value)
        self.client.get_telemetry.assert_awaited_once_with("TESTGR86", "CA", "GR86")
        self.client.get_vehicle_status_route.assert_awaited_once_with("TESTGR86", "GR86", "CA", "T")
        self.client.get_engine_status_route.assert_awaited_once_with("TESTGR86", "GR86", "CA", "T")
        self.client.get_tire_pressure.assert_awaited_once_with("TESTGR86", "GR86", "CA", "T")
        await vehicle.send_command(RemoteRequestCommand.DoorLock)
        self.client.remote_request_route.assert_awaited_once_with("TESTGR86", "GR86", "door-lock", "CA", "T")
        await vehicle.poll_vehicle_refresh()
        self.client.send_refresh_request_route.assert_awaited_once_with("TESTGR86", "GR86", "CA", "T")
        self.client.graphql_get_vehicle_status.assert_not_awaited()
        self.client.graphql_pre_wake.assert_not_awaited()

    async def test_missing_capabilities_and_inactive_access_block_commands(self):
        vehicle, = await behavior.get_vehicles(self.client)
        for command in (RemoteRequestCommand.EngineStart, RemoteRequestCommand.HazardsOn):
            with self.subTest(command=command), self.assertRaisesRegex(ValueError, "unavailable"):
                await vehicle.send_command(command)
        vehicle._remote_capabilities = {}
        with self.assertRaisesRegex(ValueError, "unavailable"):
            await vehicle.send_command(RemoteRequestCommand.DoorLock)
        self.client.remote_request_route.assert_not_awaited()

        vehicle._remote_capabilities = {"dlockUnlockCapable": True}
        vehicle._feature_flags["remoteCommands"] = 2
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.DoorLock))
        vehicle._has_remote_subscription = False
        await vehicle.update()
        self.assertTrue(vehicle.features[VehicleFeatures.FrontDriverDoor].locked)
        self.assertEqual(60, vehicle.features[VehicleFeatures.FuelLevel].value)
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.Refresh))
        self.assertFalse(vehicle.supports_command(RemoteRequestCommand.DoorLock))
        self.assertEqual(2, self.client.get_vehicle_status_route.await_count)
