from bisect import bisect_left, bisect_right
from datetime import datetime, time, timedelta

from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import reports, risk
from .filters import int_param, user_zone
from .models import RiskRule, Trade, TradingAccount
from .serializers import RiskRuleSerializer


class RiskRuleViewSet(viewsets.ModelViewSet):
    serializer_class = RiskRuleSerializer
    pagination_class = None

    def get_queryset(self):
        qs = RiskRule.objects.filter(account__user=self.request.user).select_related("account")
        account = int_param(self.request.query_params, "account")
        if account is not None:
            qs = qs.filter(account_id=account)
        return qs.order_by("account_id", "kind")


def _day(params, tz):
    raw = params.get("date")
    if not raw:
        return datetime.now(tz).date()
    try:
        value = parse_date(raw)
    except ValueError:
        value = None
    if value is None:
        raise ValidationError({"date": "Use the format YYYY-MM-DD."})
    return value


class RiskStatusView(APIView):
    """Where the account stands against its limits, for today or for ?date=YYYY-MM-DD."""

    def get(self, request):
        params = request.query_params
        account_id = int_param(params, "account")
        if account_id is None:
            raise ValidationError({"account": "This parameter is required."})
        account = TradingAccount.objects.filter(user=request.user, pk=account_id).first()
        if account is None:
            raise ValidationError({"account": "Account not found."})

        tz = user_zone(request.user)
        day = _day(params, tz)
        try:
            start = datetime.combine(day, time.min, tzinfo=tz)
            end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
        except OverflowError:
            raise ValidationError({"date": "Date is out of range."})

        # Everything up to the end of that day, oldest first
        rows = list(
            Trade.objects.filter(account=account, closed_at__lt=end)
            .order_by("closed_at", "id")
            .values_list("opened_at", "closed_at", "net_pnl", "risk_amount")
        )
        starting = account.starting_balance
        closes = [row[1] for row in rows]
        nets = [row[2] for row in rows]
        peak, equity, drawdown = risk.current_drawdown(nets, starting)

        cumulative = [reports.ZERO]
        for net in nets:
            cumulative.append(cumulative[-1] + net)

        day_rows = rows[bisect_left(closes, start):]
        risks = []
        for opened, _closed, _net, risk_amount in day_rows:
            if risk_amount is None:  # no stop loss, so nothing to measure
                continue
            # Balance when the trade opened: starting balance plus everything closed by then
            balance = starting + cumulative[bisect_right(closes, opened)]
            risks.append((risk_amount, balance))

        day_net = sum((row[2] for row in day_rows), reports.ZERO)
        rules = RiskRule.objects.filter(account=account, enabled=True).order_by("kind")
        results = risk.evaluate(
            rules, starting_balance=starting, day_net=day_net,
            day_count=len(day_rows), drawdown=drawdown, risks=risks,
        )
        return Response(reports.jsonable({
            "account": account.id,
            "currency": account.currency,
            "date": day,
            "status": risk.worst([r["level"] for r in results]),
            "rules": results,
            "context": {
                "starting_balance": starting,
                "equity": equity,
                "peak_equity": peak,
                "drawdown": drawdown,
                "day_net_pnl": day_net,
                "day_trade_count": len(day_rows),
            },
        }))