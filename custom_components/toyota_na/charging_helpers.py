"""Charging choices reported by the vehicle."""

from datetime import time

WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

CHARGE_SETTINGS = {
    "targetLimit": ("Charge Limit", "limitSelectionValues", "%", "chargingTargetLimit"),
    "maxACCurrent": ("AC Charging Current", "acCurrentSelections", "A", "currentCharge"),
    "maxDCPower": ("DC Charging Power", "dcPowerSelections", "kW", "quickChargePowerLimit"),
    "electricSupplyModeLimit": (
        "Power Supply Battery Limit", "electricSupplyLimitSelections", "%", "minimumElectricSupply",
    ),
}


def charge_options(settings, field):
    """Map displayed choices to the values accepted by Toyota."""
    _, choices_key, suffix, _ = CHARGE_SETTINGS[field]
    if not isinstance(settings.get(field), dict):
        return {}
    choices = settings.get(choices_key) or []
    if field == "targetLimit":
        choices = choices or [str(value) for value in range(100, 39, -10)]
    else:
        choices = [choice.get("key") for choice in choices
                   if isinstance(choice, dict) and choice.get("enabled") is True]
    result = {}
    for choice in choices:
        if not isinstance(choice, str):
            continue
        clean = choice.strip()
        if clean.lower() == "max" and field in ("maxACCurrent", "maxDCPower"):
            result["Max"] = 127 if field == "maxACCurrent" else 1
            continue
        if clean.lower().endswith(suffix.lower()):
            clean = clean[:-len(suffix)].strip()
        if clean.lower() == "full" and field == "targetLimit":
            clean = "100"
        try:
            value = int(clean)
        except ValueError:
            continue
        if value < 0 or (suffix == "%" and value > 100):
            continue
        label = f"{value}{suffix}" if suffix == "%" else f"{value} {suffix}"
        result[label] = value
    return result


def current_charge_option(settings, field):
    options = charge_options(settings, field)
    measurement = settings.get(field) or {}
    current = str(measurement.get("value", "")).strip()
    if current.lower() == "max" and "Max" in options:
        return "Max"
    if current.lower() == "full" and field == "targetLimit":
        current = "100"
    suffix = CHARGE_SETTINGS[field][2]
    if current.lower().endswith(suffix.lower()):
        current = current[:-len(suffix)].strip()
    try:
        value = int(current)
    except ValueError:
        return None
    return next((label for label, wire_value in options.items() if wire_value == value), None)


def schedule_identifier(value):
    try:
        identifier = int(value)
        if isinstance(value, bool) or identifier < 0 or float(value) != identifier:
            raise ValueError
    except (TypeError, ValueError, OverflowError) as err:
        raise ValueError("Schedule ID must be a nonnegative integer.") from err
    return identifier


def schedule_time(value):
    if value is None:
        return None
    try:
        parsed = time.fromisoformat(value)
        if parsed.second or parsed.microsecond or parsed.tzinfo:
            raise ValueError
    except (TypeError, ValueError) as err:
        raise ValueError("Schedule times must use HH:MM in the vehicle's local time.") from err
    return parsed.isoformat(timespec="minutes")


def build_charge_schedule(schedules, identifier=None, **changes):
    """Change one schedule without sending read-only response fields."""
    fields = ("enabled", "startTime", "endTime", "daysOfTheWeek")
    if changes.keys() - set(fields):
        raise ValueError("Unknown charge schedule setting.")
    if identifier is None:
        body = {"enabled": True, **changes}
        if not all(body.get(key) for key in ("startTime", "endTime", "daysOfTheWeek")):
            raise ValueError("A new schedule needs start time, end time, and days of the week.")
    else:
        identifier = schedule_identifier(identifier)
        existing = next((item for item in schedules if isinstance(item, dict)
                         and str(item.get("settingId")) == str(identifier)), None)
        if existing is None:
            raise ValueError("This charge schedule no longer exists.")
        body = {key: existing.get(key) for key in fields}
        body.update(changes)
        body["settingId"] = identifier
    if not isinstance(body.get("enabled"), bool):
        raise ValueError("Schedule enabled must be true or false.")
    for key in ("startTime", "endTime"):
        body[key] = schedule_time(body.get(key))
    days = body.get("daysOfTheWeek")
    if not isinstance(days, list) or not days or any(day not in WEEKDAYS for day in days):
        raise ValueError("Choose at least one valid day of the week.")
    body["daysOfTheWeek"] = list(dict.fromkeys(days))
    return body


def schedule_matches(schedule, desired):
    if not isinstance(schedule, dict):
        return False
    try:
        return (
            schedule.get("enabled") == desired["enabled"]
            and schedule_time(schedule.get("startTime")) == desired["startTime"]
            and schedule_time(schedule.get("endTime")) == desired["endTime"]
            and set(schedule.get("daysOfTheWeek") or []) == set(desired["daysOfTheWeek"])
        )
    except ValueError:
        return False
