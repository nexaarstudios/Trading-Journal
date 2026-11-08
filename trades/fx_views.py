from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import fx
from .journal_views import _date


def _code(params, name):
    value = (params.get(name) or "").strip().upper()
    if not fx.CODE.match(value):
        raise ValidationError({name: "Use a 3-letter currency code, e.g. USD."})
    return value


class FxRateView(APIView):
    """The rate for converting 1 `from` into `to` on a day, e.g. ?from=JPY&to=USD&date=2026-10-15."""

    throttle_classes = [ScopedRateThrottle]  # a miss can trigger a download, so keep it modest
    throttle_scope = "fx"

    def get(self, request):
        params = request.query_params
        from_ccy, to_ccy = _code(params, "from"), _code(params, "to")
        day = _date(params, "date") or fx.utc_today()
        found = fx.find_rate(from_ccy, to_ccy, day)
        return Response({
            "from": from_ccy,
            "to": to_ccy,
            "date": day.isoformat(),
            "rate": format(found.rate, "f") if found else None,
            "rate_date": found.day.isoformat() if found else None,
        })