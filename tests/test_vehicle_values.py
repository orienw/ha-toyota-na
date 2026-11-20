"""Typed vehicle value models and authentication exception hierarchy."""

from datetime import UTC, datetime

import pytest

from custom_components.toyota_na.exceptions import AuthError, LoginError, NotLoggedIn, TokenExpired
from custom_components.toyota_na.vehicle_values import Location, Measurement, Opening, RemoteStart


def test_opening_defaults_to_unknown_states():
    opening = Opening()
    assert (opening.closed, opening.locked) == (None, None)
    opening = Opening(closed=True, locked=False)
    assert (opening.closed, opening.locked) == (True, False)
    assert opening == Opening(True, False)


def test_measurement_preserves_units_unknowns_and_status_codes():
    numeric = Measurement(35.5, "psi")
    assert (numeric.value, numeric.unit) == (35.5, "psi")
    numeric = Measurement("charging")
    assert (numeric.value, numeric.unit) == ("charging", "")
    assert Measurement(None, "") == Measurement(None)


def test_location_stores_latitude_and_longitude():
    location = Location(latitude=34.05, longitude=-118.25)
    assert (location.latitude, location.longitude) == (34.05, -118.25)
    assert location == Location(34.05, -118.25)


def test_remote_start_timing_is_not_clamped_or_gated_by_running_state(freezer):
    freezer.move_to("2026-10-23T12:00:00Z")
    remote = RemoteStart(
        on=True,
        timer=10,
        start_time=datetime(2026, 10, 23, 11, 55, tzinfo=UTC),
    )
    assert remote.start_time == datetime(2026, 10, 23, 11, 55, tzinfo=UTC)
    assert remote.end_time == datetime(2026, 10, 23, 12, 5, tzinfo=UTC)
    assert remote.time_left == 5

    remote.on = False
    assert remote.on is False
    assert remote.time_left == 5
    remote.timer = 2
    assert remote.end_time == datetime(2026, 10, 23, 11, 57, tzinfo=UTC)
    assert remote.time_left == -3


def test_remote_start_accepts_missing_session_metadata(freezer):
    freezer.move_to("2026-10-23T12:00:00Z")
    remote = RemoteStart(on=True, timer=None)
    assert remote.start_time is None
    assert remote.end_time is None
    assert remote.time_left is None
    remote.start_time = datetime(2026, 10, 23, 11, 55, tzinfo=UTC)
    assert remote.end_time is None
    remote.timer = 7.5
    assert remote.end_time == datetime(2026, 10, 23, 12, 2, 30, tzinfo=UTC)
    assert remote.time_left == 2.5
    remote.start_time = None
    assert remote.end_time is None
    assert remote.time_left is None


@pytest.mark.parametrize("error_type", [NotLoggedIn, TokenExpired, LoginError])
def test_authentication_errors_keep_their_hierarchy_and_details(error_type):
    error = error_type("Toyota rejected the request")
    assert isinstance(error, AuthError)
    assert error.args == ("Toyota rejected the request",)
    assert str(error) == "Toyota rejected the request"
    assert repr(error) == f"{error_type.__name__}('Toyota rejected the request')"
