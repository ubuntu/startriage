"""Tests for startriage.dates - date range parsing and formatting."""

from __future__ import annotations

import datetime

import pytest

from startriage.dates import (
    compact_date_range,
    parse_interval,
    reverse_triage_task_day,
    triage_task_date_range,
)


def _d(s: str) -> datetime.date:
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def _dt_start(s: str) -> datetime.datetime:
    """Midnight UTC on the given date -- the expected start of an interval."""
    return datetime.datetime.combine(_d(s), datetime.time.min, tzinfo=datetime.timezone.utc)


def _dt_end(s: str) -> datetime.datetime:
    """Last microsecond of the given date UTC -- the expected end of an interval."""
    return datetime.datetime.combine(_d(s), datetime.time.max, tzinfo=datetime.timezone.utc)


# --- reverse_auto_date_range ---


@pytest.mark.parametrize(
    "start,end,expected",
    [
        ("2019-05-10", "2019-05-12", "Monday triage"),
        ("2019-05-06", "2019-05-06", "Tuesday triage"),
        ("2019-05-07", "2019-05-07", "Wednesday triage"),
        ("2021-03-17", "2021-03-17", "Thursday triage"),
        ("2021-04-15", "2021-04-15", "Friday triage"),
        ("2019-05-06", "2019-05-07", None),  # two days apart, not Fri-Sun
        ("2019-05-07", "2019-05-06", None),  # reversed range
        ("2019-05-06", "2019-05-09", None),  # more than two days, not Fri-Sun
        ("2021-04-16", "2021-04-16", None),  # Saturday
        ("2019-05-18", "2019-05-18", None),  # Saturday
        ("2021-04-18", "2021-04-18", None),  # Sunday
    ],
)
def test_reverse_auto_date_range(start, end, expected):
    assert reverse_triage_task_day(_d(start), _d(end)) == expected


# --- triage_task_date_range ---


def test_triage_task_date_range_none_monday():
    # No keyword on a Monday: should give the Fri-Sun range
    ref = datetime.date(2026, 4, 20)  # Monday
    start, end = triage_task_date_range(None, today=ref)
    assert start == _dt_start("2026-04-17")  # Friday
    assert end == _dt_end("2026-04-19")  # Sunday


def test_triage_task_date_range_none_tuesday():
    # No keyword on a Tuesday: should give yesterday (Monday)
    ref = datetime.date(2026, 4, 21)  # Tuesday
    start, end = triage_task_date_range(None, today=ref)
    assert start == _dt_start("2026-04-20")
    assert end == _dt_end("2026-04-20")


def test_triage_task_date_range_none_weekend_raises():
    ref = datetime.date(2026, 4, 18)  # Saturday
    with pytest.raises(ValueError, match="weekend"):
        triage_task_date_range(None, today=ref)


# --- parse_interval ---


def test_parse_interval_none_weekday():
    # Yesterday was a weekday: single day
    ref = datetime.date(2026, 4, 16)  # Thursday
    start, end = parse_interval(None, relative_to=ref)
    assert start == _dt_start("2026-04-15")
    assert end == _dt_end("2026-04-15")


def test_parse_interval_none_monday():
    # Yesterday was Sunday: return full weekend Fri-Sun
    ref = datetime.date(2026, 4, 20)  # Monday (yesterday=Sunday)
    start, end = parse_interval(None, relative_to=ref)
    assert start == _dt_start("2026-04-17")  # Friday
    assert end == _dt_end("2026-04-19")  # Sunday


def test_parse_interval_single_date():
    start, end = parse_interval("2026-04-09")
    assert start == _dt_start("2026-04-09")
    assert end == _dt_end("2026-04-09")


def test_parse_interval_range():
    start, end = parse_interval("2026-04-09:2026-04-11")
    assert start == _dt_start("2026-04-09")
    assert end == _dt_end("2026-04-11")


def test_parse_interval_range_same():
    start, end = parse_interval("2026-12-09:2026-12-09")
    assert start == _dt_start("2026-12-09")
    assert end == _dt_end("2026-12-09")


def test_parse_interval_day_name():
    # "tuesday" relative to a thursday means the most recent past Tuesday
    ref = datetime.date(2026, 4, 16)  # Thursday
    start, end = parse_interval("tuesday", relative_to=ref)
    assert start == _dt_start("2026-04-14")  # most recent Tuesday
    assert end == _dt_end("2026-04-14")


def test_parse_interval_day_name_monday():
    # "monday" relative to a thursday means most recent past Monday
    ref = datetime.date(2026, 4, 16)  # Thursday
    start, end = parse_interval("monday", relative_to=ref)
    assert start == _dt_start("2026-04-13")  # most recent Monday
    assert end == _dt_end("2026-04-13")


def test_parse_interval_friday():
    # "friday" relative to a friday means today (most recent Friday = today)
    ref = datetime.date(2026, 4, 17)  # Friday
    start, end = parse_interval("friday", relative_to=ref)
    assert start == _dt_start("2026-04-17")  # today
    assert end == _dt_end("2026-04-17")


def test_parse_interval_monday_from_tuesday():
    # "monday" from Tuesday: most recent Monday (yesterday)
    ref = datetime.date(2026, 4, 14)  # Tuesday
    start, end = parse_interval("monday", relative_to=ref)
    assert start == _dt_start("2026-04-13")  # Monday
    assert end == _dt_end("2026-04-13")


def test_parse_interval_yesterday():
    ref = datetime.date(2026, 4, 16)
    start, end = parse_interval("yesterday", relative_to=ref)
    assert start == _dt_start("2026-04-15")
    assert end == _dt_end("2026-04-15")


def test_parse_interval_n_days_ago():
    ref = datetime.date(2026, 4, 16)
    start, end = parse_interval("3 days ago", relative_to=ref)
    assert start == _dt_start("2026-04-13")
    assert end == _dt_end("2026-04-13")


def test_parse_interval_open_end():
    # "yesterday:" means from yesterday to today
    ref = datetime.date(2026, 4, 16)
    start, end = parse_interval("yesterday:", relative_to=ref)
    assert start == _dt_start("2026-04-15")
    assert end == _dt_end("2026-04-16")


def test_parse_interval_open_start_raises():
    # ":2026-04-16" (missing start) should raise
    with pytest.raises(ValueError, match="Start date"):
        parse_interval(":2026-04-16")


def test_parse_interval_friday_colon_monday():
    # "friday:monday" means from most recent Friday to most recent Monday
    ref = datetime.date(2026, 4, 14)  # Tuesday
    start, end = parse_interval("friday:monday", relative_to=ref)
    assert start == _dt_start("2026-04-10")  # most recent Friday
    assert end == _dt_end("2026-04-13")  # most recent Monday


def test_parse_interval_reversed_range_raises():
    with pytest.raises(ValueError, match="before start"):
        parse_interval("2026-04-11:2026-04-09")


def test_parse_interval_bad_value_raises():
    with pytest.raises(ValueError):
        parse_interval("notadate")


# --- compact_date_range ---


@pytest.mark.parametrize(
    "start,end,expected",
    [
        # single day
        ("2026-02-12", "2026-02-12", "2026-02-12"),
        # same month: all days listed as a set
        ("2026-02-12", "2026-02-14", "2026-02-{12,13,14}"),
        ("2026-04-18", "2026-04-20", "2026-04-{18,19,20}"),
        # cross-month, same year: interval only (start and end)
        ("2026-03-31", "2026-04-02", "2026-[03-31,04-02]"),
        ("2026-01-30", "2026-02-02", "2026-[01-30,02-02]"),
        # cross-year: full date interval
        ("2025-12-31", "2026-01-01", "[2025-12-31,2026-01-01]"),
        ("2024-12-30", "2025-01-03", "[2024-12-30,2025-01-03]"),
    ],
)
def test_compact_date_range(start: str, end: str, expected: str) -> None:
    assert compact_date_range(_d(start), _d(end)) == expected
