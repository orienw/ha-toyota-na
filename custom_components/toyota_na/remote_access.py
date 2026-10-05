"""Repairs for remote access states that need action in Toyota's app."""

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

# Toyota's remoteDisplay values that need the owner's attention, by repair.
REMOTE_ACCESS_ISSUES = {
    1: "remote_authorization_required",
    2: "remote_subscription_cancelled",
    3: "remote_subscription_cancelled",
    4: "remote_activation_failed",
    5: "remote_activation_pending",
    6: "remote_activation_error",
    8: "remote_subscription_expired",
    9: "remote_subscription_expired",
    10: "vehicle_stolen",
    11: "vehicle_stolen_immobilizer",
}


def sync_remote_access_issues(hass, entry, vehicles) -> None:
    """Show a repair for each vehicle whose remote access needs action, clearing the rest."""
    prefix = f"remote_access_{entry.entry_id}_"
    current = set()
    for vehicle in vehicles:
        translation_key = REMOTE_ACCESS_ISSUES.get(vehicle.remote_display)
        if translation_key is None:
            continue
        # A new state gets a new issue, so ignoring one state doesn't hide the next.
        issue_id = f"{prefix}{vehicle.vin}_{translation_key}"
        current.add(issue_id)
        ir.async_create_issue(
            hass,
            DOMAIN,
            issue_id,
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=translation_key,
            translation_placeholders={
                "vehicle": f"{vehicle.model_year} {vehicle.model_name}".strip(),
            },
        )
    for domain, issue_id in list(ir.async_get(hass).issues):
        if domain == DOMAIN and issue_id.startswith(prefix) and issue_id not in current:
            ir.async_delete_issue(hass, DOMAIN, issue_id)
