from datetime import datetime

from rest_framework import serializers

from .filters import user_zone
from .models import JournalEntry, JournalTemplate

MAX_SECTIONS = 30


class JournalTemplateSerializer(serializers.ModelSerializer):
    prompts = serializers.ListField(
        child=serializers.CharField(max_length=200), min_length=1, max_length=20
    )

    class Meta:
        model = JournalTemplate
        fields = ["id", "kind", "name", "prompts", "created_at"]
        read_only_fields = ["id", "created_at"]
        validators = []

    def validate(self, attrs):
        user = self.context["request"].user
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        name = attrs.get("name", getattr(self.instance, "name", None))
        clash = JournalTemplate.objects.filter(user=user, kind=kind, name__iexact=name)
        if self.instance:
            clash = clash.exclude(pk=self.instance.pk)
        if clash.exists():
            raise serializers.ValidationError(
                {"name": "You already have a template with this name for this kind."}
            )
        return attrs


class SectionSerializer(serializers.Serializer):
    prompt = serializers.CharField(max_length=200, allow_blank=True)
    answer = serializers.CharField(max_length=10_000, allow_blank=True, required=False, default="")


class JournalEntrySerializer(serializers.ModelSerializer):
    sections = SectionSerializer(many=True, required=False)

    class Meta:
        model = JournalEntry
        fields = [
            "id", "kind", "date", "template", "sections",
            "has_content", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "has_content", "created_at", "updated_at"]
        validators = []

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request and request.user.is_authenticated:
            fields["template"].queryset = JournalTemplate.objects.filter(user=request.user)
        return fields

    def validate_sections(self, value):
        if len(value) > MAX_SECTIONS:
            raise serializers.ValidationError(f"An entry can have at most {MAX_SECTIONS} sections.")
        return [dict(section) for section in value]

    def validate(self, attrs):
        user = self.context["request"].user
        kind = attrs.get("kind", getattr(self.instance, "kind", None))
        day = attrs.get("date", getattr(self.instance, "date", None))

        if self.instance is not None:
            if kind != self.instance.kind or day != self.instance.date:
                raise serializers.ValidationError(
                    "Date and kind cannot change. Save a new entry instead."
                )
        elif day > datetime.now(user_zone(user)).date():
            raise serializers.ValidationError({"date": "Journal entries cannot be dated in the future."})

        if kind == JournalTemplate.Kind.WEEKLY and day.weekday() != 0:
            raise serializers.ValidationError(
                {"date": "Weekly reviews use the Monday of the week they cover."}
            )

        template = attrs.get("template", getattr(self.instance, "template", None))
        if template is not None and template.kind != kind:
            raise serializers.ValidationError(
                {"template": "This template is for a different kind of entry."}
            )
        return attrs