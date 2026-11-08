from datetime import timedelta

from .models import JournalTemplate

DAILY_KINDS = ("pre_market", "post_market")

DEFAULT_TEMPLATES = [
    ("pre_market", "Pre-market plan", [
        "What is the market doing, and what are you watching?",
        "Which setups will you trade today?",
        "What are your limits for today (loss, number of trades)?",
        "How are you feeling going in?",
    ]),
    ("post_market", "Post-market review", [
        "What went well today?",
        "What went wrong or broke your rules?",
        "What will you do differently next session?",
        "Anything else worth noting?",
    ]),
    ("weekly", "Weekly review", [
        "What were your best trades this week, and why?",
        "Which habits cost you the most?",
        "What is the one thing to fix next week?",
    ]),
]


def ensure_default_templates(user):
    """Give a user the starter templates the first time they need any."""
    if JournalTemplate.objects.filter(user=user).exists():
        return
    JournalTemplate.objects.bulk_create(
        [JournalTemplate(user=user, kind=kind, name=name, prompts=prompts)
         for kind, name, prompts in DEFAULT_TEMPLATES],
        ignore_conflicts=True,
    )


def streaks(days, today):
    """days: the dates that have a filled daily entry. Returns the counters the dashboard shows."""
    days = sorted({d for d in days if d <= today})
    present = set(days)

    longest = run = 0
    previous = None
    for day in days:
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        longest = max(longest, run)
        previous = day

    # Today is still open, so a streak that ended yesterday is alive, not broken
    anchor = today if today in present else today - timedelta(days=1)
    current = 0
    day = anchor
    while day in present:
        current += 1
        day -= timedelta(days=1)

    return {
        "current_streak": current,
        "longest_streak": longest,
        "total_days": len(days),
        "journaled_today": today in present,
        "last_entry_date": days[-1] if days else None,
    }