"""Calendar arithmetic over sim-clock milliseconds.

Every function here is a pure function of its arguments. Nothing reads the wall
clock -- `now_ms` is threaded down from the caller, and
tests/test_ground_truth_isolation.py fails the build if `.now()`, `.utcnow()`,
or `time.time()` appears anywhere under rcp/.

Payday arithmetic matters more than it looks: the subscription proposer's whole
edge is retrying just after money lands, and `events.payday_phase` (a generated
column) buckets the value this module computes.
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

MS_PER_DAY = 86_400_000
MS_PER_HOUR = 3_600_000

# The sim clock is UTC; contact-hour rules are local. A 9pm-9am quiet window in
# India is not the same ten hours in UTC, and getting this wrong guards the
# wrong part of the day without failing anything.
IST_OFFSET_MS = 330 * 60 * 1000  # UTC+05:30


def to_utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def to_ms(d: date) -> int:
    return int(
        datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000
    )


def day_start_ms(ms: int) -> int:
    return (ms // MS_PER_DAY) * MS_PER_DAY


def add_days(ms: int, days: int) -> int:
    return ms + days * MS_PER_DAY


def add_hours(ms: int, hours: int) -> int:
    return ms + hours * MS_PER_HOUR


def hour_of_day(ms: int) -> int:
    return to_utc(ms).hour


def day_of_month(ms: int) -> int:
    return to_utc(ms).day


def local_hour(ms: int, offset_ms: int = IST_OFFSET_MS) -> int:
    """Hour of day in the customer's timezone, 0-23."""
    return int(((ms + offset_ms) % MS_PER_DAY) // MS_PER_HOUR)


def in_quiet_hours(
    ms: int, *, start_hour: int, end_hour: int, offset_ms: int = IST_OFFSET_MS
) -> bool:
    """Is this instant inside a window that wraps midnight (e.g. 21:00-09:00)?"""
    hour = local_hour(ms, offset_ms)
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:          # a same-day window, e.g. 01:00-06:00
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def shift_out_of_quiet_hours(
    ms: int, *, start_hour: int, end_hour: int, offset_ms: int = IST_OFFSET_MS
) -> int:
    """Move an instant forward to the first moment the window allows.

    Returns `ms` unchanged when it is already allowed. Deliberately a *shift*
    rather than a denial: a message that would have gone out at 10pm is still
    worth sending at 9am, and denying it would throw away real recovery value
    to satisfy a timing rule.
    """
    if not in_quiet_hours(ms, start_hour=start_hour, end_hour=end_hour,
                          offset_ms=offset_ms):
        return ms

    local = ms + offset_ms
    local_midnight = (local // MS_PER_DAY) * MS_PER_DAY
    target = local_midnight + end_hour * MS_PER_HOUR
    if target <= local:
        target += MS_PER_DAY          # past today's opening; wait for tomorrow's
    return target - offset_ms


def _payday_in_month(year: int, month: int, dom: int) -> date:
    """Clamp to the last day for short months -- a 30th-of-month payday lands on
    Feb 28 rather than raising or silently sliding into March."""
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(dom, last))


def next_payday_ms(ms: int, payday_dom: int) -> int:
    d = to_utc(ms).date()
    this_month = _payday_in_month(d.year, d.month, payday_dom)
    if this_month >= d:
        return to_ms(this_month)
    year, month = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return to_ms(_payday_in_month(year, month, payday_dom))


def last_payday_ms(ms: int, payday_dom: int) -> int:
    d = to_utc(ms).date()
    this_month = _payday_in_month(d.year, d.month, payday_dom)
    if this_month <= d:
        return to_ms(this_month)
    year, month = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
    return to_ms(_payday_in_month(year, month, payday_dom))


def payday_phase(days: int | None) -> str:
    """Bucket a signed payday distance.

    This MUST stay identical to the `payday_phase` generated column in
    migrations.py. The column is the source of truth for stored events; this
    function exists because scheduling asks the same question about a *future*
    timestamp that has no row yet. tests/test_timeutil.py asserts the two agree
    across the full range.
    """
    if days is None:
        return "unknown"
    if -3 <= days <= 0:
        return "pre_payday"
    if 1 <= days <= 5:
        return "post_payday"
    return "mid_cycle"


def days_from_payday(ms: int, payday_dom: int | None) -> int | None:
    """Signed distance to the nearer payday.

    Negative means payday is coming (money is not there yet); positive means it
    has just landed. The sign convention is what `events.payday_phase` keys off:
    -3..0 is pre_payday, 1..5 is post_payday, anything else is mid_cycle.
    """
    if payday_dom is None:
        return None
    today = day_start_ms(ms)
    since = (today - last_payday_ms(ms, payday_dom)) // MS_PER_DAY
    until = (next_payday_ms(ms, payday_dom) - today) // MS_PER_DAY
    return int(since) if since <= until else int(-until)
