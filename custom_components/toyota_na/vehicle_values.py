"""Mutable values reported by Toyota's vehicle endpoints."""

from datetime import UTC, datetime, timedelta


class ToyotaOpening:
    """A reported opening, including an unknown position."""

    def __init__(self, closed=False):
        self._closed = closed

    @property
    def closed(self):
        return self._closed

    @closed.setter
    def closed(self, value):
        self._closed = value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(closed={self._closed})"


class ToyotaLockableOpening(ToyotaOpening):
    """An opening whose position and lock state are reported separately."""

    def __init__(self, closed: bool = False, locked: bool = False):
        super().__init__(closed)
        self._locked = locked

    @property
    def locked(self):
        return self._locked

    @locked.setter
    def locked(self, value):
        self._locked = value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(closed={self._closed}, locked={self._locked})"


class ToyotaNumeric:
    """A reported value and its wire unit, without coercing either field."""

    def __init__(self, value: float, unit: str):
        self._value = value
        self._unit = unit

    @property
    def value(self) -> float:
        return self._value

    @value.setter
    def value(self, value: float):
        self._value = value

    @property
    def unit(self) -> str:
        return self._unit

    @unit.setter
    def unit(self, unit: str):
        self._unit = unit

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(value={self._value}, unit={self._unit})"


class ToyotaLocation:
    """A position whose longitude is exposed through the value property."""

    def __init__(self, lat: float, long: float):
        self._lat = lat
        self._long = long

    @property
    def lat(self) -> float:
        return self._lat

    @lat.setter
    def lat(self, value: float) -> None:
        self._lat = value

    @property
    def value(self) -> float:
        return self._long

    @value.setter
    def value(self, value: float) -> None:
        self._long = value

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(lat={self._lat}, long={self._long})"


class ToyotaRemoteStart:
    """Reported engine state and optional remote-start session timing."""

    def __init__(self, date: str | None, on: bool, timer: float | None):
        self._start_time = (
            datetime.strptime(str(date), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            if date is not None
            else None
        )
        self._on = on
        self._timer = timer

    @property
    def end_time(self) -> datetime | None:
        if self._start_time is not None and self._timer is not None:
            return self._start_time + timedelta(minutes=self._timer)
        return None

    @property
    def on(self) -> bool:
        return self._on

    @on.setter
    def on(self, value: bool) -> None:
        self._on = value

    @property
    def start_time(self) -> datetime | None:
        return self._start_time

    @start_time.setter
    def start_time(self, value: datetime | None):
        self._start_time = value

    @property
    def time_left(self) -> float | None:
        end_time = self.end_time
        if end_time is not None:
            return (end_time - datetime.now(UTC)).total_seconds() / 60
        return None

    @property
    def timer(self) -> float | None:
        return self._timer

    @timer.setter
    def timer(self, value: float):
        self._timer = value

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(end_time={self.end_time}, on={self._on}, "
            f"start_time={self.start_time}, time_left={self.time_left})"
        )
