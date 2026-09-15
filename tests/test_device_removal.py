"""Allow removing vehicles only after a successful account lookup."""

import types
import unittest
from unittest.mock import AsyncMock

import test_button as ha


class DeviceRemovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.hass = ha.FakeHass(ha.DataUpdateCoordinator([]))
        self.entry = ha.ConfigEntry()
        self.device = types.SimpleNamespace(identifiers={(ha.DOMAIN, "SOLDVIN")})
        self.client = types.SimpleNamespace(get_user_vehicle_list=AsyncMock())
        self.hass.data[ha.DOMAIN][self.entry.entry_id]["toyota_na_client"] = self.client

    async def test_removal_checks_the_account_including_unsupported_vehicles(self):
        for vehicles, allowed in (
            ([], True),
            ([{"vin": "OTHERVIN"}], True),
            ([{"vin": "SOLDVIN", "generation": "FUTURE"}], False),
            (None, False),
            ({}, False),
            ([{}], False),
        ):
            with self.subTest(vehicles=vehicles):
                self.client.get_user_vehicle_list.return_value = vehicles
                self.assertEqual(await ha.integration_runtime.async_remove_config_entry_device(
                    self.hass, self.entry, self.device,
                ), allowed)

    async def test_failed_lookup_does_not_authorize_removal(self):
        self.client.get_user_vehicle_list.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await ha.integration_runtime.async_remove_config_entry_device(
                self.hass, self.entry, self.device,
            )

    async def test_unloaded_account_and_foreign_device_cannot_be_removed(self):
        self.device.identifiers = {("other", "SOLDVIN")}
        self.assertFalse(await ha.integration_runtime.async_remove_config_entry_device(
            self.hass, self.entry, self.device,
        ))
        self.device.identifiers = {(ha.DOMAIN, "SOLDVIN")}
        del self.hass.data[ha.DOMAIN][self.entry.entry_id]
        self.assertFalse(await ha.integration_runtime.async_remove_config_entry_device(
            self.hass, self.entry, self.device,
        ))
        self.client.get_user_vehicle_list.assert_not_awaited()
