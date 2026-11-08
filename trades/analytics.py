from collections import Counter, namedtuple
from decimal import Decimal

ZERO = Decimal("0")
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# One closed trade, with everything the grouped reports need
Row = namedtuple("Row", "id opened closed net r side symbol rating seconds")

HOLDING = [  # label, from seconds (inclusive), to seconds (exclusive)
    ("Under 5 minutes", 0, 300),
    ("5 to 15 minutes", 300, 900),
    ("15 to 60 minutes", 900, 3600),
    ("1 to 4 hours", 3600, 14400),
    ("4 to 24 hours", 14400, 86400),
    ("1 to 7 days", 86400, 604800),
    ("Over 7 days", 604800, None),
]

R_BINS = [  # label, from R (inclusive), to R (exclusive)
    ("Below -3", None, -3),
    ("Between -3 and -2", -3, -2),
    ("Between -2 and -1", -2, -1),
    ("Between -1 and 0", -1, 0),
    ("Between 0 and 1", 0, 1),
    ("Between 1 and 2", 1, 2),
    ("Between 2 and 3", 2, 3),
    ("3 or more", 3, None),
]


def stats(pairs):
    """pairs: [(net_pnl, r_multiple)]. The standard column set every grouped report shares."""
    nets = [net for net, _ in pairs]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    total = len(nets)
    gross_profit = sum(wins, ZERO)
    gross_loss = -sum(losses, ZERO)  # a positive amount
    net = gross_profit - gross_loss
    r_values = [r for _, r in pairs if r is not None]
    return {
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": total - len(wins) - len(losses),
        "win_rate": Decimal(len(wins)) * 100 / total if total else None,
        "net_pnl": net,
        "avg_pnl": net / total if total else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "avg_r": sum(r_values, ZERO) / len(r_values) if r_values else None,
        "r_trade_count": len(r_values),
    }


def _group(rows, key):
    groups = {}
    for row in rows:
        groups.setdefault(key(row), []).append((row.net, row.r))
    return groups


def by_weekday(rows, tz):
    groups = _group(rows, lambda t: t.closed.astimezone(tz).weekday())
    return [{"weekday": i, "name": WEEKDAYS[i], **stats(groups.get(i, []))} for i in range(7)]


def by_hour(rows, tz):
    groups = _group(rows, lambda t: t.opened.astimezone(tz).hour)
    return [{"hour": h, "label": f"{h:02d}:00", **stats(groups.get(h, []))} for h in range(24)]


def by_month(rows, tz):
    groups = _group(rows, lambda t: t.closed.astimezone(tz).strftime("%Y-%m"))
    return [{"month": m, **stats(groups[m])} for m in sorted(groups)]


def by_symbol(rows):
    groups = _group(rows, lambda t: t.symbol)
    table = [{"symbol": s, **stats(pairs)} for s, pairs in groups.items()]
    return sorted(table, key=lambda r: (-r["net_pnl"], r["symbol"]))


def long_short(rows):
    groups = _group(rows, lambda t: t.side)
    return [{"side": side, **stats(groups.get(side, []))} for side in ("long", "short")]


def by_rating(rows):
    groups = _group(rows, lambda t: t.rating)
    table = [{"rating": n, "label": str(n), **stats(groups.get(n, []))} for n in range(1, 6)]
    table.append({"rating": None, "label": "Unrated", **stats(groups.get(None, []))})
    return table


def _mean_seconds(values):
    return sum(values) // len(values) if values else None  # whole seconds, rounded down


def holding_time(rows):
    buckets = [[] for _ in HOLDING]
    for t in rows:
        for i, (_label, _low, high) in enumerate(HOLDING):
            if high is None or t.seconds < high:
                buckets[i].append((t.net, t.r))
                break
    table = [
        {"label": label, "min_seconds": low, "max_seconds": high, **stats(bucket)}
        for (label, low, high), bucket in zip(HOLDING, buckets)
    ]
    seconds = sorted(t.seconds for t in rows)
    median = None
    if seconds:
        mid = len(seconds) // 2
        median = seconds[mid] if len(seconds) % 2 else (seconds[mid - 1] + seconds[mid]) // 2
    summary = {
        "avg_seconds": _mean_seconds(seconds),
        "median_seconds": median,
        "avg_win_seconds": _mean_seconds([t.seconds for t in rows if t.net > 0]),
        "avg_loss_seconds": _mean_seconds([t.seconds for t in rows if t.net < 0]),
    }
    return table, summary


def r_distribution(rows):
    """Only trades that have an R-multiple (a stop loss) are counted."""
    scored = [t for t in rows if t.r is not None]
    buckets = [[] for _ in R_BINS]
    for t in scored:
        for i, (_label, low, high) in enumerate(R_BINS):
            if (low is None or t.r >= low) and (high is None or t.r < high):
                buckets[i].append(t)
                break
    total = len(scored)
    table = [
        {
            "label": label, "from_r": low, "to_r": high, "trades": len(bucket),
            "share": Decimal(len(bucket)) * 100 / total if total else None,
            "net_pnl": sum((t.net for t in bucket), ZERO),
        }
        for (label, low, high), bucket in zip(R_BINS, buckets)
    ]
    values = sorted(t.r for t in scored)
    median = None
    if values:
        mid = len(values) // 2
        median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    summary = {
        "r_trade_count": total,
        "unscored_trades": len(rows) - total,
        "avg_r": sum(values, ZERO) / total if total else None,
        "median_r": median,
        "best_r": values[-1] if values else None,
        "worst_r": values[0] if values else None,
    }
    return table, summary


def streaks(rows, tz):
    """Consecutive wins or losses in close order. A breakeven trade ends the run."""
    runs, current = [], None
    for t in rows:
        kind = "win" if t.net > 0 else "loss" if t.net < 0 else None
        if kind is None:
            current = None
            continue
        day = t.closed.astimezone(tz).date()
        if current is not None and current["kind"] == kind:
            current["length"] += 1
            current["net_pnl"] += t.net
            current["end"] = day
        else:
            current = {"kind": kind, "length": 1, "net_pnl": t.net, "start": day, "end": day}
            runs.append(current)

    counts = Counter((r["kind"], r["length"]) for r in runs)
    ordered = sorted(counts.items(), key=lambda item: (item[0][0] != "win", item[0][1]))
    table = [{"kind": kind, "length": length, "count": n} for (kind, length), n in ordered]

    def longest(kind):
        pool = [r for r in runs if r["kind"] == kind]
        if not pool:
            return None
        best = max(pool, key=lambda r: r["length"])  # the first one to reach that length
        return {key: best[key] for key in ("length", "net_pnl", "start", "end")}

    def average(kind):
        lengths = [r["length"] for r in runs if r["kind"] == kind]
        return Decimal(sum(lengths)) / len(lengths) if lengths else None

    summary = {
        "win_runs": sum(1 for r in runs if r["kind"] == "win"),
        "loss_runs": sum(1 for r in runs if r["kind"] == "loss"),
        "longest_win_streak": longest("win"),
        "longest_loss_streak": longest("loss"),
        "avg_win_streak": average("win"),
        "avg_loss_streak": average("loss"),
        # Still running only if the most recent trade was a win or a loss
        "current_streak": {"kind": current["kind"], "length": current["length"]} if current else None,
    }
    return table, summary