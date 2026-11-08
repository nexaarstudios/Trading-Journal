from django.urls import path
from rest_framework.routers import DefaultRouter

from .adherence_views import AdherenceStreakView
from .analytics_views import AnalyticsReportView, ReportCatalogView, ReportCsvView
from .data_views import DeleteDataView, ExportJournalView, ExportTradesView
from .fx_views import FxRateView
from .goal_views import GoalProgressView, GoalViewSet
from .journal_views import JournalEntryViewSet, JournalStreakView, JournalTemplateViewSet
from .report_views import CalendarView, EquityView, PlaybookReportView, SummaryView, TagReportView
from .risk_views import RiskRuleViewSet, RiskStatusView
from .screenshot_views import ScreenshotFileView, ScreenshotUsageView
from .views import (
    ImportBatchViewSet,
    ImportTemplateViewSet,
    InstrumentSpecViewSet,
    PlaybookRuleViewSet,
    PlaybookViewSet,
    ScreenshotViewSet,
    TagViewSet,
    TradeViewSet,
    TradingAccountViewSet,
)

router = DefaultRouter()
router.register("accounts", TradingAccountViewSet, basename="account")
router.register("specs", InstrumentSpecViewSet, basename="spec")
router.register("trades", TradeViewSet, basename="trade")
router.register("screenshots", ScreenshotViewSet, basename="screenshot")
router.register("tags", TagViewSet, basename="tag")
router.register("playbooks", PlaybookViewSet, basename="playbook")
router.register("playbook-rules", PlaybookRuleViewSet, basename="playbook-rule")
router.register("risk-rules", RiskRuleViewSet, basename="risk-rule")
router.register("goals", GoalViewSet, basename="goal")
router.register("journal/templates", JournalTemplateViewSet, basename="journal-template")
router.register("journal/entries", JournalEntryViewSet, basename="journal-entry")
router.register("imports", ImportBatchViewSet, basename="import")
router.register("import-templates", ImportTemplateViewSet, basename="import-template")

urlpatterns = [
    path("goals/progress/", GoalProgressView.as_view()),  # before the router, or "progress" reads as an id
    path("screenshots/usage/", ScreenshotUsageView.as_view()),  # same reason for "usage"
] + router.urls + [
    path("files/screenshot/", ScreenshotFileView.as_view(), name="screenshot-file"),
    path("adherence/streak/", AdherenceStreakView.as_view()),
    path("fx/rate/", FxRateView.as_view()),
    path("journal/streak/", JournalStreakView.as_view()),
    path("reports/", ReportCatalogView.as_view()),
    path("reports/summary/", SummaryView.as_view()),
    path("reports/equity/", EquityView.as_view()),
    path("reports/calendar/", CalendarView.as_view()),
    path("reports/tags/", TagReportView.as_view()),
    path("reports/playbooks/", PlaybookReportView.as_view()),
    path("reports/<slug:slug>/csv/", ReportCsvView.as_view()),
    path("reports/<slug:slug>/", AnalyticsReportView.as_view()),
    path("risk/status/", RiskStatusView.as_view()),
    path("export/trades/", ExportTradesView.as_view()),
    path("export/journal/", ExportJournalView.as_view()),
    path("data/delete/", DeleteDataView.as_view()),
]