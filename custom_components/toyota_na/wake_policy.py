"""Vehicle wake policy helpers."""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

CONF_WAKE_INTERVAL = "automatic_wake_interval"
LAST_WAKE_AT = "last_refreshed_at"

WAKE_INTERVAL_OPTIONS = {
    0: "Cloud updates only",
    2 * 3600: "Every 2 hours",
    6 * 3600: "Every 6 hours",
    12 * 3600: "Every 12 hours",
    24 * 3600: "Every 24 hours",
}


def automatic_wake_interval(
    options: Mapping[str, Any], default_interval: int
) -> int:
    """Return a valid configured wake interval in seconds."""
    interval = options.get(CONF_WAKE_INTERVAL, default_interval)
    if type(interval) is not int or interval < 0:
        return default_interval
    return interval


def automatic_wake_due(
    entry_data: Mapping[str, Any],
    options: Mapping[str, Any],
    default_interval: int,
    *,
    now: float | None = None,
) -> bool:
    """Return whether Home Assistant should request a vehicle wake."""
    interval = automatic_wake_interval(options, default_interval)
    if interval == 0:
        return False

    last_wake_at = entry_data.get(LAST_WAKE_AT)
    if not isinstance(last_wake_at, (int, float)) or isinstance(last_wake_at, bool):
        return True

    current_time = time.time() if now is None else now
    return last_wake_at <= current_time - interval


def record_vehicle_wake(hass: Any, entry: Any, *, now: float | None = None) -> None:
    """Persist when a vehicle wake was last requested."""
    entry_data = dict(entry.data)
    entry_data[LAST_WAKE_AT] = time.time() if now is None else now
    hass.config_entries.async_update_entry(entry, data=entry_data)
