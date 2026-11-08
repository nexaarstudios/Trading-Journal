from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

ZERO = Decimal("0")
CENT = Decimal("0.01")


def summarize(rows, opening_balance):
    """rows: [(closed_at, net_pnl, r_multiple)] ordered by close time."""
    nets = [net for _, net, _ in rows]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    total = len(nets)
    gross_profit = sum(wins, ZERO)
    gross_loss = abs(sum(losses, ZERO))  # reported as a positive amount
    r_values = [r for _, _, r in rows if r is not None]

    # Walk the trades in order, tracking the running peak and the worst fall from it
    equity = peak = opening_balance
    max_dd = max_dd_pct = ZERO
    for n in nets:
        equity += n
        if equity > peak:
            peak = equity
        drop = peak - equity
        if drop > max_dd:
            max_dd = drop
        if peak > 0:
            pct = drop / peak * 100
            if pct > max_dd_pct:
                max_dd_pct = pct

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": total - len(wins) - len(losses),
        "win_rate": Decimal(len(wins)) * 100 / total if total else None,
        "net_pnl": gross_profit - gross_loss,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "avg_win": gross_profit / len(wins) if wins else None,
        "avg_loss": sum(losses, ZERO) / len(losses) if losses else None,
        "expectancy": (gross_profit - gross_loss) / total if total else None,
        "avg_r": sum(r_values, ZERO) / len(r_values) if r_values else None,
        "r_trade_count": len(r_values),
        "largest_win": max(wins) if wins else None,
        "largest_loss": min(losses) if losses else None,
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "closing_balance": equity,
    }


def daily_buckets(rows, tz):
    """Group trades by the day they closed, in the user's timezone."""
    days = {}
    for closed_at, net, r in rows:
        day = closed_at.astimezone(tz).date()
        bucket = days.get(day)
        if bucket is None:
            bucket = days[day] = {
                "date": day, "net_pnl": ZERO, "trade_count": 0,
                "wins": 0, "losses": 0, "r_total": ZERO,
            }
        bucket["net_pnl"] += net
        bucket["trade_count"] += 1
        if net > 0:
            bucket["wins"] += 1
        elif net < 0:
            bucket["losses"] += 1
        if r is not None:
            bucket["r_total"] += r
    return [days[d] for d in sorted(days)]


def equity_curve(buckets, opening_balance):
    equity = peak = opening_balance
    points = []
    for b in buckets:
        equity += b["net_pnl"]
        peak = max(peak, equity)
        points.append({
            "date": b["date"],
            "net_pnl": b["net_pnl"],
            "trade_count": b["trade_count"],
            "equity": equity,
            "drawdown": peak - equity,
        })
    return points


def weekly_totals(buckets):
    """Weeks start on Monday."""
    weeks = {}
    for b in buckets:
        monday = b["date"] - timedelta(days=b["date"].weekday())
        week = weeks.setdefault(
            monday,
            {"week_start": monday, "net_pnl": ZERO, "trade_count": 0, "r_total": ZERO},
        )
        week["net_pnl"] += b["net_pnl"]
        week["trade_count"] += b["trade_count"]
        week["r_total"] += b["r_total"]
    return [weeks[k] for k in sorted(weeks)]


def jsonable(value):
    """Money becomes a 2-decimal string. DRF would otherwise turn Decimals into floats."""
    if isinstance(value, Decimal):
        rounded = value.quantize(CENT, rounding=ROUND_HALF_UP)
        return str(ZERO.quantize(CENT) if rounded == 0 else rounded)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value