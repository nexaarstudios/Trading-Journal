from datetime import datetime, time, timedelta

from django.db.models import Avg, Count, F, Q, Sum
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import reports
from .filters import apply_base_filters, date_bounds, int_param, user_zone
from .models import DailyRollup, Tag, Trade, TradeRuleCheck, TradingAccount


def _context(request):
    """The user's accounts in scope, their filtered trades, and the shared currency."""
    params = request.query_params
    accounts = TradingAccount.objects.filter(user=request.user)

    account_id = int_param(params, "account")
    if account_id is not None:
        accounts = accounts.filter(pk=account_id)
        if not accounts.exists():
            raise ValidationError({"account": "Account not found."})

    currencies = set(accounts.values_list("currency", flat=True))
    if len(currencies) > 1:
        raise ValidationError(
            {"account": "Your accounts use different currencies. Choose one with ?account=<id>."}
        )

    trades = apply_base_filters(Trade.objects.filter(account__in=accounts), params)
    return accounts, trades, next(iter(currencies), None)


def _opening_balance(accounts, trades, start):
    """Starting balances, plus everything the same filters earned before the window."""
    total = accounts.aggregate(total=Sum("starting_balance"))["total"] or reports.ZERO
    if start is not None:
        prior = trades.filter(closed_at__lt=start).aggregate(total=Sum("net_pnl"))["total"]
        total += prior or reports.ZERO
    return total


def _window(trades, start, end):
    if start is not None:
        trades = trades.filter(closed_at__gte=start)
    if end is not None:
        trades = trades.filter(closed_at__lt=end)
    return trades


def _rows(trades):
    return list(
        trades.order_by("closed_at", "id").values_list("closed_at", "net_pnl", "r_multiple")
    )


def _daily(params, accounts, trades, start, end, tz):
    """Per-day buckets. Reads the rollup table unless a filter it can't express is set."""
    if any(params.get(key) for key in ("symbol", "side", "outcome", "tag", "playbook")):
        return reports.daily_buckets(_rows(_window(trades, start, end)), tz)

    rollup = DailyRollup.objects.filter(account__in=accounts)
    if start is not None:
        rollup = rollup.filter(date__gte=start.date())  # start is local midnight, so this is a local date
    if end is not None:
        rollup = rollup.filter(date__lt=end.date())
    grouped = (
        rollup.values("date")
        .annotate(
            day_net=Sum("net_pnl"), day_count=Sum("trade_count"), day_wins=Sum("wins"),
            day_losses=Sum("losses"), day_r=Sum("r_total"),
        )
        .order_by("date")
    )
    return [
        {
            "date": g["date"], "net_pnl": g["day_net"], "trade_count": g["day_count"],
            "wins": g["day_wins"], "losses": g["day_losses"], "r_total": g["day_r"],
        }
        for g in grouped
    ]


class SummaryView(APIView):
    def get(self, request):
        accounts, trades, currency = _context(request)
        start, end = date_bounds(request.query_params, user_zone(request.user))
        opening = _opening_balance(accounts, trades, start)

        body = reports.summarize(_rows(_window(trades, start, end)), opening)
        body["currency"] = currency
        body["opening_balance"] = opening
        return Response(reports.jsonable(body))


class EquityView(APIView):
    def get(self, request):
        accounts, trades, currency = _context(request)
        tz = user_zone(request.user)
        start, end = date_bounds(request.query_params, tz)
        opening = _opening_balance(accounts, trades, start)

        buckets = _daily(request.query_params, accounts, trades, start, end, tz)
        return Response(reports.jsonable({
            "currency": currency,
            "opening_balance": opening,
            "points": reports.equity_curve(buckets, opening),
        }))


class CalendarView(APIView):
    def get(self, request):
        accounts, trades, currency = _context(request)
        tz = user_zone(request.user)

        raw = request.query_params.get("month")
        if raw:
            try:
                first = datetime.strptime(raw, "%Y-%m").date()
            except ValueError:
                raise ValidationError({"month": "Use the format YYYY-MM."})
        else:
            first = datetime.now(tz).date().replace(day=1)
        following = (first + timedelta(days=32)).replace(day=1)
        start = datetime.combine(first, time.min, tzinfo=tz)
        end = datetime.combine(following, time.min, tzinfo=tz)

        buckets = _daily(request.query_params, accounts, trades, start, end, tz)
        totals = {
            "net_pnl": sum((b["net_pnl"] for b in buckets), reports.ZERO),
            "trade_count": sum(b["trade_count"] for b in buckets),
            "r_total": sum((b["r_total"] for b in buckets), reports.ZERO),
        }
        return Response(reports.jsonable({
            "month": first.strftime("%Y-%m"),
            "currency": currency,
            "opening_balance": _opening_balance(accounts, trades, start),
            "days": buckets,
            "weeks": reports.weekly_totals(buckets),
            "totals": totals,
        }))

class TagReportView(APIView):
    """Rank tags by the money lost on trades that carry them (default: mistake tags)."""
    fixed_kind = None  # lets the CSV export reuse this view for one tag kind

    def get(self, request):
        accounts, trades, currency = _context(request)
        start, end = date_bounds(request.query_params, user_zone(request.user))

        kind = self.fixed_kind or request.query_params.get("kind", "mistake")
        if kind not in Tag.Kind.values:
            raise ValidationError({"kind": "Use setup, mistake, condition or emotion."})

        links = Trade.tags.through.objects.filter(
            trade__in=_window(trades, start, end), tag__kind=kind
        )
        grouped = links.values("tag_id", "tag__name").annotate(
            n_trades=Count("id"),
            n_wins=Count("id", filter=Q(trade__net_pnl__gt=0)),
            net=Sum("trade__net_pnl"),
            lost=Sum("trade__net_pnl", filter=Q(trade__net_pnl__lt=0)),
            avg_r=Avg("trade__r_multiple"),
            n_r=Count("trade__r_multiple"),
        )

        tags = [
            {
                "id": g["tag_id"],
                "name": g["tag__name"],
                "kind": kind,
                "trade_count": g["n_trades"],
                "wins": g["n_wins"],
                "win_rate": reports.Decimal(g["n_wins"]) * 100 / g["n_trades"],
                "net_pnl": g["net"] or reports.ZERO,
                "money_lost": abs(g["lost"] or reports.ZERO),
                "avg_r": g["avg_r"],
                "r_trade_count": g["n_r"],
            }
            for g in grouped
        ]
        tags.sort(key=lambda t: (-t["money_lost"], -t["trade_count"], t["name"].lower()))
        return Response(reports.jsonable({"currency": currency, "kind": kind, "tags": tags}))

def _share(part, whole):
    return reports.Decimal(part) * 100 / whole if whole else None


def _mean(total, count):
    return total / count if count else None


class PlaybookReportView(APIView):
    """Adherence, followed-vs-broken P&L per playbook, and what each rule costs when broken."""

    def get(self, request):
        accounts, trades, currency = _context(request)
        start, end = date_bounds(request.query_params, user_zone(request.user))
        scoped = _window(trades, start, end).filter(playbook__isnull=False)

        followed = Q(rules_total__gt=0, rules_checked=F("rules_total"))
        broken = Q(rules_total__gt=0, rules_checked__lt=F("rules_total"))
        win = Q(net_pnl__gt=0)

        grouped = (
            scoped.order_by()
            .values("playbook_id", "playbook__name")
            .annotate(
                n=Count("id"),
                net=Sum("net_pnl"),
                scored=Count("id", filter=Q(rules_total__gt=0)),
                followed_n=Count("id", filter=followed),
                followed_net=Sum("net_pnl", filter=followed),
                followed_wins=Count("id", filter=followed & win),
                broken_n=Count("id", filter=broken),
                broken_net=Sum("net_pnl", filter=broken),
                broken_wins=Count("id", filter=broken & win),
                rules_sum=Sum("rules_total"),
                ticked_sum=Sum("rules_checked"),
            )
        )

        per_rule = (
            TradeRuleCheck.objects.filter(trade__in=scoped.order_by().values("id"))
            .order_by()
            .values("rule_id", "rule__text", "rule__playbook_id", "rule__archived")
            .annotate(
                times=Count("id"),
                followed_n=Count("id", filter=Q(checked=True)),
                broken_n=Count("id", filter=Q(checked=False)),
                followed_net=Sum("trade__net_pnl", filter=Q(checked=True)),
                broken_net=Sum("trade__net_pnl", filter=Q(checked=False)),
            )
        )
        rules_by_playbook = {}
        for r in per_rule:
            followed_net = r["followed_net"] or reports.ZERO
            broken_net = r["broken_net"] or reports.ZERO
            rules_by_playbook.setdefault(r["rule__playbook_id"], []).append({
                "id": r["rule_id"],
                "text": r["rule__text"],
                "archived": r["rule__archived"],
                "trades": r["times"],
                "followed_trades": r["followed_n"],
                "broken_trades": r["broken_n"],
                "net_when_followed": followed_net,
                "net_when_broken": broken_net,
                "avg_when_followed": _mean(followed_net, r["followed_n"]),
                "avg_when_broken": _mean(broken_net, r["broken_n"]),
            })

        playbooks = []
        for g in grouped:
            followed_net = g["followed_net"] or reports.ZERO
            broken_net = g["broken_net"] or reports.ZERO
            rules = sorted(
                rules_by_playbook.get(g["playbook_id"], []), key=lambda r: r["net_when_broken"]
            )
            playbooks.append({
                "id": g["playbook_id"],
                "name": g["playbook__name"],
                "trade_count": g["n"],
                "unscored_trades": g["n"] - g["scored"],
                "net_pnl": g["net"] or reports.ZERO,
                "adherence_score": _share(g["ticked_sum"] or 0, g["rules_sum"] or 0),
                "followed": {
                    "trades": g["followed_n"],
                    "net_pnl": followed_net,
                    "avg_pnl": _mean(followed_net, g["followed_n"]),
                    "win_rate": _share(g["followed_wins"], g["followed_n"]),
                },
                "broken": {
                    "trades": g["broken_n"],
                    "net_pnl": broken_net,
                    "avg_pnl": _mean(broken_net, g["broken_n"]),
                    "win_rate": _share(g["broken_wins"], g["broken_n"]),
                },
                "rules": rules,
            })
        playbooks.sort(key=lambda p: (-p["trade_count"], p["name"].lower()))
        return Response(reports.jsonable({"currency": currency, "playbooks": playbooks}))