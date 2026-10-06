"""Toyota's Health tab, derived from vehicle health responses as the app does."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .vehicle_helpers import app_flag, app_string

_RECALL_TYPES = ("rec-s", "safety recalls")
_CAMPAIGN_TYPES = ("lsc", "ssc", "service campaigns")


def _text(value: Any) -> str:
    return app_string(value) or ""


def _items(value: Any) -> list[Mapping]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _report(health: Mapping) -> Mapping:
    report = health.get("report")
    return report if isinstance(report, Mapping) else {}


def _vehicle_status(health: Mapping) -> Mapping:
    status = _report(health).get("vehicleStatus")
    return status if isinstance(status, Mapping) else {}


def health_reading(vehicle, features, read):
    """Return a Health tab reading when Toyota's app would show it."""
    if vehicle is None or (features and not any(vehicle.feature_enabled(name) for name in features)):
        return None
    return read(vehicle.health)


def _merge(entries, campaigns, types, *, require_text=False) -> list[dict]:
    """Add service campaign items, replacing report entries with the same campaign number."""
    for item in _items(campaigns):
        if _text(item.get("campaignType")).lower() not in types:
            continue
        number = app_string(item.get("campaignNumber"))
        entries = [(reference, entry) for reference, entry in entries if reference != number]
        title, description = _text(item.get("campaignTitle")), _text(item.get("description"))
        if not require_text or (title and description):
            entries.append(("", {
                "title": title,
                "description": description,
                "remedy": _text(item.get("remedyDescription")),
                "date": _text(item.get("recallDate")) or _text(item.get("campaignDate")),
            }))
    return [entry for _, entry in entries]


def safety_recalls(health: Mapping) -> list[dict] | None:
    """Recalls as the app's Safety Recalls tile lists them."""
    if "report" not in health and "campaigns" not in health:
        return None
    report = _report(health)
    entries = [
        (_text(item.get("dealerReferenceID")), {
            "title": _text(item.get("title")),
            "description": _text(item.get("description")),
            "remedy": _text(item.get("remedy")),
            "date": _text(item.get("nhtsarecallDate")),
        })
        for item in _items(report.get("safetyRecallsList"))
    ] if app_flag(report.get("recallsListExists")) else []
    return _merge(entries, health.get("campaigns"), _RECALL_TYPES)


def service_campaigns(health: Mapping) -> list[dict] | None:
    """Campaigns as the app's Service Campaigns tile lists them."""
    if "report" not in health and "campaigns" not in health:
        return None
    report = _report(health)
    entries = [
        (_text(item.get("dealerRefID")), {
            "title": _text(item.get("title")),
            "description": _text(item.get("activityDesc")),
            "remedy": _text(item.get("remedyDesc")),
            "date": _text(item.get("campaignDate")),
        })
        for item in _items(report.get("serviceCampaigns"))
    ] if app_flag(report.get("campaignsExists")) else []
    return _merge(entries, health.get("campaigns"), _CAMPAIGN_TYPES, require_text=True)


def vehicle_alerts(health: Mapping) -> list[dict] | None:
    """Warning lights from the report, plus diagnostic warnings it doesn't already list."""
    if "report" not in health and "status" not in health:
        return None
    alerts = []
    for item in _items(_report(health).get("vehicleAlertList")):
        title, description = _text(item.get("wngname")), _text(item.get("wngdesc"))
        if title and description:
            alerts.append({"title": title, "description": description})
    status = health.get("status")
    for item in _items(status.get("warning") if isinstance(status, Mapping) else None):
        title, description = _text(item.get("wngdesc")), _text(item.get("wngownersManual"))
        if title and description and all(alert["title"] != title for alert in alerts):
            alerts.append({"title": title, "description": description})
    return alerts


def engine_oil_low(health: Mapping) -> bool | None:
    status = app_string(_vehicle_status(health).get("engOilLevelStatus"))
    return "low" in status.lower() if status is not None else None


def key_fob_battery_low(health: Mapping) -> bool | None:
    status = _vehicle_status(health)
    if status.get("smartKeyBatteryTitle") is None:
        return None
    # Unlike the oil check, the app matches "low" case-sensitively here.
    return "low" in _text(status.get("smartKeyBatteryDesc"))


def maintenance_required(health: Mapping) -> bool | None:
    information = _report(health).get("maintenanceInformation")
    return app_flag(information.get("maintenanceRequired")) if isinstance(information, Mapping) else None


def service_due(health: Mapping) -> dict | None:
    information = _report(health).get("maintenanceInformation")
    return {"service_due": app_string(information.get("serviceDue"))} if isinstance(information, Mapping) else None
