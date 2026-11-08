from django.contrib import admin

from . import rollups
from .models import (
    DailyRollup, InstrumentSpec, Playbook, PlaybookRule, RiskRule, Tag, Trade, TradingAccount, JournalEntry, JournalTemplate, Goal, Screenshot
)


@admin.register(TradingAccount)
class TradingAccountAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "account_type", "currency", "starting_balance")
    list_filter = ("account_type", "currency")


@admin.register(InstrumentSpec)
class InstrumentSpecAdmin(admin.ModelAdmin):
    list_display = ("symbol", "asset_class", "contract_size", "pip_size", "quote_currency", "account")
    list_filter = ("asset_class",)
    search_fields = ("symbol",)


@admin.register(Trade)
class TradeAdmin(admin.ModelAdmin):
    list_display = ("symbol", "side", "lots", "closed_at", "gross_pnl", "net_pnl", "r_multiple", "account")
    list_filter = ("side", "account")
    search_fields = ("symbol",)
    date_hierarchy = "closed_at"
    readonly_fields = ("gross_pnl", "net_pnl", "r_multiple", "duration_seconds", "risk_amount", "rules_total", "rules_checked", "rate_source")
    filter_horizontal = ("tags",)

    def delete_queryset(self, request, queryset):
        # "Delete selected" in the admin: refresh the rollups once, not per trade
        with rollups.deferred():
            queryset.delete()


@admin.register(DailyRollup)
class DailyRollupAdmin(admin.ModelAdmin):
    list_display = ("account", "date", "net_pnl", "trade_count", "wins", "losses")
    list_filter = ("account",)
    date_hierarchy = "date"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

@admin.register(Tag)
class TagAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "user")
    list_filter = ("kind",)
    search_fields = ("name",)

class PlaybookRuleInline(admin.TabularInline):
    model = PlaybookRule
    extra = 0


@admin.register(Playbook)
class PlaybookAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "archived")
    list_filter = ("archived",)
    inlines = [PlaybookRuleInline]

@admin.register(RiskRule)
class RiskRuleAdmin(admin.ModelAdmin):
    list_display = ("account", "kind", "limit_value", "unit", "enabled")
    list_filter = ("kind", "enabled")

@admin.register(JournalTemplate)
class JournalTemplateAdmin(admin.ModelAdmin):
    list_display = ("name", "kind", "user")
    list_filter = ("kind",)


@admin.register(JournalEntry)
class JournalEntryAdmin(admin.ModelAdmin):
    list_display = ("date", "kind", "user", "has_content")
    list_filter = ("kind", "has_content")
    date_hierarchy = "date"


@admin.register(Goal)
class GoalAdmin(admin.ModelAdmin):
    list_display = ("kind", "target", "account", "user", "start_month", "end_month")
    list_filter = ("kind",)


@admin.register(Screenshot)
class ScreenshotAdmin(admin.ModelAdmin):
    list_display = ("trade", "content_type", "size", "created_at")
    readonly_fields = ("trade", "key", "content_type", "size", "created_at")

    def has_add_permission(self, request):
        return False  # uploads go through the API, which checks the image