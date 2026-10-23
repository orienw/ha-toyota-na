"""Vehicle value contracts remain stable without the external client package."""

from datetime import UTC, datetime

import pytest

from custom_components.toyota_na.exceptions import AuthError, LoginError, NotLoggedIn, TokenExpired
from custom_components.toyota_na.vehicle_values import (
    ToyotaLocation,
    ToyotaLockableOpening,
    ToyotaNumeric,
    ToyotaOpening,
    ToyotaRemoteStart,
)


def test_opening_defaults_unknown_states_and_inheritance():
    opening = ToyotaOpening()
    lockable = ToyotaLockableOpening()
    assert opening.closed is False
    assert (lockable.closed, lockable.locked) == (False, False)
    assert isinstance(lockable, ToyotaOpening)
    assert not isinstance(opening, ToyotaLockableOpening)

    lockable.closed = None
    lockable.locked = None
    assert (lockable.closed, lockable.locked) == (None, None)
    assert repr(lockable) == "ToyotaLockableOpening(closed=None, locked=None)"
    lockable.closed = True
    lockable.locked = False
    assert (lockable.closed, lockable.locked) == (True, False)
    assert repr(opening) == "ToyotaOpening(closed=False)"


def test_numeric_values_preserve_units_unknowns_and_status_codes():
    numeric = ToyotaNumeric(35.5, "psi")
    assert (numeric.value, numeric.unit) == (35.5, "psi")
    assert repr(numeric) == "ToyotaNumeric(value=35.5, unit=psi)"
    numeric.value = "charging"
    numeric.unit = ""
    assert (numeric.value, numeric.unit) == ("charging", "")
    numeric.value = None
    assert numeric.value is None
    assert numeric != ToyotaNumeric(None, "")


def test_location_preserves_the_longitude_value_property():
    location = ToyotaLocation(lat=34.05, long=-118.25)
    assert (location.lat, location.value) == (34.05, -118.25)
    location.lat = 0
    location.value = 180
    assert (location.lat, location.value) == (0, 180)
    assert repr(location) == "ToyotaLocation(lat=0, long=180)"


def test_remote_start_timing_is_not_clamped_or_gated_by_running_state(freezer):
    freezer.move_to("2026-10-23T12:00:00Z")
    remote = ToyotaRemoteStart(date="2026-10-23T11:55:00Z", on=True, timer=10)
    assert remote.start_time == datetime(2026, 10, 23, 11, 55, tzinfo=UTC)
    assert remote.end_time == datetime(2026, 10, 23, 12, 5, tzinfo=UTC)
    assert remote.time_left == 5

    remote.on = False
    assert remote.on is False
    assert remote.time_left == 5
    remote.timer = 2
    assert remote.end_time == datetime(2026, 10, 23, 11, 57, tzinfo=UTC)
    assert remote.time_left == -3
    assert repr(remote) == (
        "ToyotaRemoteStart(end_time=2026-10-23 11:57:00+00:00, on=False, "
        "start_time=2026-10-23 11:55:00+00:00, time_left=-3.0)"
    )


def test_remote_start_accepts_missing_session_metadata(freezer):
    freezer.move_to("2026-10-23T12:00:00Z")
    remote = ToyotaRemoteStart(date=None, on=True, timer=None)
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


@pytest.mark.parametrize("date", ["invalid", "2026-10-23T12:00:00+00:00"])
def test_remote_start_constructor_preserves_its_date_format(date):
    with pytest.raises(ValueError):
        ToyotaRemoteStart(date=date, on=True, timer=10)


@pytest.mark.parametrize("error_type", [NotLoggedIn, TokenExpired, LoginError])
def test_authentication_errors_keep_their_hierarchy_and_details(error_type):
    error = error_type("Toyota rejected the request")
    assert isinstance(error, AuthError)
    assert error.args == ("Toyota rejected the request",)
    assert str(error) == "Toyota rejected the request"
    assert repr(error) == f"{error_type.__name__}('Toyota rejected the request')"
