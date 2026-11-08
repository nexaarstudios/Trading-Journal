from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, time, timedelta

from django.db import transaction

from .filters import user_zone
from .models import DailyRollup, Trade, TradingAccount
from .reports import daily_buckets

_pending = ContextVar("rollup_pending", default=None)


def _row(account, bucket):
    return DailyRollup(
        account=account,
        date=bucket["date"],
        net_pnl=bucket["net_pnl"],
        trade_count=bucket["trade_count"],
        wins=bucket["wins"],
        losses=bucket["losses"],
        r_total=bucket["r_total"],
    )


def _trade_rows(qs):
    return list(qs.order_by("closed_at", "id").values_list("closed_at", "net_pnl", "r_multiple"))


def refresh(account, moments):
    """Recompute every day between the earliest and latest of these close times."""
    tz = user_zone(account.user)
    dates = {m.astimezone(tz).date() for m in moments}
    if not dates:
        return
    first, last = min(dates), max(dates)
    start = datetime.combine(first, time.min, tzinfo=tz)
    end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz)
    rows = _trade_rows(
        Trade.objects.filter(account=account, closed_at__gte=start, closed_at__lt=end)
    )
    with transaction.atomic():
        DailyRollup.objects.filter(account=account, date__gte=first, date__lte=last).delete()
        DailyRollup.objects.bulk_create(
            [_row(account, b) for b in daily_buckets(rows, tz)], batch_size=1000
        )


def rebuild_account(account):
    tz = user_zone(account.user)
    rows = _trade_rows(Trade.objects.filter(account=account))
    with transaction.atomic():
        DailyRollup.objects.filter(account=account).delete()
        DailyRollup.objects.bulk_create(
            [_row(account, b) for b in daily_buckets(rows, tz)], batch_size=1000
        )


def rebuild_user(user):
    for account in TradingAccount.objects.filter(user=user).select_related("user"):
        rebuild_account(account)


def _flush(pending):
    accounts = TradingAccount.objects.filter(pk__in=list(pending)).select_related("user")
    for account in accounts:
        refresh(account, pending[account.pk])


def touch(account_id, moments):
    """Mark days as changed. Inside deferred() they are collected, otherwise refreshed now."""
    pending = _pending.get()
    if pending is not None:
        pending[account_id].update(moments)
    else:
        _flush({account_id: set(moments)})


@contextmanager
def deferred():
    """Collect every change made inside the block and refresh once at the end."""
    if _pending.get() is not None:  # already inside one
        yield
        return
    pending = defaultdict(set)
    token = _pending.set(pending)
    try:
        yield
    finally:
        _pending.reset(token)
    _flush(pending)