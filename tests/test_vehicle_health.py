"""Vehicle health reads follow Toyota's app gates and cadence."""

import types
import unittest
from unittest.mock import AsyncMock, patch

import test_vehicle_behavior as behavior

from custom_components.toyota_na import patch_base_vehicle, patch_client

REPORT = {"vehicleStatus": {"engOilLevelStatus": "Normal"}}
STATUS = {"warning": []}
CAMPAIGNS = [{"campaignType": "REC-S", "campaignNumber": "24V123"}]


def health_client(**overrides):
    return types.SimpleNamespace(**{
        "get_vehicle_health_report": AsyncMock(return_value=REPORT),
        "get_vehicle_health_status": AsyncMock(return_value=STATUS),
        "get_service_campaigns": AsyncMock(return_value=CAMPAIGNS),
        **overrides,
    })


class VehicleHealthReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_use_the_vehicle_headers_like_the_app(self):
        client = health_client()
        vehicle = behavior.make_vehicle(client)
        await vehicle.update_health()
        client.get_vehicle_health_report.assert_awaited_once_with("TESTVIN", "21MM", "US", "L")
        client.get_vehicle_health_status.assert_awaited_once_with("TESTVIN", "21MM", "US", "L")
        client.get_service_campaigns.assert_awaited_once_with("TESTVIN")
        self.assertEqual(
            {key: value for key, value in vehicle.health.items() if key != "read_at"},
            {"report": REPORT, "status": STATUS, "campaigns": CAMPAIGNS},
        )

    async def test_each_read_follows_its_feature_flags(self):
        for flags, expected in (
            ({}, set()),
            ({"vehicleHealthReport": 1}, {"report"}),
            ({"scheduleMaintenance": 1}, {"report"}),
            ({"safetyRecall": 1}, {"report", "campaigns"}),
            ({"serviceCampaign": 1}, {"report", "campaigns"}),
            ({"vehicleDiagnostic": 1}, {"status"}),
            ({"vehicleHealthReport": 2, "vehicleDiagnostic": 2}, set()),
        ):
            with self.subTest(flags=flags):
                vehicle = behavior.make_vehicle(health_client())
                vehicle._feature_flags = flags
                await vehicle.update_health()
                self.assertEqual(set(vehicle.health) - {"read_at"}, expected)

    async def test_reads_are_hourly_across_polls(self):
        client = health_client()
        previous = behavior.make_vehicle(client)
        with patch.object(patch_base_vehicle.time, "monotonic", return_value=1000.0):
            await previous.update_health()
        vehicle = behavior.make_vehicle(client)
        self.assertTrue(vehicle.inherit_state(previous))
        for now, reads in ((4599.0, 1), (4600.0, 2)):
            with patch.object(patch_base_vehicle.time, "monotonic", return_value=now):
                await vehicle.update_health()
            self.assertEqual(client.get_vehicle_health_report.await_count, reads)
        self.assertIs(vehicle.health, previous.health)

    async def test_failed_or_unusable_reads_keep_the_last_response(self):
        client = health_client()
        vehicle = behavior.make_vehicle(client)
        await vehicle.update_health()
        client.get_vehicle_health_report.side_effect = RuntimeError("[APIGW-403]")
        client.get_vehicle_health_status.return_value = None
        client.get_service_campaigns.return_value = {"status": "error"}
        vehicle._health["read_at"] -= patch_base_vehicle.HEALTH_READ_INTERVAL
        with self.assertLogs(patch_base_vehicle.__name__, level="DEBUG"):
            await vehicle.update_health()
        self.assertEqual(
            (vehicle.health["report"], vehicle.health["status"], vehicle.health["campaigns"]),
            (REPORT, STATUS, CAMPAIGNS),
        )

    async def test_both_vehicle_classes_read_health_each_poll(self):
        for vehicle in (behavior.make_vehicle(types.SimpleNamespace()), behavior.make_17cy_vehicle(types.SimpleNamespace())):
            with self.subTest(vehicle=type(vehicle).__name__), patch.object(
                type(vehicle), "update_health", AsyncMock(),
            ) as update_health:
                await vehicle.update()
            update_health.assert_awaited_once()

    async def test_service_campaigns_accept_either_envelope_spelling(self):
        for body, expected in (
            (CAMPAIGNS, CAMPAIGNS),
            ({"payLoad": CAMPAIGNS}, CAMPAIGNS),
            ({"status": "error"}, {"status": "error"}),
        ):
            with self.subTest(body=body):
                client = types.SimpleNamespace(api_get=AsyncMock(return_value=body))
                self.assertEqual(await patch_client.get_service_campaigns(client, "TESTVIN"), expected)
                client.api_get.assert_awaited_once_with("v2/service-campaign", {"vin": "TESTVIN"})

    async def test_report_and_status_send_brand_generation_and_region(self):
        for function, endpoint in (
            (patch_client.get_vehicle_health_report, "v1/vehiclehealth/report"),
            (patch_client.get_vehicle_health_status, "v1/vehiclehealth/status"),
        ):
            with self.subTest(endpoint=endpoint):
                client = types.SimpleNamespace(api_get=AsyncMock(return_value=REPORT))
                await function(client, "TESTVIN", "21MM", "CA", "L")
                client.api_get.assert_awaited_once_with(endpoint, {
                    "VIN": "TESTVIN", "X-BRAND": "L", "x-region": "CA", "GENERATION": "21MM",
                })
