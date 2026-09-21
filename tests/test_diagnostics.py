"""Ensure diagnostic payloads use exact account identifier redaction keys."""

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import test_button as ha

ha.ha_const.CONF_ACCESS_TOKEN = "access_token"
ha.ha_const.CONF_EMAIL = "email"
ha.ha_const.CONF_PASSWORD = "password"
ha.module("homeassistant.components.diagnostics").async_redact_data = MagicMock()

from custom_components.toyota_na import diagnostics


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_vehicle_and_config_data_use_account_identifier_redaction(self):
        vehicle = {
            "vin": "vin", "generation": "24MM",
            "remoteUserGuid": "remote-user", "subscriberGuid": "subscriber",
            "accountInfoId": "account", "modelName": "RAV4",
        }
        client = types.SimpleNamespace(
            get_user_vehicle_list=AsyncMock(return_value=[vehicle]),
            graphql_get_vehicle_status=AsyncMock(return_value={}),
            get_telemetry=AsyncMock(return_value={}),
        )
        entry = ha.ConfigEntry()
        entry.data = {"email": "owner@example.com", "tokens": {"guid": "owner"}}
        hass = ha.FakeHass(None)
        hass.data[ha.DOMAIN][entry.entry_id]["toyota_na_client"] = client
        with patch.object(diagnostics, "async_redact_data", side_effect=lambda data, keys: data) as redact:
            await diagnostics.async_get_config_entry_diagnostics(hass, entry)
        config_data, config_keys = redact.call_args_list[0].args
        payload, payload_keys = redact.call_args_list[1].args
        self.assertEqual(entry.data, config_data)
        self.assertEqual([vehicle], payload["vehicle_list"]["data"])
        for keys in (config_keys, payload_keys):
            self.assertTrue({"remoteUserGuid", "subscriberGuid", "accountInfoId", "guid", "vin", "email", "password"} <= keys)
