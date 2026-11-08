def streaks(trades, tz):
    """trades: [(id, closed_at, followed)] oldest first, only trades whose playbook had rules."""
    longest = run = 0
    last_broken = None
    for trade_id, closed_at, followed in trades:
        if followed:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
            last_broken = {"trade_id": trade_id, "date": closed_at.astimezone(tz).date()}

    # A day is clean only if every scored trade that day followed every rule
    days = []
    for _trade_id, closed_at, followed in trades:
        day = closed_at.astimezone(tz).date()
        if days and days[-1][0] == day:
            days[-1][1] = days[-1][1] and followed
        else:
            days.append([day, followed])
    day_run = day_longest = 0
    for _day, clean in days:
        if clean:
            day_run += 1
            day_longest = max(day_longest, day_run)
        else:
            day_run = 0

    return {
        "trades": {
            "current": run, "longest": longest, "total": len(trades),
            "followed": sum(1 for _i, _c, followed in trades if followed),
        },
        "days": {
            "current": day_run, "longest": day_longest, "total_days": len(days),
            "clean_days": sum(1 for _day, clean in days if clean),
            "last_scored_date": days[-1][0] if days else None,
        },
        "last_broken": last_broken,
    }