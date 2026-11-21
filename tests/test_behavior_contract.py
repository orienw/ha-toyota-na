"""Lock in what each generation reads, sends and reports, so a refactor can prove it changed nothing.

A recording client answers every call from `payloads`. Each scenario polls twice, wakes the
vehicle, then sends every supported command. The snapshot holds the client calls with their
arguments and the vehicle's readings after each step, one line each. Run pytest with
`--snapshot-update` only when a behavior change is intended, and review the snapshot diff.
"""

import enum
import json
from dataclasses import fields, is_dataclass
from datetime import datetime

import payloads
import pytest

from custom_components.toyota_na import vehicle_discovery
from custom_components.toyota_na.vehicle import RemoteRequestCommand


class FakeAuth:
    async def get_access_token(self):
        return "token"

    async def get_guid(self):
        return "guid"

    def get_device_id(self):
        return "device"

    async def check_tokens(self, **_):
        return None


class RecordingClient:
    """Answer each client method from the payloads and record its name and arguments."""

    def __init__(self, scenario):
        self.scenario = scenario
        self.calls = []
        self.step = 0
        self.auth = FakeAuth()

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        async def call(*args, **kwargs):
            arguments = [line(arg) for arg in args]
            arguments += [f"{key}={line(value)}" for key, value in kwargs.items()]
            self.calls.append(f"{name}({', '.join(arguments)})")
            return self.respond(name)

        return call

    def respond(self, name):
        scenario, step = self.scenario, self.step
        generation, electric = scenario["generation"], scenario["electric"]
        failing = step and scenario["status_fails_second_poll"]
        if name == "get_user_vehicle_list":
            return [payloads.vehicle_entry(generation, electric)]
        if name == "get_telemetry":
            return payloads.telemetry(step)
        if name.startswith("get_vehicle_status"):
            if failing:
                raise RuntimeError("status read failed")
            return payloads.rest_status(
                step,
                timestamped=scenario["status_timestamped"],
                windows=scenario["status_windows"],
            )
        if name == "graphql_get_vehicle_status":
            if failing:
                raise RuntimeError("status read failed")
            return payloads.graphql_status(step, windows=scenario["status_windows"])
        if name.startswith("get_engine_status"):
            return payloads.engine_status(step)
        if name in ("get_electric_status", "get_electric_realtime_status"):
            return payloads.electric_status(step)
        if name == "get_tire_pressure":
            return {"tirePressure": [], "unit": "psi"}
        if name == "get_vehicle_health_report":
            return payloads.health_report()
        if name == "get_vehicle_health_status":
            return payloads.health_status()
        if name == "get_service_campaigns":
            return payloads.service_campaigns()
        if name == "get_software_update":
            return payloads.software_update()
        if name == "get_notification_history":
            return [{"vin": payloads.vin(generation, electric), "notifications": []}]
        return None


def plain(value):
    """Render readings and call arguments as plain data for the snapshot."""
    if isinstance(value, enum.Enum):
        return value.name
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(plain(key)): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(str(plain(item)) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            type(value).__name__: plain({f.name: getattr(value, f.name) for f in fields(value)})
        }
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"No plain form for {type(value).__name__}")


def line(value) -> str:
    return json.dumps(plain(value), sort_keys=True)


def readings(vehicle) -> list[str]:
    lines = [f"{feature.name}: {line(value)}" for feature, value in vehicle.features.items()]
    lines += [
        f"{name}: {line(getattr(vehicle, name))}"
        for name in (
            "health",
            "climate_settings",
            "charge_settings",
            "climate_schedules",
            "notifications",
            "subscribed",
            "electric",
            "can_receive_status",
            "uses_appsync",
            "endpoint_generation",
        )
    ]
    return sorted(lines)


VEHICLES = [
    (generation, electric)
    for generation, fuel_types in payloads.GENERATIONS
    for electric in fuel_types
]
# Only REST status can arrive without a timestamp.
REST_STATUS = {"17CY", "17CYPLUS", "21MM", "NG86", "GR86"}


def scenario(generation, electric, **changes):
    return {
        "generation": generation,
        "electric": electric,
        "window_telemetry": False,
        "status_timestamped": True,
        "status_fails_second_poll": False,
        "status_windows": True,
        **changes,
    }


SCENARIOS = [
    *(scenario(*vehicle) for vehicle in VEHICLES),
    *(scenario(*vehicle, status_fails_second_poll=True) for vehicle in VEHICLES),
    *(
        scenario(*vehicle, status_timestamped=False)
        for vehicle in VEHICLES
        if vehicle[0] in REST_STATUS
    ),
    # Telemetry fills windows and the moonroof that status never reports.
    *(scenario(*vehicle, window_telemetry=True, status_windows=False) for vehicle in VEHICLES),
    # Status wins wherever it reports them, for each status transport.
    *(
        scenario(generation, False, window_telemetry=True)
        for generation in ("17CY", "21MM", "NG86", "24MM")
    ),
]


def scenario_id(scenario):
    parts = [scenario["generation"], "ev" if scenario["electric"] else "gas"]
    if scenario["window_telemetry"]:
        parts.append("windowtelemetry" if scenario["status_windows"] else "telemetryonlywindows")
    if not scenario["status_timestamped"]:
        parts.append("untimestamped")
    if scenario["status_fails_second_poll"]:
        parts.append("statusfails")
    return "-".join(parts)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=scenario_id)
async def test_generation_behavior(scenario, snapshot, freezer):
    freezer.move_to("2026-09-15T12:10:00Z")
    client = RecordingClient(scenario)
    known_vehicles = {}
    result = {}
    for step in (0, 1):
        client.step = step
        client.calls = []
        [vehicle] = await vehicle_discovery.get_vehicles(
            client,
            window_telemetry=scenario["window_telemetry"],
            vehicles_by_vin=known_vehicles,
            unsupported_vins=set(),
        )
        result[f"poll {step + 1} calls"] = client.calls
        result[f"poll {step + 1} readings"] = readings(vehicle)

    client.calls = []
    try:
        await vehicle.poll_vehicle_refresh()
    except Exception as err:
        client.calls.append(f"raised {type(err).__name__}: {err}")
    result["wake calls"] = client.calls

    commands, unsupported = [], []
    # Refresh is the wake above, not a command.
    for command in (item for item in RemoteRequestCommand if item != RemoteRequestCommand.Refresh):
        if not vehicle.supports_command(command):
            unsupported.append(command.name)
            continue
        client.calls = []
        try:
            await vehicle.send_command(command)
        except Exception as err:
            client.calls.append(f"raised {type(err).__name__}: {err}")
        commands += [f"{command.name}: {call}" for call in client.calls] or [f"{command.name}:"]
    result["commands"] = commands
    result["unsupported commands"] = unsupported
    after = readings(vehicle)
    result["readings after commands"] = (
        "same as poll 2" if after == result["poll 2 readings"] else after
    )

    assert result == snapshot
