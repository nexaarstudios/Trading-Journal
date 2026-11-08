import csv
from datetime import datetime, timezone as dt_tz

from django.db import transaction
from django.http import StreamingHttpResponse
from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from . import rollups
from .filters import apply_base_filters, date_bounds, user_zone
from .journal_views import _date, _kind
from .models import (
    ImportTemplate,
    JournalEntry,
    JournalTemplate,
    Playbook,
    Tag,
    Trade,
    TradingAccount,
    Goal,
    Screenshot,
)

CHUNK = 2000


# ---------- CSV helpers ----------

class Echo:
    """A file-like object whose write() just hands the text back, which is what streaming needs."""

    def write(self, value):
        return value


_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r", "\n")


def safe_text(value):
    """Text typed by users must not run as a spreadsheet formula when the CSV is opened."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_STARTS) else text


def plain(value):
    """Numbers and dates: exact text, blank when missing."""
    return "" if value is None else str(value)


def csv_response(filename, header, rows):
    writer = csv.writer(Echo())

    def stream():
        yield "\ufeff"  # lets Excel read UTF-8 correctly
        yield writer.writerow(header)
        for row in rows:
            yield writer.writerow(row)

    response = StreamingHttpResponse(stream(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "no-store"
    return response


# ---------- exports ----------

TRADE_COLUMNS = [
    "id", "account", "currency", "symbol", "side",
    "opened_at_utc", "closed_at_utc", "closed_date_local",
    "entry_price", "exit_price", "lots", "stop_loss", "take_profit",
    "commission", "swap", "fees", "quote_to_account_rate", "broker_profit",
    "gross_pnl", "net_pnl", "r_multiple", "risk_amount", "duration_seconds",
    "playbook", "rules_checked", "rules_total", "rating", "tags", "notes",
]


def _trade_row(trade, tz):
    tags = sorted(trade.tags.all(), key=lambda t: (t.kind, t.name.lower()))
    return [
        trade.id,
        safe_text(trade.account.name),
        trade.account.currency,
        safe_text(trade.symbol),
        trade.side,
        trade.opened_at.astimezone(dt_tz.utc).isoformat(),
        trade.closed_at.astimezone(dt_tz.utc).isoformat(),
        trade.closed_at.astimezone(tz).date().isoformat(),
        plain(trade.entry_price), plain(trade.exit_price), plain(trade.lots),
        plain(trade.stop_loss), plain(trade.take_profit),
        plain(trade.commission), plain(trade.swap), plain(trade.fees),
        plain(trade.quote_to_account_rate), plain(trade.broker_profit),
        plain(trade.gross_pnl), plain(trade.net_pnl), plain(trade.r_multiple),
        plain(trade.risk_amount), trade.duration_seconds,
        safe_text(trade.playbook.name) if trade.playbook_id else "",
        trade.rules_checked, trade.rules_total, plain(trade.rating),
        safe_text("; ".join(f"{t.kind}:{t.name}" for t in tags)),
        safe_text(trade.notes),
    ]


class ExportTradesView(APIView):
    """Download trades as CSV. Takes the same filters as the trade log."""

    def get(self, request):
        user = request.user
        tz = user_zone(user)
        params = request.query_params

        qs = (
            Trade.objects.filter(account__user=user)
            .select_related("account", "playbook")
            .prefetch_related("tags")
        )
        qs = apply_base_filters(qs, params)
        start, end = date_bounds(params, tz)
        if start:
            qs = qs.filter(closed_at__gte=start)
        if end:
            qs = qs.filter(closed_at__lt=end)
        qs = qs.order_by("closed_at", "id")

        rows = (_trade_row(trade, tz) for trade in qs.iterator(chunk_size=CHUNK))
        filename = f"trades-{datetime.now(tz).date().isoformat()}.csv"
        return csv_response(filename, TRADE_COLUMNS, rows)


JOURNAL_COLUMNS = ["date", "kind", "section", "prompt", "answer"]


class ExportJournalView(APIView):
    """Download journal entries as CSV, one row per prompt and answer."""

    def get(self, request):
        params = request.query_params
        qs = JournalEntry.objects.filter(user=request.user)
        if kind := _kind(params):
            qs = qs.filter(kind=kind)
        if first := _date(params, "from"):
            qs = qs.filter(date__gte=first)
        if last := _date(params, "to"):
            qs = qs.filter(date__lte=last)
        qs = qs.order_by("date", "kind")

        def rows():
            for entry in qs.iterator(chunk_size=CHUNK):
                day = entry.date.isoformat()
                if not entry.sections:
                    yield [day, entry.kind, "", "", ""]
                    continue
                for number, section in enumerate(entry.sections, start=1):
                    yield [
                        day, entry.kind, number,
                        safe_text(section.get("prompt")), safe_text(section.get("answer")),
                    ]

        tz = user_zone(request.user)
        filename = f"journal-{datetime.now(tz).date().isoformat()}.csv"
        return csv_response(filename, JOURNAL_COLUMNS, rows())


# ---------- deleting data ----------

class DeleteDataSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    scope = serializers.ChoiceField(choices=["data", "account"])
    confirm = serializers.CharField()

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Incorrect password.")
        return value

    def validate_confirm(self, value):
        if value != "DELETE":
            raise serializers.ValidationError("Type DELETE to confirm.")
        return value


def wipe_user_data(user):
    """Delete everything the user created. Returns what was removed, for the confirmation."""
    counts = {
        "accounts": TradingAccount.objects.filter(user=user).count(),
        "trades": Trade.objects.filter(account__user=user).count(),
        "tags": Tag.objects.filter(user=user).count(),
        "playbooks": Playbook.objects.filter(user=user).count(),
        "journal_entries": JournalEntry.objects.filter(user=user).count(),
        "journal_templates": JournalTemplate.objects.filter(user=user).count(),
        "import_templates": ImportTemplate.objects.filter(user=user).count(),
        "goals": Goal.objects.filter(user=user).count(),
        "screenshots": Screenshot.objects.filter(trade__account__user=user).count(),
    }
    with transaction.atomic(), rollups.deferred():
        # Accounts go first. Their trades hold the rule ticks that would block deleting playbooks.
        # Trades, rollups, risk rules, import batches, spec overrides and account goals go with them.
        TradingAccount.objects.filter(user=user).delete()
        Playbook.objects.filter(user=user).delete()
        Tag.objects.filter(user=user).delete()
        JournalEntry.objects.filter(user=user).delete()
        JournalTemplate.objects.filter(user=user).delete()
        ImportTemplate.objects.filter(user=user).delete()
        Goal.objects.filter(user=user).delete()  # the ones that cover every account
    return counts


class DeleteDataView(APIView):
    throttle_classes = [ScopedRateThrottle]  # the password check must not be guessable at speed
    throttle_scope = "auth"

    def post(self, request):
        form = DeleteDataSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        scope = form.validated_data["scope"]
        user = request.user

        with transaction.atomic():
            counts = wipe_user_data(user)
            if scope == "account":
                user.delete()  # their tokens stop working at once: the user no longer exists
        return Response({"scope": scope, "deleted": counts})