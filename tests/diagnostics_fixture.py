"""Turn a diagnostics download into a test fixture, and answer the integration from one.

A fixture holds what Toyota actually returned for a reporter's vehicles, so the tests can run the
integration against it instead of against payloads written from the app. To add one:

    python tests/diagnostics_fixture.py <download.json> <name>
    pytest tests/test_reported_vehicles.py --snapshot-update

Read the new file in `tests/fixtures/diagnostics/` before committing it. Home Assistant has already
redacted identifiers, but Toyota's free text is kept. Then read the new snapshot: it is what the
integration makes of that vehicle.

A fixture's test runs at the fixture's `recorded` time, so readings are as old as the reporter saw
them. It is set to when the fixture was made; change it if the download is older than that.
"""

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "diagnostics"
REDACTED = "**REDACTED**"
SECTIONS = (
    "vehicle_status",
    "telemetry",
    "engine_status",
    "electric_status",
    "climate_settings",
    "climate_schedules",
    "tire_pressure",
    "vehicle_health",
    "notification_history",
)
# Client reads, by where a fixture holds their response.
READS = {
    "get_telemetry": ("telemetry",),
    "get_electric_status": ("electric_status",),
    "get_climate_settings": ("climate_settings",),
    "get_climate_schedules": ("climate_schedules",),
    "get_tire_pressure": ("tire_pressure",),
    "get_vehicle_health_report": ("vehicle_health", "report"),
    "get_vehicle_health_status": ("vehicle_health", "status"),
    "get_service_campaigns": ("vehicle_health", "campaigns"),
    "get_software_update": ("vehicle_health", "software"),
}
STATUS_READS = {
    "get_vehicle_status": ("vehicle_status",),
    "graphql_get_vehicle_status": ("vehicle_status",),
    "get_engine_status": ("engine_status",),
}


def _restore(value, vin):
    """Give redacted VINs and coordinates stand-ins the integration can read."""
    if isinstance(value, list):
        return [_restore(item, vin) for item in value]
    if not isinstance(value, dict):
        return value
    restored = {}
    for key, item in value.items():
        if item == REDACTED and key == "vin":
            restored[key] = vin
        elif item == REDACTED and key in ("latitude", "longitude"):
            restored[key] = 0.0
        else:
            restored[key] = _restore(item, vin)
    return restored


def fixture_from_download(download: dict) -> dict:
    """Keep each vehicle's responses and the options; drop the account and the installation."""
    data = download.get("data", download)
    vehicles = [
        vehicle
        for vehicle in data["vehicle_list"]["data"] or []
        if isinstance(vehicle, dict) and isinstance(vehicle.get("vin"), str)
    ]
    fixture = {"options": data.get("options") or {}, "vehicles": []}
    for index, vehicle in enumerate(vehicles):
        responses = {"entry": vehicle}
        for section in SECTIONS:
            # Older versions report fewer sections.
            items = (data.get(section) or {}).get("data") or []
            responses[section] = items[index] if index < len(items) else None
        fixture["vehicles"].append(_restore(responses, f"REPORTED{index:09d}"))
    return fixture


class ReportedClient:
    """Toyota's cloud as a fixture reported it. Every other request returns nothing."""

    def __init__(self, fixture, auth, session=None):
        self.auth = auth
        self._vehicles = {vehicle["entry"]["vin"]: vehicle for vehicle in fixture["vehicles"]}

    async def get_user_vehicle_list(self):
        return [copy.deepcopy(vehicle["entry"]) for vehicle in self._vehicles.values()]

    async def get_notification_history(self):
        return [
            {"vin": vin, "notifications": copy.deepcopy(vehicle["notification_history"] or [])}
            for vin, vehicle in self._vehicles.items()
        ]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        path = READS.get(name) or next(
            (path for prefix, path in STATUS_READS.items() if name.startswith(prefix)), ()
        )

        async def read(vin=None, *args, **kwargs):
            response = self._vehicles[vin] if path else None
            for key in path:
                response = response.get(key) if isinstance(response, dict) else None
            return copy.deepcopy(response)

        return read


if __name__ == "__main__":
    source, name = sys.argv[1:]
    target = FIXTURES / f"{name}.json"
    target.parent.mkdir(exist_ok=True)
    fixture = {
        "recorded": datetime.now(UTC).isoformat(timespec="seconds"),
        **fixture_from_download(json.loads(Path(source).read_text())),
    }
    target.write_text(json.dumps(fixture, indent=2) + "\n")
    print(f"Wrote {target} with {len(fixture['vehicles'])} vehicle(s)")
