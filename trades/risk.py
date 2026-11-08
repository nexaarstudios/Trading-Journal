from decimal import Decimal

ZERO = Decimal("0")
WARNING = Decimal("0.70")
ALERT = Decimal("0.90")
ORDER = ["ok", "warning", "alert", "breached"]


def level_for(ratio):
    """ratio = used / limit."""
    if ratio >= 1:
        return "breached"
    if ratio >= ALERT:
        return "alert"
    if ratio >= WARNING:
        return "warning"
    return "ok"


def worst(levels):
    return max(levels, key=ORDER.index, default="ok")


def current_drawdown(nets, opening_balance):
    """Walk the trades in order. Returns (peak equity, current equity, fall from the peak)."""
    equity = peak = opening_balance
    for net in nets:
        equity += net
        if equity > peak:
            peak = equity
    return peak, equity, peak - equity


def _as_unit(rule, money, starting_balance):
    if rule.unit == "percent":
        return money * 100 / starting_balance if starting_balance > 0 else ZERO
    return money


def _used(rule, starting_balance, day_net, day_count, drawdown, risks):
    if rule.kind == "daily_loss":
        return _as_unit(rule, max(ZERO, -day_net), starting_balance)
    if rule.kind == "max_drawdown":
        return _as_unit(rule, drawdown, starting_balance)
    if rule.kind == "trades_per_day":
        return Decimal(day_count)
    if rule.kind == "risk_per_trade":
        if rule.unit == "percent":
            return max((amount * 100 / balance for amount, balance in risks if balance > 0), default=ZERO)
        return max((amount for amount, _ in risks), default=ZERO)
    return ZERO


def evaluate(rules, *, starting_balance, day_net, day_count, drawdown, risks):
    """risks: [(money risked, account balance when the trade opened)] for the day's trades."""
    results = []
    for rule in rules:
        limit = rule.limit_value
        used = _used(rule, starting_balance, day_net, day_count, drawdown, risks)
        ratio = used / limit if limit > 0 else Decimal(1)
        remaining = max(limit - used, ZERO)
        if rule.unit == "count":
            limit, used, remaining = int(limit), int(used), int(remaining)
        results.append({
            "id": rule.id,
            "kind": rule.kind,
            "unit": rule.unit,
            "limit": limit,
            "used": used,
            "remaining": remaining,
            "percent_of_limit": ratio * 100,
            "level": level_for(ratio),
        })
    return results