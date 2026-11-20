"""Typed values reported by Toyota's vehicle endpoints."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class Opening:
    """An opening (door, window, trunk, hood, moonroof, warning, or charging flag)."""

    closed: bool | None = None
    locked: bool | None = None


@dataclass
class Measurement:
    """A reported reading and its wire unit, without coercing either field."""

    value: Any
    unit: str = ""


@dataclass
class Location:
    """A geographic coordinate pair reported by the vehicle."""

    latitude: float
    longitude: float


@dataclass
class RemoteStart:
    """Reported engine state and optional remote-start session timing."""

    on: bool
    timer: float | None = None
    start_time: datetime | None = None

    @property
    def end_time(self) -> datetime | None:
        if self.start_time is not None and self.timer is not None:
            return self.start_time + timedelta(minutes=self.timer)
        return None

    @property
    def time_left(self) -> float | None:
        end_time = self.end_time
        if end_time is not None:
            return (end_time - datetime.now(UTC)).total_seconds() / 60
        return None


VehicleValue = Opening | Measurement | Location | RemoteStart
