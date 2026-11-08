from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from django.http import Http404
from rest_framework.response import Response
from rest_framework.views import APIView

from . import analytics, reports
from .data_views import csv_response, plain, safe_text
from .filters import date_bounds, user_zone
from .report_views import (
    CalendarView,
    EquityView,
    PlaybookReportView,
    SummaryView,
    TagReportView,
    _context,
    _window,
)

BASE_FILTERS = ("account", "symbol", "side", "outcome", "tag", "playbook")
DATE_FILTERS = ("from", "to")
TEXT_KEYS = {"name", "playbook", "rule", "symbol"}  # typed by users: never allowed to run as a formula

STAT_COLUMNS = (
    ("trades", "Trades"), ("wins", "Wins"), ("losses", "Losses"), ("breakeven", "Breakeven"),
    ("win_rate", "Win rate %"), ("net_pnl", "Net P&L"), ("avg_pnl", "Avg P&L"),
    ("profit_factor", "Profit factor"), ("avg_r", "Avg R"), ("r_trade_count", "Trades with R"),
)
TAG_COLUMNS = (
    ("name", "Tag"), ("trade_count", "Trades"), ("wins", "Wins"), ("win_rate", "Win rate %"),
    ("net_pnl", "Net P&L"), ("money_lost", "Money lost"), ("avg_r", "Avg R"),
    ("r_trade_count", "Trades with R"),
)
PLAYBOOK_COLUMNS = (
    ("playbook", "Playbook"), ("rule", "Rule"), ("trades", "Trades"),
    ("adherence_score", "Adherence %"),
    ("followed_trades", "Followed: trades"), ("followed_net_pnl", "Followed: net P&L"),
    ("followed_avg_pnl", "Followed: avg P&L"), ("followed_win_rate", "Followed: win rate %"),
    ("broken_trades", "Broken: trades"), ("broken_net_pnl", "Broken: net P&L"),
    ("broken_avg_pnl", "Broken: avg P&L"), ("broken_win_rate", "Broken: win rate %"),
)


@dataclass(frozen=True)
class Report:
    slug: str
    title: str
    description: str
    columns: tuple
    build: Callable  # request -> (rows, extra)
    filters: tuple = BASE_FILTERS + DATE_FILTERS
    url: str = ""     # the JSON endpoint of the six older reports; empty = /api/reports/<slug>/
    native: bool = True

    @property
    def endpoint(self):
        return self.url or f"/api/reports/{self.slug}/"


# ---------- the nine reports built here ----------

def _loaded(request):
    accounts, trades, currency = _context(request)
    tz = user_zone(request.user)
    start, end = date_bounds(request.query_params, tz)
    queryset = _window(trades, start, end).order_by("closed_at", "id")
    rows = [
        analytics.Row(*values)
        for values in queryset.values_list(
            "id", "opened_at", "closed_at", "net_pnl", "r_multiple",
            "side", "symbol", "rating", "duration_seconds",
        )
    ]
    return rows, tz, currency


def _grouped(fn, with_tz=False):
    def build(request):
        rows, tz, currency = _loaded(request)
        return (fn(rows, tz) if with_tz else fn(rows)), {"currency": currency}
    return build


def _with_summary(fn, with_tz=False):
    def build(request):
        rows, tz, currency = _loaded(request)
        table, summary = fn(rows, tz) if with_tz else fn(rows)
        return table, {"currency": currency, "summary": summary}
    return build


# ---------- the six older reports, flattened into tables for CSV ----------

def _summary_table(request):
    data = SummaryView().get(request).data
    return [{"metric": k, "value": v} for k, v in data.items()], {"currency": data.get("currency")}


def _equity_table(request):
    data = EquityView().get(request).data
    return data["points"], {"currency": data["currency"]}


def _calendar_table(request):
    data = CalendarView().get(request).data
    return data["days"], {"currency": data["currency"]}


def _tags_table(kind):
    def build(request):
        data = TagReportView(fixed_kind=kind).get(request).data
        return data["tags"], {"currency": data["currency"]}
    return build


def _playbooks_table(request):
    data = PlaybookReportView().get(request).data
    rows = []
    for pb in data["playbooks"]:
        rows.append({
            "playbook": pb["name"], "rule": "", "trades": pb["trade_count"],
            "adherence_score": pb["adherence_score"],
            "followed_trades": pb["followed"]["trades"], "followed_net_pnl": pb["followed"]["net_pnl"],
            "followed_avg_pnl": pb["followed"]["avg_pnl"], "followed_win_rate": pb["followed"]["win_rate"],
            "broken_trades": pb["broken"]["trades"], "broken_net_pnl": pb["broken"]["net_pnl"],
            "broken_avg_pnl": pb["broken"]["avg_pnl"], "broken_win_rate": pb["broken"]["win_rate"],
        })
        for rule in pb["rules"]:
            rows.append({
                "playbook": pb["name"], "rule": rule["text"], "trades": rule["trades"],
                "adherence_score": None,
                "followed_trades": rule["followed_trades"], "followed_net_pnl": rule["net_when_followed"],
                "followed_avg_pnl": rule["avg_when_followed"], "followed_win_rate": None,
                "broken_trades": rule["broken_trades"], "broken_net_pnl": rule["net_when_broken"],
                "broken_avg_pnl": rule["avg_when_broken"], "broken_win_rate": None,
            })
    return rows, {"currency": data["currency"]}


# ---------- the catalog: all 15 core reports ----------

_REPORTS = [
    Report(
        "summary", "Performance summary",
        "Net P&L, win rate, profit factor, expectancy and drawdown for the period.",
        (("metric", "Metric"), ("value", "Value")), _summary_table,
        url="/api/reports/summary/", native=False,
    ),
    Report(
        "equity", "Equity curve and drawdown", "Account equity and the fall from its peak, day by day.",
        (("date", "Date"), ("net_pnl", "Net P&L"), ("trade_count", "Trades"),
         ("equity", "Equity"), ("drawdown", "Drawdown")),
        _equity_table, url="/api/reports/equity/", native=False,
    ),
    Report(
        "calendar", "Calendar", "Daily P&L, trade count and R for one month (?month=YYYY-MM).",
        (("date", "Date"), ("net_pnl", "Net P&L"), ("trade_count", "Trades"),
         ("wins", "Wins"), ("losses", "Losses"), ("r_total", "R total")),
        _calendar_table, filters=BASE_FILTERS + ("month",), url="/api/reports/calendar/", native=False,
    ),
    Report(
        "mistakes", "Mistake report", "Mistake tags ranked by the money lost on trades that carry them.",
        TAG_COLUMNS, _tags_table("mistake"), url="/api/reports/tags/?kind=mistake", native=False,
    ),
    Report(
        "setups", "Setup report", "Setup tags ranked by the money lost on trades that carry them.",
        TAG_COLUMNS, _tags_table("setup"), url="/api/reports/tags/?kind=setup", native=False,
    ),
    Report(
        "emotions", "Emotions report",
        "Emotion tags ranked by the money lost on trades that carry them.",
        TAG_COLUMNS, _tags_table("emotion"), url="/api/reports/tags/?kind=emotion", native=False,
    ),
    Report(
        "playbooks", "Playbook adherence",
        "Rule adherence per playbook, and the P&L of followed versus broken trades.",
        PLAYBOOK_COLUMNS, _playbooks_table, url="/api/reports/playbooks/", native=False,
    ),
    Report(
        "by-weekday", "P&L by day of week", "How each weekday performs, by the day trades closed.",
        (("name", "Day"),) + STAT_COLUMNS, _grouped(analytics.by_weekday, with_tz=True),
    ),
    Report(
        "by-hour", "P&L by hour of day", "Performance by the hour trades were opened, in your timezone.",
        (("label", "Hour"),) + STAT_COLUMNS, _grouped(analytics.by_hour, with_tz=True),
    ),
    Report(
        "by-month", "P&L by month", "Performance month by month, by the day trades closed.",
        (("month", "Month"),) + STAT_COLUMNS, _grouped(analytics.by_month, with_tz=True),
    ),
    Report(
        "by-symbol", "P&L by symbol", "Performance per instrument, best net P&L first.",
        (("symbol", "Symbol"),) + STAT_COLUMNS, _grouped(analytics.by_symbol),
    ),
    Report(
        "long-short", "Long versus short", "Buys against sells.",
        (("side", "Side"),) + STAT_COLUMNS, _grouped(analytics.long_short),
    ),
    Report(
        "holding-time", "Holding time", "Performance by how long trades were held.",
        (("label", "Holding time"),) + STAT_COLUMNS, _with_summary(analytics.holding_time),
    ),
    Report(
        "r-distribution", "R-multiple distribution",
        "How many trades ended in each R range. Only trades with a stop loss are counted.",
        (("label", "R range"), ("trades", "Trades"), ("share", "Share %"), ("net_pnl", "Net P&L")),
        _with_summary(analytics.r_distribution),
    ),
    Report(
        "streaks", "Win and loss streaks", "How often wins and losses came in runs.",
        (("kind", "Streak"), ("length", "Length"), ("count", "Times")),
        _with_summary(analytics.streaks, with_tz=True),
    ),
    Report(
        "by-rating", "Rating versus result", "Whether the trades you rated highest made more money.",
        (("label", "Rating"),) + STAT_COLUMNS, _grouped(analytics.by_rating),
    ),
]
REPORTS = {report.slug: report for report in _REPORTS}


class ReportCatalogView(APIView):
    """All 15 core reports: where to fetch each one, its columns, and the filters it accepts."""

    def get(self, request):
        return Response([
            {
                "slug": r.slug,
                "title": r.title,
                "description": r.description,
                "url": r.endpoint,
                "csv_url": f"/api/reports/{r.slug}/csv/",
                "filters": list(r.filters),
                "columns": [{"key": key, "label": label} for key, label in r.columns],
            }
            for r in _REPORTS
        ])


class AnalyticsReportView(APIView):
    """The nine reports built in this module. The older six keep their own endpoints."""

    def get(self, request, slug):
        report = REPORTS.get(slug)
        if report is None or not report.native:
            raise Http404
        table, extra = report.build(request)
        return Response(reports.jsonable({
            "slug": report.slug,
            "title": report.title,
            "columns": [{"key": key, "label": label} for key, label in report.columns],
            **extra,
            "rows": table,
        }))


class ReportCsvView(APIView):
    """Any of the 15 reports as a CSV download, with the same query parameters."""

    def get(self, request, slug):
        report = REPORTS.get(slug)
        if report is None:
            raise Http404
        table, _extra = report.build(request)
        table = reports.jsonable(table)
        keys = [key for key, _label in report.columns]

        def cells(row):
            return [
                safe_text(row.get(key)) if key in TEXT_KEYS else plain(row.get(key))
                for key in keys
            ]

        today = datetime.now(user_zone(request.user)).date().isoformat()
        return csv_response(f"report-{slug}-{today}.csv", keys, (cells(row) for row in table))