"""Charging choices reported by the vehicle."""

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
