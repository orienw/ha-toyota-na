"""Toyota responses for every vehicle generation, shaped like the app's server models.

The values are synthetic. Each builder takes a step, so a second poll can report changed readings.
"""

import copy
import json
from pathlib import Path

STATUS_24MM = json.loads((Path(__file__).parent / "fixtures" / "vehicle_24mm.json").read_text())

# Generations with the fuel types Toyota sells them in.
GENERATIONS = (
    ("17CY", (False, True)),
    ("17CYPLUS", (False, True)),
    ("21MM", (False, True)),
    ("24MM", (False, True)),
    ("26BEV", (True,)),
    ("NG86", (False,)),
    ("GR86", (False,)),
)

CAPABILITIES = dict.fromkeys(
    [
        "buzzerCapable",
        "climateCapable",
        "dlockUnlockCapable",
        "doorLockUnlockCapable",
        "estartEnabled",
        "estartStopCapable",
        "estopEnabled",
        "hazardCapable",
        "hornCapable",
        "lightsCapable",
        "moonroofCloseCapable",
        "powerTailgateCapable",
        "powerWindowsCloseCapable",
        "powerWindowsOpenCapable",
        "remoteEConnectCapable",
        "trunkLockUnlockCapable",
        "vehicleFinderCapable",
    ],
    True,
)
# The app reads feature flags as Integer 1.
FEATURES = dict.fromkeys(
    [
        "chargeSetting",
        "multiDayCharging",
        "remoteClimate",
        "remoteCommands",
        "tirePressure",
        "vehicleState",
        "vehicleHealthReport",
        "vehicleDiagnostic",
        "scheduleMaintenance",
        "safetyRecall",
        "serviceCampaign",
        "autoDrive",
        "ssa",
    ],
    1,
)


def vin(generation: str, electric: bool) -> str:
    return f"VIN{generation:0>13}{'E' if electric else 'G'}"


def vehicle_entry(generation: str, electric: bool, *, capabilities: bool = True) -> dict:
    """One vehicle from `v2/vehicle/guid`.

    Every capability and feature is on, to reach each request a generation can send. That is
    more than Toyota reports for any one vehicle. Without capabilities, the entry has none of
    those models, like the older payloads that omit them.
    """
    entry = {
        "vin": vin(generation, electric),
        "generation": generation,
        "brand": "T",
        "region": "US",
        "modelYear": "2025",
        "modelName": "TEST",
        "remoteSubscriptionStatus": "ACTIVE",
        "evVehicle": electric,
        "backdoorType": "hatch",
        "remoteDisplay": 0,
    }
    if capabilities:
        entry |= {
            "remoteServiceCapabilities": dict(CAPABILITIES),
            "extendedCapabilities": dict(CAPABILITIES),
            "features": dict(FEATURES),
            "capabilities": [{"name": "remoteStart", "value": True}],
        }
    return entry


def rest_status(step: int, *, timestamped: bool = True, windows: bool = True) -> dict:
    """REST vehicle status: the driver's door opens on the second step.

    Without windows, it reports no window or moonroof, like vehicles that only report them in
    telemetry.
    """

    def section(name, *values):
        return {"section": name, "values": [{"value": value} for value in values]}

    door = "open" if step else "closed"
    status = {
        "latitude": 34.05,
        "longitude": -118.25 - step,
        "vehicleStatus": [
            {
                "category": "Driver Side",
                "sections": [section("Door", door, "unlocked"), section("Window", "closed")],
            },
            {
                "category": "Passenger Side",
                "sections": [section("Door", "closed", "locked"), section("Window", "open")],
            },
            {
                "category": "Driver Side Rear",
                "sections": [section("Door", "closed", "locked"), section("Window", "closed")],
            },
            {
                "category": "Passenger Side Rear",
                "sections": [section("Door", "closed", "locked"), section("Window", "closed")],
            },
            {
                "category": "Other",
                "sections": [
                    section("Hatch", "closed", "locked"),
                    section("Moonroof", "closed"),
                    section("Hood", "closed"),
                ],
            },
        ],
    }
    if not windows:
        for category in status["vehicleStatus"]:
            category["sections"] = [
                item
                for item in category["sections"]
                if item["section"] not in ("Window", "Moonroof")
            ]
    if timestamped:
        status["occurrenceDate"] = f"2026-09-15T12:0{step}:00Z"
    return status


def telemetry(step: int) -> dict:
    """Telemetry with mixed pressure units and the app's Integer and String window types."""
    return {
        "lastTimestamp": f"2026-09-15T11:5{step}:00Z",
        "tirePressureTimestamp": f"2026-09-15T11:5{step}:00Z",
        "distanceToEmpty": {"value": 250 - step, "unit": "mi"},
        "flTirePressure": {"value": 240, "unit": "kPa"},
        "frTirePressure": {"value": 35, "unit": "psi"},
        "rlTirePressure": {"value": 2.4, "unit": "bar"},
        "rrTirePressure": {"value": 35, "unit": "psi"},
        "spareTirePressure": {"value": 60, "unit": "psi"},
        "fuelLevel": 80 - step,
        "odometer": {"value": 12345 + step, "unit": "mi"},
        "tripA": {"value": 12.5, "unit": "mi"},
        "tripB": {"value": 99, "unit": "mi"},
        "nextService": {"value": 4000, "unit": "mi"},
        "speed": {"value": 0, "unit": "mph"},
        "driverWindow": 1,
        "passengerWindow": 2,
        "rlWindow": "1",
        "rrWindow": "2",
        "sunRoof": 1,
        "vehicleLocation": {"latitude": 34.0, "longitude": -118.0},
    }


def tire_pressure(step: int, vehicle_vin: str) -> dict:
    """`v1/telemetry/tires/pressure`: newer than telemetry, and one tire goes low on step two."""

    def tire(value, low=False):
        return {"value": value, "unit": "psi", "displayLowTirePressureWarning": low}

    return {
        "vin": vehicle_vin,
        "tirePressureTimestamp": f"2026-09-15T12:0{step}:30Z",
        "flTirePressure": tire(36 - 8 * step, low=bool(step)),
        "frTirePressure": tire(36),
        "rlTirePressure": tire(34),
        "rrTirePressure": tire(34),
        "spareTirePressure": tire(60),
    }


def electric_status(step: int) -> dict:
    """REST electric status: plugged in on the second step, charge time unavailable."""
    return {
        "vehicleInfo": {
            "acquisitionDatetime": f"2026-09-15T12:0{step}:00Z",
            "maxNoOfChargeSchedules": 3,
            "chargeInfo": {
                "plugStatus": 40 if step else 12,
                "evDistance": 30,
                "evDistanceAC": 28,
                "evDistanceUnit": "mi",
                "chargeRemainingAmount": 70 + step,
                "remainingChargeTime": 65535,
                "gasolineTravelableDistance": 300,
                "chargeType": 1,
                "connectorStatus": 5,
            },
            "timerChargeInfo": {"multipleTimerSettings": []},
        }
    }


def engine_status(step: int) -> dict:
    """REST engine status: running, then stopped."""
    return {"status": "0" if step else "1", "timer": 10, "date": "2026-09-15T11:58:00Z"}


def graphql_status(step: int, vehicle_vin: str | None = None, *, windows: bool = True) -> dict:
    """AppSync `getVehicleStatus`, optionally without windows."""
    status = copy.deepcopy(STATUS_24MM)
    status["lastUpdateDateTime"] = f"2026-09-15T12:0{step}:00Z"
    if not windows:
        del status["vehicleState"]["windows"]
    if vehicle_vin is not None:
        status["vin"] = vehicle_vin
    return status


def health_report() -> dict:
    return {"vehicleStatus": {"engOilLevelStatus": "Normal"}}


def health_status() -> dict:
    return {"warning": []}


def service_campaigns() -> list:
    return [{"campaignType": "REC-S", "campaignNumber": "24V123"}]


def software_update() -> dict:
    return {"notificationStatus": 1, "updateAvailable": True, "updateName": "Multimedia"}
