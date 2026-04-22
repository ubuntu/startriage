"""Tests for startriage.dates - date range parsing and formatting."""

from __future__ import annotations

import datetime

import pytest

from startriage.dates import auto_date_range, compact_date_range, parse_interval, reverse_auto_date_range


def _d(s: str) -> datetime.date:
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


# --- auto_date_range ---


@pytest.mark.parametrize(
    "today,keyword,start,end",
    [
        ("2019-05-14", "mon", "2019-05-10", "2019-05-12"),
        ("2019-05-14", "tue", "2019-05-13", "2019-05-13"),
        ("2019-05-13", "tue", "2019-05-06", "2019-05-06"),
        ("2019-05-14", "wed", "2019-05-07", "2019-05-07"),
        # dsctriage variants (full names)
        ("2019-05-14", "monday", "2019-05-10", "2019-05-12"),
        ("2019-05-14", "tuesday", "2019-05-13", "2019-05-13"),
        ("2019-05-14", "wednesday", "2019-05-07", "2019-05-07"),
    ],
)
def test_auto_date_range(today, keyword, start, end):
    assert auto_date_range(keyword, today=_d(today)) == (_d(start), _d(end))


@pytest.mark.parametrize(
    "today,keyword",
    [
        ("2019-05-14", "sun"),
        ("2019-05-14", "sat"),
        ("2019-05-14", "sunday"),
        ("2019-05-14", "saturday"),
    ],
)
def test_auto_date_range_weekend_raises(today, keyword):
    with pytest.raises(ValueError):
        auto_date_range(keyword, today=_d(today))


def test_auto_date_range_bad_keyword():
    with pytest.raises(ValueError):
        auto_date_range("notaday")


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
    assert reverse_auto_date_range(_d(start), _d(end)) == expected


# --- parse_interval ---


def test_parse_interval_none_weekday():
    # Yesterday was a weekday: single day
    ref = datetime.date(2026, 4, 16)  # Thursday
    start, end = parse_interval(None, relative_to=ref)
    assert start == end == datetime.date(2026, 4, 15)


def test_parse_interval_none_monday():
    # Yesterday was Sunday: return full weekend Fri-Sun
    ref = datetime.date(2026, 4, 20)  # Monday (yesterday=Sunday)
    start, end = parse_interval(None, relative_to=ref)
    yesterday = ref - datetime.timedelta(days=1)  # Sunday 2026-04-19
    assert end == yesterday
    assert start == yesterday - datetime.timedelta(days=2)  # Friday 2026-04-17


def test_parse_interval_single_date():
    start, end = parse_interval("2026-04-09")
    assert start == end == datetime.date(2026, 4, 9)


def test_parse_interval_range():
    start, end = parse_interval("2026-04-09:2026-04-11")
    assert start == datetime.date(2026, 4, 9)
    assert end == datetime.date(2026, 4, 11)


def test_parse_interval_range_same():
    start, end = parse_interval("2026-12-09:2026-12-09")
    assert start == datetime.date(2026, 12, 9)
    assert end == datetime.date(2026, 12, 9)


def test_parse_interval_day_name():
    # monday relative to a thursday should give fri-sun
    ref = datetime.date(2026, 4, 16)  # Thursday
    start, end = parse_interval("monday", relative_to=ref)
    assert start == datetime.date(2026, 4, 10)
    assert end == datetime.date(2026, 4, 12)


def test_parse_interval_friday():
    # Friday triage: show previous weekday (Thursday)
    ref = datetime.date(2026, 4, 17)  # Friday
    start, end = parse_interval("friday", relative_to=ref)
    assert start == end == datetime.date(2026, 4, 16)  # Thursday


def test_parse_interval_monday_from_tuesday():
    # Monday triage from Tuesday context: Fri-Sun of the previous week
    ref = datetime.date(2026, 4, 14)  # Tuesday
    start, end = parse_interval("monday", relative_to=ref)
    assert start == datetime.date(2026, 4, 10)  # Friday
    assert end == datetime.date(2026, 4, 12)  # Sunday


def test_parse_interval_yesterday():
    ref = datetime.date(2026, 4, 16)
    start, end = parse_interval("yesterday", relative_to=ref)
    assert start == end == datetime.date(2026, 4, 15)


def test_parse_interval_n_days_ago():
    ref = datetime.date(2026, 4, 16)
    start, end = parse_interval("3 days ago", relative_to=ref)
    assert start == end == datetime.date(2026, 4, 13)


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
