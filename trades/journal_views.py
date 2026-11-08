from datetime import datetime

from django.utils.dateparse import parse_date
from rest_framework import viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from . import journal, reports
from .filters import user_zone
from .journal_serializers import JournalEntrySerializer, JournalTemplateSerializer
from .models import JournalEntry, JournalTemplate


def _date(params, name):
    raw = params.get(name)
    if not raw:
        return None
    try:
        value = parse_date(raw)
    except ValueError:
        value = None
    if value is None:
        raise ValidationError({name: "Use the format YYYY-MM-DD."})
    return value


def _kind(params):
    kind = params.get("kind")
    if kind and kind not in JournalTemplate.Kind.values:
        raise ValidationError({"kind": "Use pre_market, post_market or weekly."})
    return kind


class JournalTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = JournalTemplateSerializer
    pagination_class = None

    def get_queryset(self):
        qs = JournalTemplate.objects.filter(user=self.request.user)
        if kind := _kind(self.request.query_params):
            qs = qs.filter(kind=kind)
        return qs

    def list(self, request, *args, **kwargs):
        journal.ensure_default_templates(request.user)
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        journal.ensure_default_templates(self.request.user)  # defaults first, so they aren't skipped
        serializer.save(user=self.request.user)


class JournalEntryViewSet(viewsets.ModelViewSet):
    serializer_class = JournalEntrySerializer

    def get_queryset(self):
        qs = JournalEntry.objects.filter(user=self.request.user)
        params = self.request.query_params
        if kind := _kind(params):
            qs = qs.filter(kind=kind)
        if day := _date(params, "date"):
            qs = qs.filter(date=day)
        if first := _date(params, "from"):
            qs = qs.filter(date__gte=first)
        if last := _date(params, "to"):
            qs = qs.filter(date__lte=last)
        return qs

    def create(self, request, *args, **kwargs):
        """Create the entry for this kind and day, or update it if it already exists."""
        form = self.get_serializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data

        updates = {key: data[key] for key in ("template", "sections") if key in data}
        defaults = dict(updates)
        if "sections" not in defaults and data.get("template"):
            # Only a brand-new entry is pre-filled, so reopening never wipes answers
            defaults["sections"] = [{"prompt": p, "answer": ""} for p in data["template"].prompts]

        entry, created = JournalEntry.objects.get_or_create(
            user=request.user, kind=data["kind"], date=data["date"], defaults=defaults
        )
        if not created and updates:
            for key, value in updates.items():
                setattr(entry, key, value)
            entry.save()
        return Response(self.get_serializer(entry).data, status=201 if created else 200)


class JournalStreakView(APIView):
    """Days journaled in a row. Weekly reviews don't count toward it."""

    def get(self, request):
        today = _date(request.query_params, "date") or datetime.now(user_zone(request.user)).date()
        days = (
            JournalEntry.objects.filter(
                user=request.user, has_content=True, kind__in=journal.DAILY_KINDS, date__lte=today
            )
            .order_by()
            .values_list("date", flat=True)
            .distinct()
        )
        return Response(reports.jsonable({"date": today, **journal.streaks(days, today)}))