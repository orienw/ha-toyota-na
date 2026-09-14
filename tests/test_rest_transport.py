"""REST routing through the integration and upstream client wrappers."""

import importlib.util
from pathlib import Path
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import aiohttp
from toyota_na.client import ToyotaOneClient


SPEC = importlib.util.spec_from_file_location(
    "rest_patch_client",
    Path(__file__).resolve().parents[1] / "custom_components/toyota_na/patch_client.py",
)
client_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client_module)


class Client:
    api_get = ToyotaOneClient.api_get
    api_post = ToyotaOneClient.api_post
    api_request = client_module.api_request
    get_electric_status = client_module.get_electric_status
    auth = types.SimpleNamespace(get_device_id=lambda: "device")

    async def _auth_headers(self):
        return {"AUTHORIZATION": "Bearer test-token"}


class RestTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = AsyncMock()
        self.response.status = 200
        self.response.raise_for_status = MagicMock()
        self.response.json.return_value = {"payload": {"status": "started"}}
        self.response.__aenter__.return_value = self.response
        self.session = MagicMock()
        self.session.__aenter__.return_value = self.session
        self.session.request.return_value = self.response
        self.patch = patch.object(client_module.aiohttp, "ClientSession", return_value=self.session)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_electric_status_uses_generation_specific_version_and_headers(self):
        status = {"vehicleInfo": {"chargeInfo": {"chargeRemainingAmount": 80}}}
        self.response.json.return_value = {"payload": status}
        for generation, version in (("17CY", "v2"), ("17CYPLUS", "v3"), ("21MM", "v3")):
            with self.subTest(generation=generation):
                result = await Client().get_electric_status(
                    "TESTVIN", region="CA", generation=generation,
                )
                args, kwargs = self.session.request.call_args
                self.assertEqual(args, ("GET", f"https://onecdn.telematicsct.com/oneapi/{version}/electric/status"))
                self.assertEqual(kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(kwargs["headers"]["x-region"], "CA")
                self.assertEqual(result, status)

    async def test_electric_refresh_followup_keeps_generation_and_request_number(self):
        status = {"vehicleInfo": {"chargeInfo": {"plugStatus": 40}}}
        for generation, version, query in (
            ("17CY", "v2", ""),
            ("17CYPLUS", "v3", "?realtime-status=request-123"),
            ("21MM", "v3", "?realtime-status=request-123"),
        ):
            with self.subTest(generation=generation):
                self.session.request.reset_mock()
                self.response.json.side_effect = [
                    {"payload": {"appRequestNo": "request-123", "returnCode": "ONE-RES-10000"}},
                    {"payload": status},
                ]
                result = await client_module.get_electric_realtime_status(
                    Client(), "TESTVIN", generation, "CA",
                )
                refresh, followup = self.session.request.call_args_list
                self.assertEqual(refresh.args, ("POST", "https://onecdn.telematicsct.com/oneapi/v2/electric/realtime-status"))
                self.assertEqual(refresh.kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(refresh.kwargs["headers"]["device-id"], "device")
                UUID(refresh.kwargs["headers"]["X-CORRELATIONID"])
                self.assertEqual(followup.args, ("GET", f"https://onecdn.telematicsct.com/oneapi/{version}/electric/status{query}"))
                self.assertEqual(followup.kwargs["headers"]["X-GENERATION"], generation)
                self.assertEqual(result, status)

    async def test_21mm_command_reaches_cdn_root_with_vehicle_headers(self):
        await client_module.remote_request_21mm(Client(), "TESTVIN", "engine-start", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://onecdn.telematicsct.com/v1/remote/route/command"))
        self.assertEqual(kwargs["json"], {"command": "engine-start", "autoFixPopup": False})
        headers = kwargs["headers"]
        self.assertEqual(headers["VIN"], "TESTVIN")
        self.assertEqual(headers["X-GENERATION"], "21MM")
        self.assertEqual(headers["X-BRAND"], "T")
        self.assertEqual(headers["x-region"], "CA")
        self.assertEqual(headers["Content-Type"], "application/json")
        UUID(headers["X-CORRELATIONID"])

    async def test_21mm_engine_status_reaches_cdn_root(self):
        result = await client_module.get_engine_status_21mm(Client(), "TESTVIN", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", "https://onecdn.telematicsct.com/v1/remote/route/engine-status"))
        self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(kwargs["headers"]["x-region"], "CA")
        self.assertEqual(result, {"status": "started"})

    async def test_21mm_vehicle_status_reaches_cdn_root(self):
        status = {"vehicleStatus": [], "latitude": 34.05, "longitude": -118.25}
        self.response.json.return_value = {"payload": status}

        result = await client_module.get_vehicle_status_21mm(Client(), "TESTVIN", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("GET", "https://onecdn.telematicsct.com/v1/remote/route/status"))
        self.assertEqual(kwargs["headers"]["VIN"], "TESTVIN")
        self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(kwargs["headers"]["x-region"], "CA")
        self.assertEqual(result, status)

    async def test_21mm_refresh_uses_route_request_body(self):
        await client_module.send_refresh_request_21mm(Client(), "TESTVIN", "CA")

        args, kwargs = self.session.request.call_args
        self.assertEqual(args, ("POST", "https://onecdn.telematicsct.com/v1/remote/route/refresh-status"))
        self.assertEqual(kwargs["json"], {"autoFixPopup": False})
        self.assertEqual(kwargs["headers"]["VIN"], "TESTVIN")
        self.assertEqual(kwargs["headers"]["X-GENERATION"], "21MM")
        self.assertEqual(kwargs["headers"]["x-region"], "CA")

    async def test_rejected_21mm_command_raises(self):
        self.response.status = 400
        self.response.text.return_value = "rejected"
        self.response.raise_for_status.side_effect = aiohttp.ClientResponseError(
            MagicMock(), (), status=400,
        )

        with self.assertRaises(aiohttp.ClientResponseError):
            await client_module.remote_request_21mm(Client(), "TESTVIN", "engine-start")
