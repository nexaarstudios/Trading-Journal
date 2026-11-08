from datetime import timezone as dt_timezone
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.core.validators import MaxValueValidator, MinValueValidator

from .pnl import PnLError, compute_pnl

class RuleCheckError(ValueError):
    """The ticked rules don't fit this trade's playbook."""


class TradingAccount(models.Model):
    class AccountType(models.TextChoices):
        LIVE = "live", "Live"
        DEMO = "demo", "Demo"
        PROP = "prop", "Prop firm"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="trading_accounts"
    )
    name = models.CharField(max_length=100)
    account_type = models.CharField(
        max_length=10, choices=AccountType.choices, default=AccountType.LIVE
    )
    starting_balance = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")  # ISO code, e.g. USD
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_account_name_per_user"),
        ]

    def __str__(self):
        return f"{self.name} ({self.currency})"


class InstrumentSpec(models.Model):
    class AssetClass(models.TextChoices):
        FOREX = "forex", "Forex"
        METAL = "metal", "Metal"
        INDEX = "index", "Index"
        FUTURES = "futures", "Futures"

    # account = null means the global default; set means a per-account override
    account = models.ForeignKey(
        TradingAccount,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="instrument_overrides",
    )
    symbol = models.CharField(max_length=20)
    asset_class = models.CharField(max_length=10, choices=AssetClass.choices)
    base_currency = models.CharField(max_length=3)
    quote_currency = models.CharField(max_length=3)
    contract_size = models.DecimalField(max_digits=18, decimal_places=4)
    pip_size = models.DecimalField(max_digits=10, decimal_places=6)

    class Meta:
        ordering = ["symbol"]
        constraints = [
            # one override per account per symbol
            models.UniqueConstraint(fields=["account", "symbol"], name="unique_spec_per_account"),
            # one global default per symbol (a plain unique constraint ignores NULLs)
            models.UniqueConstraint(
                fields=["symbol"],
                condition=Q(account__isnull=True),
                name="unique_global_spec_per_symbol",
            ),
        ]

    def __str__(self):
        return self.symbol if self.account_id is None else f"{self.symbol} (override)"


class Trade(models.Model):
    class Side(models.TextChoices):
        LONG = "long", "Long"
        SHORT = "short", "Short"

    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name="trades")
    symbol = models.CharField(max_length=20)
    side = models.CharField(max_length=5, choices=Side.choices)
    opened_at = models.DateTimeField()
    closed_at = models.DateTimeField()

    entry_price = models.DecimalField(max_digits=18, decimal_places=6)
    exit_price = models.DecimalField(max_digits=18, decimal_places=6)
    lots = models.DecimalField(max_digits=12, decimal_places=4)
    stop_loss = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)
    take_profit = models.DecimalField(max_digits=18, decimal_places=6, null=True, blank=True)

    # Costs in account currency. Positive = cost. A negative swap is a credit.
    commission = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    swap = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    fees = models.DecimalField(max_digits=18, decimal_places=2, default=0)

    # Needed only for crosses (e.g. GBPJPY on a USD account): JPY->USD at close
    quote_to_account_rate = models.DecimalField(
        max_digits=18, decimal_places=8, null=True, blank=True
    )
    # Who supplied the rate: "manual" (typed or imported), "auto" (from the FX table), "" (not needed)
    rate_source = models.CharField(max_length=6, blank=True, default="", editable=False)
    # Set by CSV import. When present it replaces the computed gross P&L.
    broker_profit = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)

    # Computed on every save, never typed in
    gross_pnl = models.DecimalField(max_digits=18, decimal_places=2, default=0, editable=False)
    net_pnl = models.DecimalField(max_digits=18, decimal_places=2, default=0, editable=False)
    r_multiple = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True, editable=False
    )
    duration_seconds = models.PositiveIntegerField(default=0, editable=False)
    risk_amount = models.DecimalField(
        max_digits=18, decimal_places=2, null=True, blank=True, editable=False
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    import_batch = models.ForeignKey(
        "ImportBatch",
        null=True,
        blank=True,
        editable=False,
        on_delete=models.SET_NULL,
        related_name="trades",
    )
    tags = models.ManyToManyField("Tag", blank=True, related_name="trades")
    playbook = models.ForeignKey(
        "Playbook", null=True, blank=True, on_delete=models.SET_NULL, related_name="trades"
    )
    rating = models.PositiveSmallIntegerField(
        null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    notes = models.TextField(blank=True)
    # How many of this trade's playbook rules were ticked. Stored so reports stay fast.
    rules_total = models.PositiveSmallIntegerField(default=0, editable=False)
    rules_checked = models.PositiveSmallIntegerField(default=0, editable=False)

    class Meta:
        ordering = ["-closed_at"]
        indexes = [
            models.Index(fields=["account", "-closed_at"]),
            models.Index(fields=["account", "symbol"]),
        ]

    def __str__(self):
        return f"{self.symbol} {self.side} {self.lots} ({self.net_pnl})"

    def _spec(self):
        symbol = self.symbol.strip().upper()
        spec = InstrumentSpec.objects.filter(account=self.account, symbol=symbol).first()
        if spec is None:
            spec = InstrumentSpec.objects.filter(account__isnull=True, symbol=symbol).first()
        if spec is None:
            raise PnLError(f"No instrument spec for {symbol}. Add it in Instrument specs.")
        return spec

    def _resolve_rate(self, spec):
        """Fill in the quote-to-account rate for cross pairs, unless the user typed one."""
        from . import fx

        if self.quote_to_account_rate is not None and self.rate_source != "auto":
            self.rate_source = "manual"
            return
        self.quote_to_account_rate, self.rate_source = None, ""
        currency = self.account.currency
        if not fx.needs_rate(spec, currency):
            return
        day = self.closed_at.astimezone(dt_timezone.utc).date()
        found = fx.find_rate(spec.quote_currency, currency, day)
        if found:
            self.quote_to_account_rate, self.rate_source = found.rate, "auto"

    def _compute(self):
        spec = self._spec()
        self._resolve_rate(spec)
        return compute_pnl(
            side=self.side,
            entry=self.entry_price,
            exit_price=self.exit_price,
            lots=self.lots,
            contract_size=spec.contract_size,
            base_currency=spec.base_currency,
            quote_currency=spec.quote_currency,
            account_currency=self.account.currency,
            commission=self.commission,
            swap=self.swap,
            fees=self.fees,
            stop=self.stop_loss,
            quote_to_account_rate=self.quote_to_account_rate,
            broker_profit=self.broker_profit,
        )

    def clean(self):
        required = (
            self.account_id, self.symbol, self.side, self.opened_at,
            self.closed_at, self.entry_price, self.exit_price, self.lots,
        )
        if any(v is None or v == "" for v in required):
            return  # field-level errors will already be reported
        if self.closed_at < self.opened_at:
            raise ValidationError({"closed_at": "Close time cannot be before open time."})
        try:
            self._compute()
        except PnLError as exc:
            raise ValidationError(str(exc))

    def apply_result(self, result):
        self.gross_pnl = result.gross
        self.net_pnl = result.net
        self.r_multiple = result.r_multiple
        self.risk_amount = result.risk
        self.duration_seconds = int((self.closed_at - self.opened_at).total_seconds())

    def save(self, *args, **kwargs):
        self.symbol = self.symbol.strip().upper()
        self.apply_result(self._compute())
        super().save(*args, **kwargs)

    def sync_rules(self, checked_ids=None):
        """Snapshot the playbook's active rules on this trade and tick the given ones.

        checked_ids=None leaves existing ticks alone. A list sets exactly those ticked.
        """
        checks = {c.rule_id: c for c in TradeRuleCheck.objects.filter(trade=self).select_related("rule")}

        if self.playbook_id is None:
            if checked_ids:
                raise RuleCheckError("Choose a playbook before ticking rules.")
            TradeRuleCheck.objects.filter(trade=self).delete()
            checks = {}
        else:
            if any(c.rule.playbook_id != self.playbook_id for c in checks.values()):
                TradeRuleCheck.objects.filter(trade=self).delete()  # playbook changed: new snapshot
                checks = {}
            if not checks:
                rules = list(self.playbook.rules.filter(archived=False))
                TradeRuleCheck.objects.bulk_create([TradeRuleCheck(trade=self, rule=r) for r in rules])
                checks = {
                    c.rule_id: c
                    for c in TradeRuleCheck.objects.filter(trade=self).select_related("rule")
                }
            if checked_ids is not None:
                wanted = set(checked_ids)
                if wanted - set(checks):
                    raise RuleCheckError("Some rules do not belong to this trade's playbook.")
                for rule_id, check in checks.items():
                    check.checked = rule_id in wanted
                TradeRuleCheck.objects.bulk_update(list(checks.values()), ["checked"])

        total = len(checks)
        ticked = sum(1 for c in checks.values() if c.checked)
        self.rules_total, self.rules_checked = total, ticked
        Trade.objects.filter(pk=self.pk).update(rules_total=total, rules_checked=ticked)


class ImportTemplate(models.Model):
    """A saved column mapping, so the next file from the same broker needs no setup."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="import_templates"
    )
    name = models.CharField(max_length=100)
    mapping = models.JSONField(default=dict)  # {"symbol": "Item", "side": "Type", ...}
    timezone = models.CharField(max_length=64, default="UTC")
    date_format = models.CharField(max_length=50, blank=True)  # strptime format, blank = auto
    decimal_separator = models.CharField(max_length=1, blank=True, default="")  # "" means detect from the data
    costs_as_negative = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_template_name_per_user"),
        ]

    def __str__(self):
        return self.name


class ImportBatch(models.Model):
    class Status(models.TextChoices):
        COMPLETED = "completed", "Completed"
        UNDONE = "undone", "Undone"

    account = models.ForeignKey(
        TradingAccount, on_delete=models.CASCADE, related_name="import_batches"
    )
    template = models.ForeignKey(
        ImportTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="batches"
    )
    filename = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.COMPLETED)
    total_rows = models.PositiveIntegerField(default=0)
    imported_count = models.PositiveIntegerField(default=0)
    duplicate_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.filename or 'import'} ({self.imported_count} trades)"

class DailyRollup(models.Model):
    """One account's totals for one day, in the owner's timezone. Rebuilt from trades."""

    account = models.ForeignKey(
        TradingAccount, on_delete=models.CASCADE, related_name="daily_rollups"
    )
    date = models.DateField()
    net_pnl = models.DecimalField(max_digits=18, decimal_places=2, default=0)
    trade_count = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    r_total = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    class Meta:
        ordering = ["date"]
        constraints = [
            models.UniqueConstraint(fields=["account", "date"], name="unique_rollup_per_day"),
        ]

    def __str__(self):
        return f"{self.account_id} {self.date}: {self.net_pnl}"

class Tag(models.Model):
    class Kind(models.TextChoices):
        SETUP = "setup", "Setup"
        MISTAKE = "mistake", "Mistake"
        CONDITION = "condition", "Market condition"
        EMOTION = "emotion", "Emotion"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="tags"
    )
    name = models.CharField(max_length=50)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "kind", "name"], name="unique_tag_per_user_kind"),
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"

class Playbook(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="playbooks"
    )
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    archived = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["user", "name"], name="unique_playbook_name_per_user"),
        ]

    def __str__(self):
        return self.name


class PlaybookRule(models.Model):
    playbook = models.ForeignKey(Playbook, on_delete=models.CASCADE, related_name="rules")
    text = models.CharField(max_length=200)
    position = models.PositiveIntegerField(default=0)
    archived = models.BooleanField(default=False)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return self.text


class TradeRuleCheck(models.Model):
    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name="rule_checks")
    # RESTRICT: a rule that trades depend on can't be hard-deleted on its own,
    # but it still goes when its whole playbook or user is deleted together with the trades.
    rule = models.ForeignKey(PlaybookRule, on_delete=models.RESTRICT, related_name="checks")
    checked = models.BooleanField(default=False)

    class Meta:
        ordering = ["rule__position", "rule_id"]
        constraints = [
            models.UniqueConstraint(fields=["trade", "rule"], name="unique_check_per_trade_rule"),
        ]

class RiskRule(models.Model):
    class Kind(models.TextChoices):
        DAILY_LOSS = "daily_loss", "Daily loss"
        MAX_DRAWDOWN = "max_drawdown", "Total drawdown"
        TRADES_PER_DAY = "trades_per_day", "Trades per day"
        RISK_PER_TRADE = "risk_per_trade", "Risk per trade"

    class Unit(models.TextChoices):
        MONEY = "money", "Money"
        PERCENT = "percent", "Percent"
        COUNT = "count", "Count"

    account = models.ForeignKey(TradingAccount, on_delete=models.CASCADE, related_name="risk_rules")
    kind = models.CharField(max_length=20, choices=Kind.choices)
    unit = models.CharField(max_length=10, choices=Unit.choices, default=Unit.MONEY)
    limit_value = models.DecimalField(max_digits=18, decimal_places=2)
    enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["account_id", "kind"]
        constraints = [
            models.UniqueConstraint(fields=["account", "kind"], name="unique_risk_rule_per_account_kind"),
        ]

    def __str__(self):
        return f"{self.kind} {self.limit_value} {self.unit}"

class JournalTemplate(models.Model):
    class Kind(models.TextChoices):
        PRE_MARKET = "pre_market", "Pre-market"
        POST_MARKET = "post_market", "Post-market"
        WEEKLY = "weekly", "Weekly review"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="journal_templates"
    )
    kind = models.CharField(max_length=12, choices=Kind.choices)
    name = models.CharField(max_length=100)
    prompts = models.JSONField(default=list)  # ["What are you watching?", ...]
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "kind", "name"], name="unique_journal_template_per_user_kind"
            ),
        ]

    def __str__(self):
        return f"{self.name} ({self.kind})"


class JournalEntry(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="journal_entries"
    )
    kind = models.CharField(max_length=12, choices=JournalTemplate.Kind.choices)
    date = models.DateField()  # the user's local day; weekly reviews use that week's Monday
    template = models.ForeignKey(
        JournalTemplate, null=True, blank=True, on_delete=models.SET_NULL, related_name="entries"
    )
    sections = models.JSONField(default=list)  # [{"prompt": "...", "answer": "..."}]
    has_content = models.BooleanField(default=False, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "kind"]
        verbose_name_plural = "journal entries"
        constraints = [
            models.UniqueConstraint(fields=["user", "kind", "date"], name="unique_journal_entry_per_day"),
        ]
        indexes = [models.Index(fields=["user", "date"])]

    def __str__(self):
        return f"{self.kind} {self.date}"

    def save(self, *args, **kwargs):
        self.has_content = any((s.get("answer") or "").strip() for s in self.sections)
        super().save(*args, **kwargs)


class FxRate(models.Model):
    """A daily reference rate: 1 unit of base_currency is worth `rate` units of quote_currency."""

    base_currency = models.CharField(max_length=3)
    quote_currency = models.CharField(max_length=3)
    date = models.DateField()
    rate = models.DecimalField(max_digits=18, decimal_places=8)
    source = models.CharField(max_length=20, default="frankfurter")

    class Meta:
        ordering = ["base_currency", "quote_currency", "-date"]
        constraints = [
            models.UniqueConstraint(
                fields=["base_currency", "quote_currency", "date"], name="unique_fx_rate_per_day"
            ),
        ]

    def __str__(self):
        return f"{self.base_currency}/{self.quote_currency} {self.date}: {self.rate}"


class Goal(models.Model):
    """A monthly target. start_month and end_month are the first day of a month; no end means it repeats."""

    class Kind(models.TextChoices):
        NET_PNL = "net_pnl", "Net P&L"
        WIN_RATE = "win_rate", "Win rate"
        JOURNAL_DAYS = "journal_days", "Days journaled"
        RULE_ADHERENCE = "rule_adherence", "Rule adherence"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="goals")
    account = models.ForeignKey(
        TradingAccount, null=True, blank=True, on_delete=models.CASCADE, related_name="goals"
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    target = models.DecimalField(max_digits=18, decimal_places=2)
    start_month = models.DateField()
    end_month = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["kind", "start_month", "id"]

    def __str__(self):
        return f"{self.kind} {self.target} from {self.start_month:%Y-%m}"


class Screenshot(models.Model):
    trade = models.ForeignKey(Trade, on_delete=models.CASCADE, related_name="screenshots")
    key = models.CharField(max_length=255, unique=True, editable=False)  # where the file lives in storage
    content_type = models.CharField(max_length=20, editable=False)
    size = models.PositiveIntegerField(editable=False)
    width = models.PositiveIntegerField(default=0, editable=False)
    height = models.PositiveIntegerField(default=0, editable=False)
    thumb_key = models.CharField(max_length=255, blank=True, default="", editable=False)
    caption = models.CharField(max_length=200, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position", "id"]

    def __str__(self):
        return f"{self.content_type} {self.size} bytes ({self.key})"