from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction
from rest_framework import serializers
from rest_framework.fields import empty

from .importer import FIELDS
from .models import (
    ImportBatch,
    ImportTemplate,
    InstrumentSpec,
    Playbook,
    PlaybookRule,
    RiskRule,
    RuleCheckError,
    Tag,
    Trade,
    TradeRuleCheck,
    TradingAccount,
)
from .pnl import PnLError

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_ID = 9_223_372_036_854_775_807


# ---------- shared validators ----------

def _check_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise serializers.ValidationError("Use an IANA timezone name, e.g. Africa/Lagos.")
    return value


def _check_date_format(value):
    if value:
        try:
            sample = datetime(2026, 10, 15, 14, 30, 45).strftime(value)
            datetime.strptime(sample, value)
        except ValueError:
            raise serializers.ValidationError(
                "Not a valid date format. Example: %d/%m/%Y %H:%M"
            )
    return value


def _check_mapping(value):
    if not isinstance(value, dict):
        raise serializers.ValidationError("Mapping must be an object of field: column name.")
    for key, column in value.items():
        if key not in FIELDS:
            raise serializers.ValidationError(f"Unknown field '{key}'.")
        if column is not None and not isinstance(column, str):
            raise serializers.ValidationError(f"The column for '{key}' must be text.")
    return value


# ---------- accounts and specs ----------

class TradingAccountSerializer(serializers.ModelSerializer):
    class Meta:
        model = TradingAccount
        fields = ["id", "name", "account_type", "starting_balance", "currency", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        user = self.context["request"].user
        qs = TradingAccount.objects.filter(user=user, name=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("You already have an account with this name.")
        return value

    def validate_currency(self, value):
        value = value.upper()
        if len(value) != 3 or not value.isalpha():
            raise serializers.ValidationError("Use a 3-letter currency code, e.g. USD.")
        return value


class InstrumentSpecSerializer(serializers.ModelSerializer):
    class Meta:
        model = InstrumentSpec
        fields = [
            "id", "symbol", "asset_class", "base_currency",
            "quote_currency", "contract_size", "pip_size", "account",
        ]


# ---------- trades ----------

class TagBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tag
        fields = ["id", "name", "kind"]


class TradeRuleCheckSerializer(serializers.ModelSerializer):
    rule = serializers.IntegerField(source="rule_id", read_only=True)
    text = serializers.CharField(source="rule.text", read_only=True)

    class Meta:
        model = TradeRuleCheck
        fields = ["rule", "text", "checked"]


class TradeSerializer(serializers.ModelSerializer):
    tags = TagBriefSerializer(many=True, read_only=True)
    tag_ids = serializers.PrimaryKeyRelatedField(
        many=True, queryset=Tag.objects.none(), source="tags", write_only=True, required=False
    )
    rule_checks = TradeRuleCheckSerializer(many=True, read_only=True)
    screenshot_count = serializers.SerializerMethodField()
    checked_rule_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_ID),
        write_only=True, required=False, max_length=200,
    )

    class Meta:
        model = Trade
        fields = [
            "id", "account", "symbol", "side", "opened_at", "closed_at",
            "entry_price", "exit_price", "lots", "stop_loss", "take_profit",
            "commission", "swap", "fees", "quote_to_account_rate", "rate_source", "broker_profit",
            "gross_pnl", "net_pnl", "r_multiple", "risk_amount", "duration_seconds",
            "playbook", "rating", "notes", "rules_total", "rules_checked",
            "rule_checks", "checked_rule_ids", "screenshot_count",
            "tags", "tag_ids", "import_batch", "created_at", "updated_at",
        ]
        read_only_fields = [
            "id", "gross_pnl", "net_pnl", "r_multiple", "risk_amount", "duration_seconds",
            "rules_total", "rules_checked", "import_batch", "created_at", "updated_at", "rate_source",
        ]
        extra_kwargs = {"notes": {"max_length": 10_000}}


    def get_screenshot_count(self, obj):
        return len(obj.screenshots.all())  # the trade log prefetches them


    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            # A user can only attach trades to their own accounts, tags and playbooks
            fields["account"].queryset = TradingAccount.objects.filter(user=request.user)
            fields["tag_ids"].child_relation.queryset = Tag.objects.filter(user=request.user)
            fields["playbook"].queryset = Playbook.objects.filter(user=request.user, archived=False)
        return fields

    def validate(self, attrs):
        opened = attrs.get("opened_at", getattr(self.instance, "opened_at", None))
        closed = attrs.get("closed_at", getattr(self.instance, "closed_at", None))
        if opened and closed and closed < opened:
            raise serializers.ValidationError({"closed_at": "Close time cannot be before open time."})
        return attrs

    def _sync(self, trade, checked):
        try:
            trade.sync_rules(checked)
        except RuleCheckError as exc:
            raise serializers.ValidationError({"checked_rule_ids": str(exc)})

    @staticmethod
    def _flag_rate_source(data):
        # A rate the user sends is a manual override. Sending null hands the rate back to the FX table.
        if "quote_to_account_rate" in data:
            data["rate_source"] = "manual" if data["quote_to_account_rate"] is not None else ""

    def create(self, validated_data):
        checked = validated_data.pop("checked_rule_ids", None)
        self._flag_rate_source(validated_data)
        try:
            with transaction.atomic():  # a bad tick rolls the whole trade back
                trade = super().create(validated_data)
                self._sync(trade, checked)
        except PnLError as exc:
            raise serializers.ValidationError({"detail": str(exc)})
        return trade

    def update(self, instance, validated_data):
        checked = validated_data.pop("checked_rule_ids", None)
        self._flag_rate_source(validated_data)
        resync = checked is not None or "playbook" in validated_data
        try:
            with transaction.atomic():
                trade = super().update(instance, validated_data)
                if resync:
                    self._sync(trade, checked)
        except PnLError as exc:
            raise serializers.ValidationError({"detail": str(exc)})
        return trade


# ---------- imports ----------

class ImportTemplateSerializer(serializers.ModelSerializer):
    decimal_separator = serializers.ChoiceField(
        choices=[".", ","], required=False, allow_blank=True
    )

    class Meta:
        model = ImportTemplate
        fields = [
            "id", "name", "mapping", "timezone", "date_format",
            "decimal_separator", "costs_as_negative", "created_at",
        ]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        user = self.context["request"].user
        qs = ImportTemplate.objects.filter(user=user, name=value)
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("You already have a template with this name.")
        return value

    def validate_mapping(self, value):
        return _check_mapping(value)

    def validate_timezone(self, value):
        return _check_timezone(value)

    def validate_date_format(self, value):
        return _check_date_format(value)


class ImportBatchSerializer(serializers.ModelSerializer):
    class Meta:
        model = ImportBatch
        fields = [
            "id", "account", "template", "filename", "status", "total_rows",
            "imported_count", "duplicate_count", "error_count", "created_at",
        ]
        read_only_fields = fields


class ImportPreviewTradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trade
        fields = [
            "symbol", "side", "opened_at", "closed_at", "entry_price", "exit_price",
            "lots", "commission", "swap", "gross_pnl", "net_pnl", "r_multiple",
        ]
        read_only_fields = fields


class OptionalBooleanField(serializers.BooleanField):
    # Multipart forms omit unticked booleans; treat "absent" as "not provided"
    # instead of False, so a template's value can apply.
    default_empty_html = empty


MAX_PASTE_CHARS = 1_000_000


class ImportRequestSerializer(serializers.Serializer):
    file = serializers.FileField(required=False)
    text = serializers.CharField(
        required=False, allow_blank=True, trim_whitespace=False, max_length=MAX_PASTE_CHARS
    )
    has_header = OptionalBooleanField(required=False)
    account = serializers.PrimaryKeyRelatedField(queryset=TradingAccount.objects.none())
    template = serializers.PrimaryKeyRelatedField(
        queryset=ImportTemplate.objects.none(), required=False, allow_null=True
    )
    mapping = serializers.JSONField(required=False)
    timezone = serializers.CharField(required=False)
    date_format = serializers.CharField(required=False, allow_blank=True)
    decimal_separator = serializers.ChoiceField(choices=[".", ","], required=False)
    costs_as_negative = OptionalBooleanField(required=False)
    save_template_name = serializers.CharField(required=False, allow_blank=True, max_length=100)

    def get_fields(self):
        fields = super().get_fields()
        user = self.context["request"].user
        fields["account"].queryset = TradingAccount.objects.filter(user=user)
        fields["template"].queryset = ImportTemplate.objects.filter(user=user)
        return fields

    def validate(self, attrs):
        has_file = "file" in attrs
        has_text = bool((attrs.get("text") or "").strip())
        if has_file == has_text:
            raise serializers.ValidationError("Send either a file or pasted text.")
        return attrs

    def validate_file(self, value):
        if value.size > MAX_UPLOAD_BYTES:
            raise serializers.ValidationError("Files are limited to 10 MB.")
        return value

    def validate_mapping(self, value):
        return _check_mapping(value)

    def validate_timezone(self, value):
        return _check_timezone(value)

    def validate_date_format(self, value):
        return _check_date_format(value)


# ---------- tags and bulk edit ----------

class TagSerializer(serializers.ModelSerializer):
    trade_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Tag
        fields = ["id", "name", "kind", "trade_count"]

    def validate(self, attrs):
        user = self.context["request"].user
        name = attrs.get("name", getattr(self.instance, "name", None))
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        clash = Tag.objects.filter(user=user, kind=kind, name__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"name": "You already have a tag with this name and kind."}
            )
        return attrs


class BulkTradeSerializer(serializers.Serializer):
    ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_ID),
        min_length=1, max_length=5000,
    )
    action = serializers.ChoiceField(choices=["add_tags", "remove_tags", "delete"])
    tag_ids = serializers.ListField(
        child=serializers.IntegerField(min_value=1, max_value=MAX_ID),
        required=False, max_length=100,
    )

    def validate(self, attrs):
        if attrs["action"] == "delete":
            attrs["tag_ids"] = []
            return attrs
        wanted = set(attrs.get("tag_ids", []))
        if not wanted:
            raise serializers.ValidationError({"tag_ids": "Choose at least one tag."})
        user = self.context["request"].user
        found = set(Tag.objects.filter(user=user, id__in=wanted).values_list("id", flat=True))
        if found != wanted:
            raise serializers.ValidationError({"tag_ids": "Some tags were not found."})
        attrs["tag_ids"] = sorted(wanted)
        return attrs


# ---------- playbooks ----------

class PlaybookRuleBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlaybookRule
        fields = ["id", "text", "position"]


class PlaybookSerializer(serializers.ModelSerializer):
    rules = PlaybookRuleBriefSerializer(many=True, read_only=True)
    trade_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Playbook
        fields = ["id", "name", "description", "archived", "rules", "trade_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_name(self, value):
        user = self.context["request"].user
        clash = Playbook.objects.filter(user=user, name__iexact=value)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError("You already have a playbook with this name.")
        return value


class PlaybookRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = PlaybookRule
        fields = ["id", "playbook", "text", "position", "archived"]
        read_only_fields = ["id"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            fields["playbook"].queryset = Playbook.objects.filter(user=request.user)
        return fields

    def validate(self, attrs):
        if self.instance and "playbook" in attrs and attrs["playbook"] != self.instance.playbook:
            raise serializers.ValidationError({"playbook": "A rule cannot move to another playbook."})
        return attrs


# ---------- risk rules ----------

class RiskRuleSerializer(serializers.ModelSerializer):
    unit = serializers.ChoiceField(choices=RiskRule.Unit.choices, required=False)
    limit_value = serializers.DecimalField(
        max_digits=18, decimal_places=2, min_value=Decimal("0.01")
    )

    class Meta:
        model = RiskRule
        fields = ["id", "account", "kind", "unit", "limit_value", "enabled"]
        read_only_fields = ["id"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            fields["account"].queryset = TradingAccount.objects.filter(user=request.user)
        return fields

    def validate(self, attrs):
        current = self.instance
        account = attrs.get("account", current.account if current else None)
        kind = attrs.get("kind", current.kind if current else None)
        if current and (account != current.account or kind != current.kind):
            raise serializers.ValidationError(
                "A rule's account and kind cannot change. Create a new rule instead."
            )

        unit = attrs.get("unit") or (current.unit if current else None)
        limit = attrs.get("limit_value", current.limit_value if current else None)

        if kind == RiskRule.Kind.TRADES_PER_DAY:
            if unit not in (None, RiskRule.Unit.COUNT):
                raise serializers.ValidationError({"unit": "Trades per day is a count."})
            unit = RiskRule.Unit.COUNT
            if limit != limit.to_integral_value():
                raise serializers.ValidationError({"limit_value": "Use a whole number of trades."})
        else:
            unit = unit or RiskRule.Unit.MONEY
            if unit == RiskRule.Unit.COUNT:
                raise serializers.ValidationError({"unit": "Use money or percent for this rule."})
            if unit == RiskRule.Unit.PERCENT and limit > 100:
                raise serializers.ValidationError({"limit_value": "A percent limit cannot exceed 100."})
            if (
                unit == RiskRule.Unit.PERCENT
                and kind in (RiskRule.Kind.DAILY_LOSS, RiskRule.Kind.MAX_DRAWDOWN)
                and account.starting_balance <= 0
            ):
                raise serializers.ValidationError(
                    {"unit": "Percent limits need an account starting balance above zero."}
                )

        if current is None and RiskRule.objects.filter(account=account, kind=kind).exists():
            raise serializers.ValidationError({"kind": "This account already has a rule of this kind."})
        attrs["unit"] = unit
        return attrs