from rest_framework.response import Response
from rest_framework.views import APIView

from . import adherence, reports
from .filters import apply_base_filters, user_zone
from .models import Trade


class AdherenceStreakView(APIView):
    """Trades and days in a row where every playbook rule was followed. A live count, so date filters are ignored."""

    def get(self, request):
        user = request.user
        queryset = Trade.objects.filter(account__user=user, rules_total__gt=0)
        queryset = apply_base_filters(queryset, request.query_params)  # account, playbook, symbol, side, outcome, tag
        rows = [
            (trade_id, closed_at, checked == total)
            for trade_id, closed_at, checked, total in queryset.order_by("closed_at", "id").values_list(
                "id", "closed_at", "rules_checked", "rules_total"
            )
        ]
        return Response(reports.jsonable(adherence.streaks(rows, user_zone(user))))