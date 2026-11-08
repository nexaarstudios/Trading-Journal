from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.utils.dateparse import parse_date
from rest_framework.exceptions import ValidationError


def user_zone(user):
    try:
        return ZoneInfo(user.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def int_param(params, name):
    raw = params.get(name)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except ValueError:
        raise ValidationError({name: "Must be a whole number."})


def int_list_param(params, name):
    """?tag=1&tag=2 and ?tag=1,2 both work."""
    values = []
    for raw in params.getlist(name):
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                values.append(int(part))
            except ValueError:
                raise ValidationError({name: "Use whole numbers, e.g. ?tag=1,2."})
    return values


def apply_base_filters(qs, params):
    """Filters that don't involve dates: account, symbol, side, outcome, tag."""
    account = int_param(params, "account")
    if account is not None:
        qs = qs.filter(account_id=account)
    if symbol := params.get("symbol"):
        qs = qs.filter(symbol=symbol.strip().upper())
    if side := params.get("side"):
        qs = qs.filter(side=side)
    playbook = int_param(params, "playbook")
    if playbook is not None:
        qs = qs.filter(playbook_id=playbook)

    outcome = params.get("outcome")
    if outcome:
        if outcome == "win":
            qs = qs.filter(net_pnl__gt=0)
        elif outcome == "loss":
            qs = qs.filter(net_pnl__lt=0)
        elif outcome == "breakeven":
            qs = qs.filter(net_pnl=0)
        else:
            raise ValidationError({"outcome": "Use win, loss or breakeven."})

    tag_ids = int_list_param(params, "tag")
    if tag_ids:  # any of these tags
        through = qs.model.tags.through
        qs = qs.filter(id__in=through.objects.filter(tag_id__in=tag_ids).values("trade_id"))
    return qs


def _date_param(params, name):
    raw = params.get(name)
    if not raw:
        return None
    try:
        value = parse_date(raw)
    except ValueError:
        value = None
    if value is None:
        raise ValidationError({name: "Use the format YYYY-MM-DD."})
    return value


def date_bounds(params, tz):
    """Turn ?from=&to= (local dates, both inclusive) into a UTC-comparable [start, end)."""
    first = _date_param(params, "from")
    last = _date_param(params, "to")
    if first and last and last < first:
        raise ValidationError({"to": "'to' cannot be before 'from'."})
    start = datetime.combine(first, time.min, tzinfo=tz) if first else None
    try:
        end = datetime.combine(last + timedelta(days=1), time.min, tzinfo=tz) if last else None
    except OverflowError:
        raise ValidationError({"to": "Date is out of range."})
    return start, end