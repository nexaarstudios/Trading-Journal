from datetime import datetime
from decimal import Decimal

from rest_framework import serializers

from . import goals
from .models import Goal, TradingAccount


class MonthField(serializers.Field):
    """A month as 'YYYY-MM' on the outside and the first day of that month inside."""

    default_error_messages = {"invalid": "Use the format YYYY-MM."}

    def to_representation(self, value):
        return value.strftime("%Y-%m")

    def to_internal_value(self, data):
        try:
            return datetime.strptime(str(data), "%Y-%m").date()
        except ValueError:
            self.fail("invalid")


class GoalSerializer(serializers.ModelSerializer):
    start_month = MonthField()
    end_month = MonthField(required=False, allow_null=True)
    target = serializers.DecimalField(max_digits=18, decimal_places=2, min_value=Decimal("0.01"))

    class Meta:
        model = Goal
        fields = ["id", "kind", "account", "target", "start_month", "end_month", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            fields["account"].queryset = TradingAccount.objects.filter(user=request.user)
        return fields

    def validate(self, attrs):
        current = self.instance

        def pick(name):
            if name in attrs:
                return attrs[name]
            return getattr(current, name) if current else None

        kind, account, target = pick("kind"), pick("account"), pick("target")
        start, end = pick("start_month"), pick("end_month")

        if current and kind != current.kind:
            raise serializers.ValidationError(
                {"kind": "A goal's kind cannot change. Create a new goal instead."}
            )
        if end is not None and end < start:
            raise serializers.ValidationError({"end_month": "The end month cannot be before the start month."})
        if kind == Goal.Kind.NET_PNL and account is None:
            raise serializers.ValidationError(
                {"account": "A profit goal needs an account, because money has a currency."}
            )
        if kind == Goal.Kind.JOURNAL_DAYS and account is not None:
            raise serializers.ValidationError(
                {"account": "Journal goals cover your whole journal, not one account."}
            )
        if kind in (Goal.Kind.WIN_RATE, Goal.Kind.RULE_ADHERENCE) and target > 100:
            raise serializers.ValidationError({"target": "A percentage goal cannot exceed 100."})
        if kind == Goal.Kind.JOURNAL_DAYS and (target != target.to_integral_value() or target > 31):
            raise serializers.ValidationError({"target": "Use a whole number of days, at most 31."})

        # Two goals of the same kind for the same account must not cover the same month
        user = self.context["request"].user
        rivals = Goal.objects.filter(user=user, kind=kind, account=account)
        if current:
            rivals = rivals.exclude(pk=current.pk)
        for other in rivals:
            if goals.overlaps(start, end, other.start_month, other.end_month):
                raise serializers.ValidationError(
                    "You already have a goal of this kind that covers those months."
                )
        return attrs