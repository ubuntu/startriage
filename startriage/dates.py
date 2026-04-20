"""Date parsing and triage range calculation for startriage."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta


def auto_date_range(keyword: str, today: date | None = None) -> tuple[date, date]:
    """Given a day-of-week keyword, calculate the inclusive triage date range.

    Monday triage covers the previous Friday, Saturday and Sunday.
    Tuesday-Friday triage covers only the previous day.
    Weekends are not valid triage days.

    :raises ValueError: for weekend day names or unrecognised keywords.
    """
    triage_day = today or datetime.now().date()
    triage_found = False

    for _ in range(7):
        if keyword.lower() in (
            datetime.strftime(triage_day, "%A").lower(),
            datetime.strftime(triage_day, "%a").lower(),
        ):
            triage_found = True
            break
        triage_day -= timedelta(days=1)

    if not triage_found:
        raise ValueError(f"Unrecognised day name: '{keyword}'")

    if triage_day.weekday() in (5, 6):
        raise ValueError(f"No triage range defined for weekend day '{keyword}'")

    if triage_day.weekday() == 0:
        # Monday triage: previous Fri-Sun
        return triage_day - timedelta(days=3), triage_day - timedelta(days=1)

    # Normal weekday triage: previous day only
    prev = triage_day - timedelta(days=1)
    return prev, prev


def reverse_auto_date_range(start: date, end: date) -> str | None:
    """Given an inclusive date range, return the triage day label if it matches a known pattern."""
    if start > end or (end - start).days > 2:
        return None

    start_wd = start.weekday()
    end_wd = end.weekday()

    if start_wd == 4 and end_wd == 6:
        return "Monday triage"

    if start == end and start_wd not in (4, 5, 6):
        return ["Tuesday", "Wednesday", "Thursday", "Friday"][start_wd] + " triage"

    return None


def _parse_single_date(token: str, relative_to: date | None = None) -> date:
    """Parse a single date token into a date object.

    Supported formats:
      YYYY-MM-DD, today, yesterday, day names (monday/mon), last <dayname>,
      N days ago, N weeks ago.
    """
    ref = relative_to or datetime.now().date()
    token_lower = token.strip().lower()

    # YYYY-MM-DD
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", token_lower):
        return datetime.strptime(token_lower, "%Y-%m-%d").date()

    if token_lower == "today":
        return ref
    if token_lower == "yesterday":
        return ref - timedelta(days=1)

    # Day names (most recent past occurrence, including today)
    day_names = {
        "monday": 0,
        "mon": 0,
        "tuesday": 1,
        "tue": 1,
        "wednesday": 2,
        "wed": 2,
        "thursday": 3,
        "thu": 3,
        "friday": 4,
        "fri": 4,
        "saturday": 5,
        "sat": 5,
        "sunday": 6,
        "sun": 6,
    }
    if token_lower in day_names:
        target_wd = day_names[token_lower]
        diff = (ref.weekday() - target_wd + 7) % 7
        return ref - timedelta(days=diff)

    # last <dayname>
    parts = token_lower.split()
    if len(parts) == 2 and parts[0] == "last" and parts[1] in day_names:
        target_wd = day_names[parts[1]]
        diff = (ref.weekday() - target_wd + 7) % 7 + 7
        return ref - timedelta(days=diff)

    # N days/weeks ago
    if len(parts) == 3 and parts[2] == "ago":
        try:
            n = int(parts[0])
            unit = parts[1].rstrip("s")
            if unit == "day":
                return ref - timedelta(days=n)
            if unit == "week":
                return ref - timedelta(weeks=n)
        except ValueError:
            pass

    raise ValueError(
        f"Cannot parse date '{token}'. "
        "Expected YYYY-MM-DD, today, yesterday, a day name, 'last <dayname>', or 'N days/weeks ago'."
    )


def parse_interval(arg: str | None, relative_to: date | None = None) -> tuple[date, date]:
    """Parse a -i / --interval argument into an inclusive (start, end) date pair.

    Formats accepted:
      YYYY-MM-DD             → single day
      YYYY-MM-DD:YYYY-MM-DD  → explicit range
      monday / tuesday / ...  -> triage range for that weekday (Mon->Fri-Sun, else ->prev day)
      yesterday / today / N days ago / last monday


    Default when arg is None: yesterday, or Fri-Sun if yesterday was Sunday.
    """
    if arg is None:
        yesterday = (relative_to or datetime.now().date()) - timedelta(days=1)
        if yesterday.weekday() == 6:  # Sunday → include friday + full weekend
            return yesterday - timedelta(days=2), yesterday
        return yesterday, yesterday

    if ":" in arg:
        parts = arg.split(":", 1)
        start = _parse_single_date(parts[0], relative_to)
        end = _parse_single_date(parts[1], relative_to)
        if end < start:
            raise ValueError(f"End date {end} is before start date {start}")
        return start, end

    # Try as a day name → triage range
    day_names_short = {
        "monday",
        "mon",
        "tuesday",
        "tue",
        "wednesday",
        "wed",
        "thursday",
        "thu",
        "friday",
        "fri",
    }
    if arg.strip().lower() in day_names_short:
        try:
            return auto_date_range(arg, relative_to)
        except ValueError:
            pass

    # Single date or relative keyword → single-day range
    d = _parse_single_date(arg, relative_to)
    return d, d
