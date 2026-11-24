"""A fake Toyota cloud behind a fake aiohttp session, for tests that run the real integration.

Everything above aiohttp is real: auth, client, vehicles, coordinator and the WebSocket handler.
The cloud answers REST and AppSync requests from `payloads`, checks access tokens, issues new
ones on refresh, and serves the AppSync WebSocket so tests can push vehicle status.
"""

import asyncio
import copy
import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import jwt
import payloads
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from custom_components.toyota_na.auth import ToyotaOneAuth

INITIAL_ACCESS_TOKEN = "test-access"  # The token `common.account_entry` saves.


class Response:
    def __init__(self, method, url, status, body):
        self.method = method
        self.url = URL(url)
        self.status = status
        self.reason = "OK" if status < 400 else "Error"
        self.headers = CIMultiDictProxy(CIMultiDict())
        self._body = body

    async def text(self):
        return json.dumps(self._body)

    async def json(self, **_):
        return copy.deepcopy(self._body)

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                aiohttp.RequestInfo(self.url, self.method, self.headers, self.url),
                (),
                status=self.status,
                message=self.reason,
                headers=self.headers,
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Socket:
    """The AppSync realtime connection: acknowledges the handshake and each subscription."""

    def __init__(self, cloud):
        self._cloud = cloud
        self._frames = asyncio.Queue()
        self.closed = False

    async def send_json(self, frame):
        if frame["type"] == "connection_init":
            self._put({"type": "connection_ack", "payload": {"connectionTimeoutMs": 300_000}})
        elif frame["type"] == "start":
            authorization = frame["payload"]["extensions"]["authorization"]
            if authorization["Authorization"] != f"Bearer {self._cloud.access_token}":
                # AppSync's subscription error, with the type Toyota's app treats as a
                # rejected token. Not observed from Toyota.
                error = {"errorType": "APPSYNC-AUTH-403", "message": "Unauthorized"}
                self._put({"type": "error", "id": frame["id"], "payload": {"errors": [error]}})
                return
            vin = json.loads(frame["payload"]["data"])["variables"]["vin"]
            self._cloud.subscriptions[vin] = frame["id"]
            self._put({"type": "start_ack", "id": frame["id"]})

    async def receive(self):
        frame = await self._frames.get()
        if frame is None:
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSED, data=None)
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))

    async def close(self):
        if not self.closed:
            self.closed = True
            self._frames.put_nowait(None)

    def _put(self, frame):
        if not self.closed:
            self._frames.put_nowait(frame)


class Connecting:
    """Like aiohttp's `ws_connect`: awaited for push updates, or used with `async with`."""

    def __init__(self, cloud):
        self._socket = Socket(cloud)
        cloud.sockets.append(self._socket)

    def __await__(self):
        return self._opened().__await__()

    async def _opened(self):
        return self._socket

    async def __aenter__(self):
        return self._socket

    async def __aexit__(self, *exc):
        await self._socket.close()
        return False


class Session:
    """The aiohttp session the integration gets from Home Assistant."""

    closed = False

    def __init__(self, cloud):
        self._cloud = cloud

    def request(self, method, url, headers=None, **kwargs):
        body = kwargs.get("json", kwargs.get("data"))
        if isinstance(body, str):
            body = json.loads(body)
        status, reply = self._cloud.handle(method.upper(), str(url), headers or {}, body)
        return Response(method.upper(), str(url), status, reply)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def ws_connect(self, url, **_):
        return Connecting(self._cloud)

    async def close(self):
        self.closed = True


class ToyotaCloud:
    def __init__(self, vehicles):
        self.vehicles = vehicles
        self.session = Session(self)
        self.access_token = INITIAL_ACCESS_TOKEN
        self.refresh_works = True
        self.refreshes = 0
        # Vehicle readings Toyota returns, by VIN.
        self.graphql_status = {}
        self.rest_status = {}
        self.telemetry = {}
        self.engine_status = {}
        # VINs whose AppSync status read Toyota refuses with APIGW-403, whatever the token.
        self.refused_vins = set()
        self.requests = []
        self.sockets = []
        self.subscriptions = {}
        for vehicle in vehicles:
            vin = vehicle["vin"]
            self.graphql_status[vin] = payloads.graphql_status(0, vin)
            self.rest_status[vin] = payloads.rest_status(0)
            self.telemetry[vin] = payloads.telemetry(0)
            self.engine_status[vin] = payloads.engine_status(1)

    def revoke_access_token(self, *, refresh_works=True):
        """Reject the current access token, as Toyota does when it revokes one early."""
        self.access_token = f"revoked-{self.access_token}"
        self.refresh_works = refresh_works

    @property
    def socket(self):
        """The live update connection, the first one the integration opens."""
        return self.sockets[0] if self.sockets else None

    def push_status(self, vin, status):
        """Send a vehicle status update over the vehicle's subscription."""
        self.socket._put(
            {
                "type": "data",
                "id": self.subscriptions[vin],
                "payload": {"data": {"onVehicleStatusUpdated": status}},
            }
        )

    def requested(self, path=None, operation=None):
        return [
            request
            for request in self.requests
            if (path is None or request["path"].endswith(path))
            and (operation is None or request["operation"] == operation)
        ]

    def handle(self, method, url, headers, body):
        path = urlsplit(url).path
        operation = body.get("operationName") if isinstance(body, dict) else None
        self.requests.append({"method": method, "path": path, "operation": operation})
        if url == ToyotaOneAuth.ACCESS_TOKEN_URL:
            return self._refresh()
        authorization = headers.get("AUTHORIZATION") or headers.get("Authorization") or ""
        if authorization != f"Bearer {self.access_token}":
            return 401, {"errors": [{"message": "Unauthorized"}]}
        if path == "/graphql":
            return self._graphql(operation, body.get("variables") or {})
        return 200, {"payload": self._rest(method, path, headers.get("VIN"))}

    def _refresh(self):
        if not self.refresh_works:
            return 400, {"error": "invalid_grant"}
        self.refreshes += 1
        self.access_token = f"access-{self.refreshes}"
        return 200, {
            "access_token": self.access_token,
            "refresh_token": f"refresh-{self.refreshes}",
            "id_token": jwt.encode(
                {"sub": "test-guid", "aud": "oneappsdkclient"}, key="", algorithm="none"
            ),
            "expires_in": 3600,
        }

    def _graphql(self, operation, variables):
        if operation == "GetVehicleStatus":
            vin = variables["vin"]
            if vin in self.refused_vins:
                return 200, {
                    "data": {"getVehicleStatus": None},
                    "errors": [{"errorType": "APIGW-403", "message": "Forbidden"}],
                }
            return 200, {"data": {"getVehicleStatus": copy.deepcopy(self.graphql_status[vin])}}
        if operation == "ConfirmSubscriptionStatus":
            return 200, {"data": {"confirmSubscriptionStatus": {"status": "ACTIVE"}}}
        return 200, {"data": {}}

    def _rest(self, method, path, vin):
        if path.endswith("/v2/vehicle/guid"):
            return copy.deepcopy(self.vehicles)
        if path.endswith("/v2/telemetry"):
            return copy.deepcopy(self.telemetry[vin])
        # Legacy, global and route paths, by generation.
        if path.endswith(("/remote/status", "/remote/route/status")):
            return copy.deepcopy(self.rest_status[vin])
        if path.endswith(("/remote/engine-status", "/remote/route/engine-status")):
            return copy.deepcopy(self.engine_status[vin])
        if path.endswith("/telemetry/tires/pressure"):
            return payloads.tire_pressure(0, vin)
        if path.endswith("/notification/history"):
            return [{"vin": vehicle["vin"], "notifications": []} for vehicle in self.vehicles]
        if path.endswith("/electric/status"):
            return payloads.electric_status(0)
        if method == "GET" and path.endswith("/remote/route/ac-reservation"):
            return payloads.climate_schedules()
        if method == "GET" and path.endswith("/remote/route/climate-settings"):
            return payloads.climate_settings()
        for ending, response in (
            ("/vehiclehealth/report", payloads.health_report),
            ("/vehiclehealth/status", payloads.health_status),
            ("/service-campaign", payloads.service_campaigns),
            ("/ota/update/check", payloads.software_update),
        ):
            if path.endswith(ending):
                return response()
        # Settings and command acknowledgements: accepted, nothing to report.
        return None
