from __future__ import annotations

import calendar

import pytest

from capalis.core.cron import Cron, CronError, iso_minute


def at(text: str) -> int:
    year, month, day, hour, minute = (int(x) for x in text.replace("T", "-").replace(":", "-").split("-"))
    return calendar.timegm((year, month, day, hour, minute, 0, 0, 0, 0))


def test_fields_ranges_lists_and_steps() -> None:
    every_ten = Cron.parse("*/10 * * * *")
    assert every_ten.matches(at("2026-11-10T12:20")) and not every_ten.matches(at("2026-11-10T12:21"))
    weekdays = Cron.parse("0 9 * * 1-5")
    assert weekdays.matches(at("2026-11-10T09:00"))          # a Tuesday
    assert not weekdays.matches(at("2026-11-15T09:00"))      # a Sunday
    assert Cron.parse("30 8,17 * * *").matches(at("2026-11-10T17:30"))
    assert Cron.parse("0 0 * * 7").matches(at("2026-11-15T00:00")), "7 is Sunday too"


def test_day_of_month_or_weekday_when_both_are_set() -> None:
    either = Cron.parse("0 12 1 * 1")
    assert either.matches(at("2026-12-01T12:00")) and either.matches(at("2026-11-16T12:00"))


def test_latest_missed_slot_fires_once() -> None:
    cron = Cron.parse("*/10 * * * *")
    assert cron.latest_between(at("2026-11-10T12:00"), at("2026-11-10T12:47")) == at("2026-11-10T12:40")
    assert cron.latest_between(at("2026-11-10T12:40"), at("2026-11-10T12:45")) is None
    assert iso_minute(at("2026-11-10T12:40")) == "2026-11-10T12:40Z"


@pytest.mark.parametrize("bad", ["* * * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"])
def test_bad_expressions_are_rejected(bad: str) -> None:
    with pytest.raises(CronError):
        Cron.parse(bad)
