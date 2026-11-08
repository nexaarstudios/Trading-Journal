from calendar import monthrange
from datetime import date, datetime, time, timedelta

CUMULATIVE = ("net_pnl", "journal_days")  # these build up through the month, so they have a pace line


def first_of_next_month(first):
    return (first + timedelta(days=32)).replace(day=1)


def month_bounds(first, tz):
    """The month as a [start, end) window of aware datetimes in the user's timezone."""
    return (
        datetime.combine(first, time.min, tzinfo=tz),
        datetime.combine(first_of_next_month(first), time.min, tzinfo=tz),
    )


def active_in(start, end, month):
    """Does a goal running from start to end (None = forever) cover this month?"""
    return start <= month and (end is None or month <= end)


def overlaps(a_start, a_end, b_start, b_end):
    far = date.max
    return a_start <= (b_end or far) and b_start <= (a_end or far)


def evaluate(kind, target, current, month, today):
    """Turn a measured value into the numbers a progress card shows."""
    days = monthrange(month.year, month.month)[1]
    last_day = month.replace(day=days)
    started = month <= today
    achieved = started and current is not None and current >= target
    percent = current * 100 / target if current is not None else None

    expected = None
    if not started:
        status = "upcoming"
    elif last_day < today:
        status = "achieved" if achieved else "missed"
    else:  # the month is under way
        if kind in CUMULATIVE:
            expected = target * today.day / days
        if achieved:
            status = "achieved"
        elif current is None:
            status = "no_data"
        elif expected is not None and current >= expected:
            status = "on_pace"
        else:
            status = "behind"
    return {
        "percent": percent,
        "achieved": achieved,
        "status": status,
        "expected_by_today": expected,
    }