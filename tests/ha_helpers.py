"""Home Assistant's test helpers, from whichever package this Home Assistant ships them in."""

try:
    from pytest_homeassistant.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
    )
    from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
    from pytest_homeassistant.syrupy import HomeAssistantSnapshotExtension
except ImportError:  # Home Assistant before 2026.11
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_capture_events,
        async_fire_time_changed,
    )
    from pytest_homeassistant_custom_component.components.diagnostics import (
        get_diagnostics_for_config_entry,
    )
    from pytest_homeassistant_custom_component.syrupy import HomeAssistantSnapshotExtension

__all__ = [
    "HomeAssistantSnapshotExtension",
    "MockConfigEntry",
    "async_capture_events",
    "async_fire_time_changed",
    "get_diagnostics_for_config_entry",
]
