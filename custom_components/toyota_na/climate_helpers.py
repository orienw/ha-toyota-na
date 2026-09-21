"""Vehicle-provided climate preferences and supported options."""

from copy import deepcopy
import math

AIRFLOWS = {
    "upperBody": "Upper body",
    "feet": "Feet",
    "upperBodyFeet": "Upper body and feet",
    "frontDefrostFeet": "Windshield and feet",
}
SEATS = {
    "frontDriver": "ventFrontDriver",
    "frontPassenger": "ventFrontPassenger",
    "rearDriver": "ventRearDriver",
    "rearPassenger": "ventRearPassenger",
}


def climate_parameters(settings: dict, category: str) -> dict:
    for operation in settings.get("acOperations") or []:
        if (
            isinstance(operation, dict)
            and operation.get("categoryName") == category
            and operation.get("available") is True
        ):
            return {
                parameter["name"]: parameter
                for parameter in operation.get("acParameters") or []
                if isinstance(parameter, dict) and parameter.get("available") is True
                and isinstance(parameter.get("enabled"), bool)
                and isinstance(parameter.get("name"), str)
            }
    return {}


def climate_bounds(settings: dict, key: str) -> tuple | None:
    if key == "temperature":
        values = (
            settings.get("minTemp"), settings.get("maxTemp"), settings.get("tempInterval"),
        )
    else:
        minimum, maximum = settings.get("minAirFlow"), settings.get("maxAirFlow")
        values = (1 if minimum is None else minimum, 7 if maximum is None else maximum, 1)
    if all(type(value) in (int, float) and math.isfinite(value) for value in values):
        minimum, maximum, step = values
        if key == "airFlowVolume" and (minimum % 1 or maximum % 1):
            return None
        if minimum <= maximum and step > 0:
            return values
    return None


def seat_modes(settings: dict, position: str) -> list[str]:
    modes = ["Off"]
    if position in climate_parameters(settings, "seatHeat"):
        modes.append("Heat")
    if SEATS[position] in climate_parameters(settings, "seatVent"):
        modes.append("Ventilate")
    return modes


def apply_climate_changes(settings: dict, changes: dict) -> dict:
    settings = deepcopy(settings)
    for key, value in changes.items():
        if key in ("temperature", "airFlowVolume"):
            bounds = climate_bounds(settings, key)
            if (
                bounds is None or type(settings.get(key)) not in (int, float)
                or type(value) not in (int, float) or not math.isfinite(value)
            ):
                raise ValueError("Toyota did not provide a valid climate range.")
            minimum, maximum, step = bounds
            if not minimum <= value <= maximum or not math.isclose(
                (value - minimum) / step, round((value - minimum) / step), abs_tol=1e-6,
            ):
                raise ValueError("Climate value does not match the vehicle's range and step.")
            settings[key] = int(value) if key == "airFlowVolume" else value
        elif key == "settingsOn":
            if not isinstance(value, bool):
                raise ValueError("Climate settings must be enabled or disabled.")
            settings[key] = value
        elif key == "extendedRuntime":
            runtime = settings.get(key) or {}
            if runtime.get("available") is not True or not isinstance(value, bool):
                raise ValueError("Longer climate runtime is unavailable for this vehicle.")
            runtime["enabled"] = value
        elif key == "parameter":
            category, name, enabled = value
            parameter = climate_parameters(settings, category).get(name)
            if parameter is None or not isinstance(enabled, bool):
                raise ValueError("This climate preference is unavailable for this vehicle.")
            parameter["enabled"] = enabled
        elif key == "airflow":
            parameters = climate_parameters(settings, "airflow")
            if value not in AIRFLOWS or value not in parameters:
                raise ValueError("This airflow direction is unavailable for this vehicle.")
            for name, parameter in parameters.items():
                parameter["enabled"] = name == value
        elif key == "seat":
            position, mode = value
            modes = seat_modes(settings, position) if position in SEATS else []
            if len(modes) < 2 or mode not in modes:
                raise ValueError("This seat climate setting is unavailable for this vehicle.")
            for category, name, selected in (
                ("seatHeat", position, mode == "Heat"),
                ("seatVent", SEATS[position], mode == "Ventilate"),
            ):
                parameter = climate_parameters(settings, category).get(name)
                if parameter is not None:
                    parameter["enabled"] = selected
        else:
            raise ValueError("Unknown climate preference.")
    return settings
