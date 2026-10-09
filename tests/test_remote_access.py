"""Repairs follow each vehicle's remote access state."""

import unittest
from unittest import mock

import test_button as ha
from custom_components.toyota_na.remote_access import sync_remote_access_issues


class RemoteAccessIssueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = ha.FakeHass(ha.DataUpdateCoordinator([]))
        self.entry = ha.ConfigEntry()
        self.vehicle = ha.FakeVehicle(set())

    def issues(self):
        return {
            issue_id: issue["translation_key"]
            for (domain, issue_id), issue in self.hass.issue_registry.issues.items()
            if domain == ha.DOMAIN
        }

    def test_only_states_needing_action_raise_a_repair(self):
        for remote_display, expected in (
            (None, None),
            (0, None),
            (7, None),
            (12, None),
            (1, "remote_authorization_required"),
            (2, "remote_subscription_cancelled"),
            (3, "remote_subscription_cancelled"),
            (4, "remote_activation_failed"),
            (5, "remote_activation_pending"),
            (6, "remote_activation_error"),
            (8, "remote_subscription_expired"),
            (9, "remote_subscription_expired"),
            (10, "vehicle_stolen"),
            (11, "vehicle_stolen_immobilizer"),
            (7, None),
        ):
            with self.subTest(remote_display=remote_display):
                self.vehicle.remote_display = remote_display
                sync_remote_access_issues(self.hass, self.entry, [self.vehicle])
                self.assertEqual(
                    self.issues(),
                    {f"remote_access_entry_TESTVIN_{expected}": expected} if expected else {},
                )

    def test_repair_names_the_vehicle(self):
        self.vehicle.remote_display = 1
        sync_remote_access_issues(self.hass, self.entry, [self.vehicle])
        self.assertEqual(
            list(self.hass.issue_registry.issues.values()),
            [
                {
                    "is_fixable": False,
                    "severity": "warning",
                    "translation_key": "remote_authorization_required",
                    "translation_placeholders": {"vehicle": "2024 LC 500 2-DOOR COUPE"},
                }
            ],
        )

    def test_only_this_accounts_departed_vehicles_lose_their_repairs(self):
        other_entry = ha.ConfigEntry()
        other_entry.entry_id = "other"
        sold = ha.FakeVehicle(set(), vin="SOLDVIN")
        self.vehicle.remote_display = sold.remote_display = 5
        self.hass.issue_registry.issues[(ha.DOMAIN, "unrelated")] = {"translation_key": "unrelated"}
        sync_remote_access_issues(self.hass, self.entry, [self.vehicle, sold])
        sync_remote_access_issues(self.hass, other_entry, [sold])
        sync_remote_access_issues(self.hass, self.entry, [self.vehicle])
        self.assertEqual(
            set(self.issues()),
            {
                "remote_access_entry_TESTVIN_remote_activation_pending",
                "remote_access_other_SOLDVIN_remote_activation_pending",
                "unrelated",
            },
        )

    async def test_polls_update_repairs_and_failed_polls_keep_them(self):
        self.vehicle.remote_display = 4
        coordinator = ha.DataUpdateCoordinator([])
        self.hass.data[ha.DOMAIN]["entry"]["coordinator"] = coordinator
        with (
            mock.patch.object(
                ha.integration_runtime, "get_vehicles", mock.AsyncMock(return_value=[self.vehicle])
            ),
            mock.patch.object(ha.integration_runtime, "automatic_wake_due", return_value=False),
        ):
            await ha.integration_runtime.update_vehicles_status(
                self.hass, object(), self.entry, coordinator
            )
        self.assertEqual(
            self.issues(),
            {"remote_access_entry_TESTVIN_remote_activation_failed": "remote_activation_failed"},
        )
        with (
            mock.patch.object(
                ha.integration_runtime, "get_vehicles", mock.AsyncMock(side_effect=RuntimeError())
            ),
            self.assertLogs(ha.integration_runtime.__name__, level="ERROR"),
            self.assertRaises(ha.update_coordinator.UpdateFailed),
        ):
            await ha.integration_runtime.update_vehicles_status(
                self.hass, object(), self.entry, coordinator
            )
        self.assertEqual(
            self.issues(),
            {"remote_access_entry_TESTVIN_remote_activation_failed": "remote_activation_failed"},
        )

    async def test_removing_the_account_clears_its_repairs(self):
        self.vehicle.remote_display = 10
        sync_remote_access_issues(self.hass, self.entry, [self.vehicle])
        await ha.integration_runtime.async_remove_entry(self.hass, self.entry)
        self.assertEqual(self.issues(), {})
