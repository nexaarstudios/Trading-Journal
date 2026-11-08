from datetime import datetime
from decimal import Decimal

from django.db.models import Count, Q, Sum
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import goals, journal, reports
from .filters import int_param, user_zone
from .goal_serializers import GoalSerializer
from .journal_views import _date
from .models import Goal, JournalEntry, Trade, TradingAccount


class GoalViewSet(viewsets.ModelViewSet):
    serializer_class = GoalSerializer
    pagination_class = None

    def get_queryset(self):
        return Goal.objects.filter(user=self.request.user).select_related("account")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


def _month(params, today):
    raw = params.get("month")
    if not raw:
        return today.replace(day=1)
    try:
        return datetime.strptime(raw, "%Y-%m").date()
    except ValueError:
        raise ValidationError({"month": "Use the format YYYY-MM."})


def _measure(goal, user, first, following, start, end):
    """The value a goal is measured on for one month, or None when there is nothing to measure."""
    if goal.kind == Goal.Kind.JOURNAL_DAYS:
        days = (
            JournalEntry.objects.filter(
                user=user, has_content=True, kind__in=journal.DAILY_KINDS,
                date__gte=first, date__lt=following,
            )
            .order_by()
            .values("date")
            .distinct()
            .count()
        )
        return Decimal(days)

    trades = Trade.objects.filter(account__user=user, closed_at__gte=start, closed_at__lt=end)
    if goal.account_id:
        trades = trades.filter(account_id=goal.account_id)

    if goal.kind == Goal.Kind.NET_PNL:
        return trades.aggregate(total=Sum("net_pnl"))["total"] or reports.ZERO
    if goal.kind == Goal.Kind.WIN_RATE:
        agg = trades.aggregate(n=Count("id"), wins=Count("id", filter=Q(net_pnl__gt=0)))
        return Decimal(agg["wins"]) * 100 / agg["n"] if agg["n"] else None
    agg = trades.filter(rules_total__gt=0).aggregate(
        total=Sum("rules_total"), ticked=Sum("rules_checked")
    )
    return Decimal(agg["ticked"]) * 100 / agg["total"] if agg["total"] else None


class GoalProgressView(APIView):
    """How each goal that covers a month is going. Defaults to the current month."""

    def get(self, request):
        params = request.query_params
        tz = user_zone(request.user)
        today = _date(params, "date") or datetime.now(tz).date()
        first = _month(params, today)
        try:
            following = goals.first_of_next_month(first)
            start, end = goals.month_bounds(first, tz)
        except OverflowError:
            raise ValidationError({"month": "Date is out of range."})

        queryset = Goal.objects.filter(user=request.user).select_related("account")
        account_id = int_param(params, "account")
        if account_id is not None:
            if not TradingAccount.objects.filter(user=request.user, pk=account_id).exists():
                raise ValidationError({"account": "Account not found."})
            # That account's goals, plus the ones that cover every account
            queryset = queryset.filter(Q(account_id=account_id) | Q(account__isnull=True))

        results = []
        for goal in queryset:
            if not goals.active_in(goal.start_month, goal.end_month, first):
                continue
            current = _measure(goal, request.user, first, following, start, end)
            results.append({
                "id": goal.id,
                "kind": goal.kind,
                "account": goal.account_id,
                "currency": goal.account.currency if goal.account_id else None,
                "target": goal.target,
                "current": current,
                **goals.evaluate(goal.kind, goal.target, current, first, today),
            })
        return Response(reports.jsonable({
            "month": first.strftime("%Y-%m"),
            "today": today,
            "goals": results,
        }))