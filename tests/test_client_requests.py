"""Lock in the exact request each Toyota client method sends: method, URL, headers and body.

A session records requests and answers each with an empty success. New client methods are
picked up automatically and fail until they have example arguments. Run pytest with
`--snapshot-update` only when a request is meant to change, and review the snapshot diff.
"""

import inspect
import json
import time

import pytest

from custom_components.toyota_na.client import ToyotaOneClient

# Generated per request.
VOLATILE_HEADERS = {"x-correlationid", "x-correlation-id"}

ARGUMENTS = {
    "vin": "VIN0000000000TEST",
    "generation": "21MM",
    "region": "US",
    "brand": "L",
    "command": "door-lock",
    "value": 1,
    "guid": "guid",
    "backdoor_type": "hatch",
    "settings": {"acOperations": []},
}

# These wait for Toyota to confirm the command's result.
CONFIRMED_COMMANDS = {
    "electric_command",
    "remote_request_24mm",
    "save_charge_schedule",
    "save_climate_schedule",
    "update_charge_settings",
}
GENERIC = {"api_get", "api_post", "api_request", "graphql_request"}


class Response:
    status = 200
    reason = "OK"
    headers = {}

    async def text(self):
        return json.dumps({"payload": {}, "data": {}})

    async def json(self, **_):
        return {"payload": {}, "data": {}}

    def raise_for_status(self):
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RecordingSession:
    closed = False

    def __init__(self):
        self.requests = []

    def request(self, method, url, headers=None, **kwargs):
        body = kwargs.get("json", kwargs.get("data"))
        if isinstance(body, str):
            try:
                body = json.loads(body)
            except ValueError:
                pass
        self.requests.append(
            {
                "method": method.upper(),
                "url": str(url),
                "headers": {
                    key: value
                    for key, value in sorted((headers or {}).items())
                    if key.lower() not in VOLATILE_HEADERS
                },
                "body": body,
            }
        )
        return Response()

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


class Auth:
    async def get_access_token(self):
        return "access-token"

    async def get_guid(self):
        return "account-guid"

    def get_device_id(self):
        return "device-id"

    async def check_tokens(self, **_):
        return None


METHODS = sorted(
    name
    for name, member in inspect.getmembers(ToyotaOneClient)
    if inspect.iscoroutinefunction(member)
    and not name.startswith("_")
    and name not in CONFIRMED_COMMANDS | GENERIC
)


@pytest.fixture(autouse=True)
def device_timezone(monkeypatch):
    """The timezone header follows the host; pin it so the snapshot holds everywhere."""
    monkeypatch.setattr(time, "tzname", ("PST", "PDT"))


@pytest.mark.parametrize("name", METHODS)
async def test_client_request(name, snapshot):
    session = RecordingSession()
    method = getattr(ToyotaOneClient(Auth(), session=session), name)
    parameters = inspect.signature(method).parameters
    arguments = {key: value for key, value in ARGUMENTS.items() if key in parameters}
    missing = [
        key
        for key, parameter in parameters.items()
        if key not in arguments and parameter.default is inspect.Parameter.empty
    ]
    assert not missing, f"Add example arguments for {missing}"
    try:
        await method(**arguments)
        outcome = "returned"
    except Exception as err:
        outcome = f"raised {type(err).__name__}: {err}"
    assert {"outcome": outcome, "requests": session.requests} == snapshot
