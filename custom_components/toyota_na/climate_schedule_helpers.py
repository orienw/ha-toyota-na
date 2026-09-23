"""Convert Toyota reservations between UTC and Home Assistant's timezone."""

from copy import deepcopy
from datetime import date, datetime, time, timezone
import math

from .charging_helpers import WEEKDAYS, schedule_time
from .climate_helpers import climate_bounds

SCHEDULE_FIELDS = (
    "status", "reservationType", "date", "days", "time", "settingType",
    "temperatureUnit", "temperature", "acOptions", "ventilationOptions",
)


def _reservation_datetime(schedule, selected_date=None):
    selected_date = selected_date or schedule.get("date")
    return datetime.strptime(f"{selected_date} {schedule.get('time')}", "%m-%d-%Y %H:%M").replace(tzinfo=timezone.utc)


def _shift_days(days, offset):
    return [WEEKDAYS[(WEEKDAYS.index(day) + offset) % 7] for day in days]


def _current_offset(zone, now=None):
    # Repeating reservations are a UTC time and weekdays, so they convert with
    # the offset in effect now. A fixed offset keeps display and edits exact
    # inverses and has no clock-change gaps.
    return timezone((now or datetime.now(zone)).astimezone(zone).utcoffset())


def local_climate_schedule(schedule, zone, *, now=None):
    result = deepcopy(schedule)
    result["time_zone"] = str(zone)
    repeating = schedule.get("reservationType") == "REPETITION"
    try:
        utc = _reservation_datetime(schedule, "01-01-2000" if repeating else None)
        local = utc.astimezone(_current_offset(zone, now) if repeating else zone)
        result["date"] = None if repeating else local.date().isoformat()
        result["time"] = local.strftime("%H:%M")
        result["days"] = _shift_days(schedule.get("days") or [], (local.date() - utc.date()).days)
    except (TypeError, ValueError):
        result.update(date=None, time=None, days=None)
    return result


def build_climate_schedule(settings, existing, changes, zone, *, now=None):
    if changes.keys() - {"enabled", "time", "date", "days", "temperature"}:
        raise ValueError("Unknown climate schedule setting.")
    if "date" in changes and "days" in changes:
        raise ValueError("Choose a date or repeating days, not both.")
    now = now or datetime.now(zone)
    body = {key: deepcopy(existing[key]) for key in SCHEDULE_FIELDS if existing.get(key) is not None} if existing else {}
    if not existing:
        if not changes.get("time") or "temperature" not in changes or not (changes.get("date") or changes.get("days")):
            raise ValueError("A new climate schedule needs a time, temperature, and date or repeating days.")
        if changes.get("enabled") is False:
            raise ValueError("Create the climate schedule before disabling it.")
        body["settingType"] = "CUSTOM"
    if "enabled" in changes:
        if not isinstance(changes["enabled"], bool):
            raise ValueError("Schedule enabled must be true or false.")
        if existing:
            body["status"] = "active" if changes["enabled"] else "inactive"
    if not existing or changes.keys() & {"time", "date", "days"}:
        local = local_climate_schedule(existing, zone, now=now) if existing else {}
        try:
            selected_time = schedule_time(changes.get("time", local.get("time")))
        except ValueError as err:
            if "time" in changes:
                raise ValueError("Climate schedule time must use HH:MM in Home Assistant's timezone.") from err
            raise RuntimeError("Toyota did not return a valid climate schedule time.") from err
        if selected_time is None:
            if "time" in changes:
                raise ValueError("Choose a climate schedule time.")
            raise RuntimeError("Toyota did not return a valid climate schedule time.")
        if "date" in changes:
            body["reservationType"] = "ONE_TIME"
            selected_date = changes["date"]
            days = []
        elif "days" in changes:
            body["reservationType"] = "REPETITION"
            selected_date = now.date().isoformat()
            days = changes["days"]
        else:
            selected_date = now.date().isoformat() if body.get("reservationType") == "REPETITION" else local.get("date")
            days = local.get("days")
        if body.get("reservationType") not in ("ONE_TIME", "REPETITION"):
            raise RuntimeError("Toyota did not return a valid climate reservation type.")
        conversion = _current_offset(zone, now) if body["reservationType"] == "REPETITION" else zone
        try:
            local_time = datetime.combine(date.fromisoformat(selected_date), time.fromisoformat(selected_time), conversion)
        except (TypeError, ValueError) as err:
            if "date" in changes:
                raise ValueError("Climate schedule date must use YYYY-MM-DD.") from err
            raise RuntimeError("Toyota did not return a valid climate schedule date.") from err
        utc = local_time.astimezone(timezone.utc)
        if utc.astimezone(conversion).replace(tzinfo=None) != local_time.replace(tzinfo=None):
            raise ValueError("This local time does not exist because the clocks move forward. Choose another time.")
        body["date"] = utc.strftime("%m-%d-%Y")
        body["time"] = utc.strftime("%H:%M")
        if body["reservationType"] == "REPETITION":
            if not isinstance(days, list) or not days or any(day not in WEEKDAYS for day in days):
                if "days" not in changes:
                    raise RuntimeError("Toyota did not return valid climate schedule days.")
                raise ValueError("Choose at least one valid day of the week.")
            body["days"] = _shift_days(list(dict.fromkeys(days)), (utc.date() - local_time.date()).days)
        else:
            body.pop("days", None)
    if "temperature" in changes:
        bounds = climate_bounds(settings, "temperature")
        unit = str(settings.get("temperatureUnit", "")).lower()
        if bounds is None or unit not in ("c", "f"):
            raise RuntimeError("Toyota did not provide a valid climate schedule temperature range.")
        value = changes["temperature"]
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("Climate temperature must be a finite number.")
        minimum, maximum, step = bounds
        if not minimum <= value <= maximum or not math.isclose((value - minimum) / step, round((value - minimum) / step), abs_tol=1e-6):
            raise ValueError("Climate temperature does not match the vehicle's range and step.")
        body.update(temperature=format(value, "g"), temperatureUnit=unit, settingType="CUSTOM")
    elif body.get("temperature") is not None:
        body["temperature"] = str(body["temperature"])
    if body.get("status", "active") == "active" and body.get("reservationType") == "ONE_TIME":
        try:
            scheduled = _reservation_datetime(body)
        except ValueError as err:
            raise RuntimeError("Toyota did not return a valid climate schedule date and time.") from err
        if scheduled <= now:
            raise ValueError("A one-time climate schedule must be in the future.")
    return body


def climate_schedule_matches(schedule, desired):
    try:
        if desired.get("reservationType") == "REPETITION":
            if _reservation_datetime(schedule, "01-01-2000").time() != _reservation_datetime(desired, "01-01-2000").time():
                return False
        elif _reservation_datetime(schedule) != _reservation_datetime(desired):
            return False
        if schedule.get("status") != desired.get("status", "active"):
            return False
        for key, value in desired.items():
            if key in ("date", "time", "status"):
                continue
            if key == "days":
                if set(schedule.get(key) or []) != set(value):
                    return False
            elif key == "temperature":
                if float(schedule.get(key)) != float(value):
                    return False
            elif key == "temperatureUnit":
                if str(schedule.get(key)).lower() != str(value).lower():
                    return False
            elif isinstance(value, dict):
                reported = schedule.get(key) or {}
                if not isinstance(reported, dict) or any(reported.get(name) != setting for name, setting in value.items()):
                    return False
            elif schedule.get(key) != value:
                return False
        return True
    except (TypeError, ValueError):
        return False
