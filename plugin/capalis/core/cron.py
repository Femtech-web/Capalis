"""Five-field cron (minute hour day-of-month month day-of-week), evaluated in UTC.

Supports ``*``, numbers, ranges ``a-b``, lists ``a,b`` and steps ``*/n`` or ``a-b/n``. When both the day of
month and the day of week are restricted, either may match (the usual cron rule).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

_BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class CronError(ValueError):
    pass


def _field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise CronError(f"bad step in {part!r}")
        if body == "*":
            start, end = low, high
        elif "-" in body:
            a, b = body.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(body)
        if not low <= start <= end <= high:
            raise CronError(f"{part!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class Cron:
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]
    any_day: bool
    any_weekday: bool

    @classmethod
    def parse(cls, expression: str) -> Cron:
        fields = expression.split()
        if len(fields) != 5:
            raise CronError("a schedule has five fields: minute hour day month weekday")
        try:
            parsed = [_field(f, lo, hi) for f, (lo, hi) in zip(fields, _BOUNDS, strict=True)]
        except ValueError as exc:
            raise CronError(str(exc)) from exc
        weekdays = frozenset(0 if d == 7 else d for d in parsed[4])
        return cls(parsed[0], parsed[1], parsed[2], parsed[3], weekdays, fields[2] == "*", fields[4] == "*")

    def matches(self, epoch: int) -> bool:
        t = time.gmtime(epoch)
        weekday = (t.tm_wday + 1) % 7  # cron counts Sunday as 0; time counts Monday as 0
        if t.tm_min not in self.minutes or t.tm_hour not in self.hours or t.tm_mon not in self.months:
            return False
        day_ok, weekday_ok = t.tm_mday in self.days, weekday in self.weekdays
        if self.any_day or self.any_weekday:
            return day_ok and weekday_ok
        return day_ok or weekday_ok

    def latest_between(self, after: int, until: int) -> int | None:
        """The latest matching minute in (after, until], or None. Only the latest missed slot matters:
        a schedule that missed several slots while Hermes was off fires once, not once per slot."""
        minute = until - until % 60
        floor = max(after - after % 60 + 60, until - 366 * 86400)
        while minute >= floor:
            if self.matches(minute):
                return minute
            minute -= 60
        return None


def iso_minute(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(epoch))

