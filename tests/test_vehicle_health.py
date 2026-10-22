"""Vehicle health reads follow Toyota's app gates and cadence."""

import types
import unittest
from unittest.mock import AsyncMock, patch

import pytest
from common import FakeVehicle, make_17cy_vehicle, make_vehicle
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.helpers import entity_registry as er

from custom_components.toyota_na import health_helpers, patch_base_vehicle, patch_client

REPORT = {"vehicleStatus": {"engOilLevelStatus": "Normal"}}
STATUS = {"warning": []}
CAMPAIGNS = [{"campaignType": "REC-S", "campaignNumber": "24V123"}]
SOFTWARE = {
    "notificationStatus": 1,
    "updateAvailable": True,
    "updateName": "Multimedia",
    "versionNumber": "2.1",
}
# Attributes Home Assistant adds to every state, around the entity's own.
STANDARD_ATTRIBUTES = {"friendly_name", "icon", "device_class", "state_class"}


def health_client(**overrides):
    return types.SimpleNamespace(
        **{
            "get_vehicle_health_report": AsyncMock(return_value=REPORT),
            "get_vehicle_health_status": AsyncMock(return_value=STATUS),
            "get_service_campaigns": AsyncMock(return_value=CAMPAIGNS),
            "get_software_update": AsyncMock(return_value=SOFTWARE),
            **overrides,
        }
    )


class VehicleHealthReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_use_the_vehicle_headers_like_the_app(self):
        client = health_client()
        vehicle = make_vehicle(client)
        await vehicle.update_health()
        client.get_vehicle_health_report.assert_awaited_once_with("TESTVIN", "21MM", "US", "L")
        client.get_vehicle_health_status.assert_awaited_once_with("TESTVIN", "21MM", "US", "L")
        client.get_service_campaigns.assert_awaited_once_with("TESTVIN")
        client.get_software_update.assert_awaited_once_with("TESTVIN")
        self.assertEqual(
            {key: value for key, value in vehicle.health.items() if key != "read_at"},
            {"report": REPORT, "status": STATUS, "campaigns": CAMPAIGNS, "software": SOFTWARE},
        )

    async def test_each_read_follows_its_feature_flags(self):
        for flags, expected in (
            ({}, set()),
            ({"vehicleHealthReport": 1}, {"report"}),
            ({"scheduleMaintenance": 1}, {"report"}),
            ({"safetyRecall": 1}, {"report", "campaigns"}),
            ({"serviceCampaign": 1}, {"report", "campaigns"}),
            ({"vehicleDiagnostic": 1}, {"status"}),
            ({"autoDrive": 1}, {"software"}),
            ({"vehicleHealthReport": 2, "vehicleDiagnostic": 2}, set()),
        ):
            with self.subTest(flags=flags):
                vehicle = make_vehicle(health_client())
                vehicle._feature_flags = flags
                await vehicle.update_health()
                self.assertEqual(set(vehicle.health) - {"read_at"}, expected)

    async def test_reads_are_hourly_across_polls(self):
        client = health_client()
        previous = make_vehicle(client)
        with patch.object(patch_base_vehicle.time, "monotonic", return_value=1000.0):
            await previous.update_health()
        vehicle = make_vehicle(client)
        self.assertTrue(vehicle.inherit_state(previous))
        for now, reads in ((4599.0, 1), (4600.0, 2)):
            with patch.object(patch_base_vehicle.time, "monotonic", return_value=now):
                await vehicle.update_health()
            self.assertEqual(client.get_vehicle_health_report.await_count, reads)
        self.assertIs(vehicle.health, previous.health)

    async def test_failed_or_unusable_reads_keep_the_last_response(self):
        client = health_client()
        vehicle = make_vehicle(client)
        with patch.object(patch_base_vehicle.time, "monotonic", return_value=1000.0):
            await vehicle.update_health()
        client.get_vehicle_health_report.side_effect = RuntimeError("[APIGW-403]")
        client.get_vehicle_health_status.return_value = {}
        client.get_service_campaigns.return_value = {"status": "error"}
        with (
            patch.object(patch_base_vehicle.time, "monotonic", return_value=4600.0),
            self.assertLogs(patch_base_vehicle.__name__, level="DEBUG"),
        ):
            await vehicle.update_health()
        self.assertEqual(
            (vehicle.health["report"], vehicle.health["status"], vehicle.health["campaigns"]),
            (REPORT, STATUS, CAMPAIGNS),
        )

    async def test_reads_retry_each_poll_until_they_first_answer(self):
        client = health_client(
            get_vehicle_health_report=AsyncMock(side_effect=[TimeoutError(), {}, REPORT, REPORT]),
            get_service_campaigns=AsyncMock(return_value=[]),
        )
        vehicle = make_vehicle(client)
        for now in (1000.0, 1600.0, 2200.0, 2800.0, 4599.0):
            with patch.object(patch_base_vehicle.time, "monotonic", return_value=now):
                await vehicle.update_health()
        self.assertEqual(client.get_vehicle_health_report.await_count, 3)
        self.assertEqual(client.get_service_campaigns.await_count, 1)
        self.assertEqual((vehicle.health["report"], vehicle.health["campaigns"]), (REPORT, []))
        with patch.object(patch_base_vehicle.time, "monotonic", return_value=5800.0):
            await vehicle.update_health()
        self.assertEqual(client.get_vehicle_health_report.await_count, 4)

    async def test_both_vehicle_classes_read_health_each_poll(self):
        for vehicle in (
            make_vehicle(types.SimpleNamespace()),
            make_17cy_vehicle(types.SimpleNamespace()),
        ):
            with (
                self.subTest(vehicle=type(vehicle).__name__),
                patch.object(
                    type(vehicle),
                    "update_health",
                    AsyncMock(),
                ) as update_health,
            ):
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
                self.assertEqual(
                    await patch_client.get_service_campaigns(client, "TESTVIN"), expected
                )
                client.api_get.assert_awaited_once_with("v2/service-campaign", {"vin": "TESTVIN"})

    async def test_report_and_status_send_brand_generation_and_region(self):
        for function, endpoint in (
            (patch_client.get_vehicle_health_report, "v1/vehiclehealth/report"),
            (patch_client.get_vehicle_health_status, "v1/vehiclehealth/status"),
        ):
            with self.subTest(endpoint=endpoint):
                client = types.SimpleNamespace(
                    api_request=AsyncMock(return_value={"payload": REPORT})
                )
                self.assertEqual(await function(client, "TESTVIN", "21MM", "CA", "L"), REPORT)
                client.api_request.assert_awaited_once_with(
                    "GET",
                    endpoint,
                    {
                        "VIN": "TESTVIN",
                        "X-BRAND": "L",
                        "x-region": "CA",
                        "GENERATION": "21MM",
                    },
                    envelope=True,
                )

    async def test_software_update_check_uses_the_cdn_path_and_vin_header(self):
        for body, expected in (
            ({"payload": SOFTWARE, "status": {}}, SOFTWARE),
            ({"status": {"messages": [{"description": "error"}]}}, None),
            (None, None),
        ):
            with self.subTest(body=body):
                client = types.SimpleNamespace(api_request=AsyncMock(return_value=body))
                self.assertEqual(
                    await patch_client.get_software_update(client, "TESTVIN"), expected
                )
                client.api_request.assert_awaited_once_with(
                    "GET",
                    "https://onecdn.telematicsct.com/oa24mm/v1/ota/update/check",
                    {"vin": "TESTVIN", "X-APIVERSION": "v1"},
                    envelope=True,
                )

    async def test_report_and_status_reject_error_bodies(self):
        error = {
            "status": {"messages": [{"description": "VIN TESTVIN not found for account-guid"}]}
        }
        for body in (error, {**error, "payload": None}, [], None):
            for function in (
                patch_client.get_vehicle_health_report,
                patch_client.get_vehicle_health_status,
            ):
                with self.subTest(body=body, function=function.__name__):
                    client = types.SimpleNamespace(api_request=AsyncMock(return_value=body))
                    self.assertIsNone(await function(client, "TESTVIN", "21MM", "CA", "L"))


class HealthTabTests(unittest.TestCase):
    def test_recalls_merge_report_and_campaign_lists_like_the_app(self):
        report = {
            "recallsListExists": True,
            "safetyRecallsList": [
                {
                    "title": "Airbag",
                    "description": "Inflator",
                    "remedy": "Replace",
                    "nhtsarecallDate": "2024-01-02",
                    "dealerReferenceID": "24V1",
                },
                {"title": "Fuel pump", "dealerReferenceID": "24V2"},
            ],
        }
        campaigns = [
            {
                "campaignType": "REC-S",
                "campaignNumber": "24V1",
                "campaignTitle": "Airbag inflator",
                "description": "Updated",
                "remedyDescription": "Replace inflator",
                "campaignDate": "2024-03-04",
            },
            {"campaignType": "Safety Recalls", "campaignNumber": "24V9"},
            {
                "campaignType": "LSC",
                "campaignNumber": "24V2",
                "campaignTitle": "Warranty",
                "description": "Extended",
            },
        ]
        self.assertEqual(
            health_helpers.safety_recalls({"report": report, "campaigns": campaigns}),
            [
                {"title": "Fuel pump", "description": "", "remedy": "", "date": ""},
                {
                    "title": "Airbag inflator",
                    "description": "Updated",
                    "remedy": "Replace inflator",
                    "date": "2024-03-04",
                },
                {"title": "", "description": "", "remedy": "", "date": ""},
            ],
        )
        report["recallsListExists"] = None
        self.assertEqual(len(health_helpers.safety_recalls({"report": report})), 0)
        self.assertIsNone(health_helpers.safety_recalls({"status": {}}))

    def test_campaigns_need_text_but_still_replace_report_entries(self):
        report = {
            "campaignsExists": True,
            "serviceCampaigns": [
                {
                    "title": "Software",
                    "activityDesc": "Update ECU",
                    "remedyDesc": "Reflash",
                    "campaignDate": "2025",
                    "dealerRefID": "21TA",
                },
                {"title": "Coating", "dealerRefID": "21TB"},
            ],
        }
        campaigns = [
            {
                "campaignType": "ssc",
                "campaignNumber": "21TA",
                "campaignTitle": "",
                "description": "No title",
            },
            {
                "campaignType": "Service Campaigns",
                "campaignNumber": "21TC",
                "campaignTitle": "Wiring",
                "description": "Inspect",
                "recallDate": "2026-01-01",
                "campaignDate": "2025-12-01",
            },
            {
                "campaignType": "rec-s",
                "campaignNumber": "21TB",
                "campaignTitle": "Recall",
                "description": "Not a campaign",
            },
        ]
        self.assertEqual(
            health_helpers.service_campaigns({"report": report, "campaigns": campaigns}),
            [
                {"title": "Coating", "description": "", "remedy": "", "date": ""},
                {"title": "Wiring", "description": "Inspect", "remedy": "", "date": "2026-01-01"},
            ],
        )
        self.assertEqual(health_helpers.service_campaigns({"campaigns": []}), [])
        self.assertIsNone(health_helpers.service_campaigns({}))

    def test_fields_decode_like_the_apps_gson_models(self):
        report = {
            "recallsListExists": "True",
            "safetyRecallsList": [{"title": 2024, "dealerReferenceID": "24001"}, {"title": "Kept"}],
            "campaignsExists": "true",
            "serviceCampaigns": [{"title": "Software", "activityDesc": True, "dealerRefID": 21}],
            "vehicleStatus": {"engOilLevelStatus": 0},
            "maintenanceInformation": {"serviceDue": 5000},
        }
        campaigns = [
            {"campaignType": "rec-s", "campaignNumber": 24001.5, "campaignTitle": "Not a match"},
            {
                "campaignType": "lsc",
                "campaignNumber": 21,
                "campaignTitle": "Replaced",
                "description": "By number",
            },
        ]
        health = {"report": report, "campaigns": campaigns}
        self.assertEqual(
            [item["title"] for item in health_helpers.safety_recalls(health)],
            ["2024", "Kept", "Not a match"],
        )
        self.assertEqual(
            health_helpers.service_campaigns(health),
            [
                {"title": "Replaced", "description": "By number", "remedy": "", "date": ""},
            ],
        )
        self.assertIs(health_helpers.engine_oil_low(health), False)
        self.assertEqual(health_helpers.service_due(health), {"service_due": "5000"})
        report["serviceCampaigns"][0]["dealerRefID"] = "21"
        self.assertEqual(len(health_helpers.service_campaigns(health)), 1)
        report["campaignsExists"] = "yes"
        report["serviceCampaigns"].append({"title": "Hidden"})
        self.assertEqual(len(health_helpers.service_campaigns(health)), 1)

    def test_software_update_follows_the_apps_texts(self):
        self.assertIsNone(health_helpers.software_update_available({}))
        self.assertIsNone(health_helpers.software_update_details({"software": "unexpected"}))
        for software, available, status in (
            ({"updateAvailable": True, "notificationStatus": 1}, True, "available"),
            ({"updateAvailable": "true", "notificationStatus": "5"}, True, "available"),
            ({"updateAvailable": False, "notificationStatus": 2}, False, "initialized"),
            ({"notificationStatus": 7}, False, "initialized"),
            ({"notificationStatus": 4}, False, "failed"),
            ({"notificationStatus": 4.0}, False, "failed"),
            ({"notificationStatus": "4.0"}, False, "failed"),
            ({"notificationStatus": 3}, False, "up_to_date"),
            ({"notificationStatus": None}, False, "up_to_date"),
            ({"notificationStatus": True}, False, "up_to_date"),
        ):
            with self.subTest(software=software):
                health = {"software": software}
                self.assertIs(health_helpers.software_update_available(health), available)
                self.assertEqual(
                    health_helpers.software_update_details(health)["update_status"], status
                )
        self.assertEqual(
            health_helpers.software_update_details({"software": SOFTWARE}),
            {
                "update_name": "Multimedia",
                "version": "2.1",
                "update_status": "available",
                "notification_status": 1,
            },
        )

    def test_alerts_add_diagnostic_warnings_the_report_does_not_list(self):
        health = {
            "report": {
                "vehicleAlertList": [
                    {"wngname": "Check engine", "wngdesc": "Visit a dealer"},
                    {"wngname": "Tire pressure", "wngdesc": ""},
                ]
            },
            "status": {
                "warning": [
                    {"wngdesc": "Check engine", "wngownersManual": "See manual"},
                    {"wngdesc": "Brake system", "wngownersManual": "Stop safely"},
                    {"wngdesc": "Brake system", "wngownersManual": "Duplicate"},
                    {"wngdesc": "Washer fluid"},
                ]
            },
        }
        self.assertEqual(
            health_helpers.vehicle_alerts(health),
            [
                {"title": "Check engine", "description": "Visit a dealer"},
                {"title": "Brake system", "description": "Stop safely"},
            ],
        )
        self.assertEqual(health_helpers.vehicle_alerts({"status": {"warning": "unexpected"}}), [])
        self.assertIsNone(health_helpers.vehicle_alerts({"campaigns": []}))

    def test_oil_key_fob_and_maintenance_match_the_app_text(self):
        def report(**sections):
            return {"report": sections}

        for status, expected in (("Oil LOW", True), ("Normal", False), (None, None)):
            with self.subTest(oil=status):
                self.assertEqual(
                    health_helpers.engine_oil_low(
                        report(vehicleStatus={"engOilLevelStatus": status})
                    ),
                    expected,
                )
        for status, expected in (
            ({"smartKeyBatteryTitle": "Key fob", "smartKeyBatteryDesc": "Battery is low"}, True),
            ({"smartKeyBatteryTitle": "Key fob", "smartKeyBatteryDesc": "Low battery"}, False),
            ({"smartKeyBatteryTitle": "Key fob"}, False),
            ({"smartKeyBatteryDesc": "Battery is low"}, None),
        ):
            with self.subTest(key_fob=status):
                self.assertEqual(
                    health_helpers.key_fob_battery_low(report(vehicleStatus=status)), expected
                )
        for information, expected in (
            ({"maintenanceRequired": True, "serviceDue": "Service due"}, True),
            ({"maintenanceRequired": "TRUE"}, True),
            ({"maintenanceRequired": "false"}, False),
            ({"maintenanceRequired": 1}, False),
            ({}, False),
            (None, None),
        ):
            with self.subTest(maintenance=information):
                self.assertEqual(
                    health_helpers.maintenance_required(report(maintenanceInformation=information)),
                    expected,
                )
        self.assertEqual(
            health_helpers.service_due(
                report(maintenanceInformation={"serviceDue": "Service due"})
            ),
            {"service_due": "Service due"},
        )
        self.assertIsNone(health_helpers.engine_oil_low({"report": "unexpected"}))


def health_entities(hass, entry, vin):
    """Entity IDs of a vehicle's sensors and binary sensors, by name."""
    return {
        item.unique_id.removeprefix(f"{vin}."): item.entity_id
        for item in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        if item.domain in ("sensor", "binary_sensor") and item.unique_id.startswith(f"{vin}.")
    }


def extra_attributes(state):
    return {key: value for key, value in state.attributes.items() if key not in STANDARD_ATTRIBUTES}


async def add_to_account(hass, account, vehicle):
    """List another vehicle in the account and poll it."""
    account.get_vehicles.return_value.append(vehicle)
    await account.coordinator.async_refresh()
    await hass.async_block_till_done()


@pytest.fixture
async def health(hass, setup_vehicles):
    vehicle = FakeVehicle(set())
    vehicle.health = {
        "report": {
            "recallsListExists": True,
            "safetyRecallsList": [{"title": "Airbag", "dealerReferenceID": "24V1"}],
            "vehicleAlertList": [{"wngname": "Check engine", "wngdesc": "Visit a dealer"}],
            "vehicleStatus": {
                "engOilLevelStatus": "Low",
                "smartKeyBatteryTitle": "Key fob",
                "smartKeyBatteryDesc": "Good",
            },
            "maintenanceInformation": {
                "maintenanceRequired": False,
                "serviceDue": "5,000 miles",
            },
        },
        "campaigns": [],
    }
    account = await setup_vehicles([vehicle])

    async def refresh():
        account.coordinator.async_set_updated_data(account.coordinator.data)
        await hass.async_block_till_done()

    return vehicle, account, health_entities(hass, account.entry, "TESTVIN"), refresh


async def test_health_entities_show_the_app_tiles(hass, health):
    _, _, entities, _ = health
    assert set(entities) == {
        "Safety Recalls",
        "Service Campaigns",
        "Vehicle Alerts",
        "Engine Oil",
        "Key Fob Battery",
        "Maintenance Required",
    }
    states = {name: hass.states.get(entity) for name, entity in entities.items()}
    assert states["Safety Recalls"].state == "1"
    assert states["Safety Recalls"].attributes["recalls"][0]["title"] == "Airbag"
    assert states["Service Campaigns"].state == "0"
    assert extra_attributes(states["Vehicle Alerts"]) == {
        "alerts": [{"title": "Check engine", "description": "Visit a dealer"}],
    }
    assert states["Engine Oil"].state == "on"
    assert states["Key Fob Battery"].state == "off"
    maintenance = states["Maintenance Required"]
    assert maintenance.state == "off"
    assert extra_attributes(maintenance) == {"service_due": "5,000 miles"}
    assert maintenance.attributes["device_class"] == BinarySensorDeviceClass.PROBLEM
    assert states["Key Fob Battery"].attributes["device_class"] == BinarySensorDeviceClass.BATTERY


async def test_feature_flags_gate_each_tile(hass, health):
    vehicle, _, entities, refresh = health
    vehicle._feature_flags = {"scheduleMaintenance": 1, "safetyRecall": 2}
    await refresh()

    available = {
        name for name, entity in entities.items() if hass.states.get(entity).state != "unavailable"
    }
    assert available == {"Maintenance Required", "Key Fob Battery"}
    recalls = hass.states.get(entities["Safety Recalls"])
    assert recalls.state == "unavailable"
    assert "recalls" not in recalls.attributes


async def test_software_update_appears_behind_auto_drive(hass, health):
    _, account, _, refresh = health
    other = FakeVehicle(set(), vin="OTAVIN")
    other.health = {"software": SOFTWARE}
    other._feature_flags = {"autoDrive": 1}
    await add_to_account(hass, account, other)

    entities = health_entities(hass, account.entry, "OTAVIN")
    assert set(entities) == {"Software Update"}
    ota = hass.states.get(entities["Software Update"])
    assert ota.attributes["device_class"] == BinarySensorDeviceClass.UPDATE
    assert ota.state == "on"
    assert ota.attributes["version"] == "2.1"
    other._feature_flags = {"autoDrive": 2}
    await refresh()
    assert hass.states.get(entities["Software Update"]).state == "unavailable"


async def test_vehicles_without_health_get_no_health_entities(hass, health):
    _, account, _, refresh = health
    other = FakeVehicle(set(), vin="OTHERVIN")
    await add_to_account(hass, account, other)
    assert health_entities(hass, account.entry, "OTHERVIN") == {}
    other.health = {"status": {"warning": []}}
    await refresh()
    assert set(health_entities(hass, account.entry, "OTHERVIN")) == {"Vehicle Alerts"}
