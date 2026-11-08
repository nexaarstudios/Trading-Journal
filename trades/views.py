from django.db import transaction
from django.db.models import Count, Max, Prefetch, Q
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from types import SimpleNamespace
from .emotions import ensure_default_emotions
from django.conf import settings
from django.contrib.auth import get_user_model

from . import screenshots as shots
from .screenshot_serializers import ScreenshotSerializer

from . import rollups
from .filters import apply_base_filters, date_bounds, int_param, user_zone
from .importer import (
    ImportFileError, build_plan, commit_plan, missing_fields, read_pasted, read_table,
    sample_rows, suggest_mapping, undo_batch,
)
from .models import (
    ImportBatch, ImportTemplate, InstrumentSpec, Playbook, PlaybookRule,
    Screenshot, Tag, Trade, TradingAccount,
)
from .serializers import (
    BulkTradeSerializer,
    ImportBatchSerializer,
    ImportPreviewTradeSerializer,
    ImportRequestSerializer,
    ImportTemplateSerializer,
    InstrumentSpecSerializer,
    PlaybookRuleSerializer,
    PlaybookSerializer,
    TagSerializer,
    TradeSerializer,
    TradingAccountSerializer,
)

PREVIEW_ROWS = 10
MAX_ERRORS_SHOWN = 50


class TradingAccountViewSet(viewsets.ModelViewSet):
    serializer_class = TradingAccountSerializer

    def get_queryset(self):
        return TradingAccount.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class InstrumentSpecViewSet(viewsets.ReadOnlyModelViewSet):
    """Global defaults plus the user's own per-account overrides."""

    serializer_class = InstrumentSpecSerializer
    pagination_class = None

    def get_queryset(self):
        return InstrumentSpec.objects.filter(
            Q(account__isnull=True) | Q(account__user=self.request.user)
        )



class TradeViewSet(viewsets.ModelViewSet):
    serializer_class = TradeSerializer

    def get_queryset(self):
        qs = (
            Trade.objects.filter(account__user=self.request.user)
            .select_related("account")
            .prefetch_related("tags", "rule_checks__rule", "screenshots")
        )
        params = self.request.query_params
        qs = apply_base_filters(qs, params)

        batch = int_param(params, "import_batch")
        if batch is not None:
            qs = qs.filter(import_batch_id=batch)

        start, end = date_bounds(params, user_zone(self.request.user))
        if start:
            qs = qs.filter(closed_at__gte=start)
        if end:
            qs = qs.filter(closed_at__lt=end)
        return qs

    @action(detail=False, methods=["post"])
    def bulk(self, request):
        """Add or remove tags on many trades at once, or delete them."""
        form = BulkTradeSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        data = form.validated_data

        ids = set(data["ids"])
        trades = Trade.objects.filter(account__user=request.user, id__in=ids)
        matched = list(trades.values_list("id", flat=True))  # other people's ids are simply not matched
        through = Trade.tags.through

        with transaction.atomic():
            if data["action"] == "delete":
                with rollups.deferred():  # refresh the daily rollups once, not per trade
                    trades.delete()
            elif data["action"] == "add_tags":
                through.objects.bulk_create(
                    [through(trade_id=t, tag_id=g) for t in matched for g in data["tag_ids"]],
                    ignore_conflicts=True, batch_size=1000,
                )
            else:
                through.objects.filter(trade_id__in=matched, tag_id__in=data["tag_ids"]).delete()
        return Response({"requested": len(ids), "affected": len(matched)})


    @action(detail=True, methods=["get", "post"], parser_classes=[MultiPartParser, FormParser])
    def screenshots(self, request, pk=None):
        """List a trade's screenshots with fresh signed links, or upload one (multipart: file, caption)."""
        trade = self.get_object()
        context = {"request": request}
        if request.method == "GET":
            return Response(ScreenshotSerializer(trade.screenshots.all(), many=True, context=context).data)

        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError({"file": "Attach an image."})
        caption = (request.data.get("caption") or "").strip()
        if len(caption) > 200:
            raise ValidationError({"caption": "Captions are limited to 200 characters."})

        try:
            prepared = shots.prepare_upload(upload)  # decode and check it before taking any lock
            with transaction.atomic():
                # Lock the user's row so two uploads at once can't both slip past the limits
                get_user_model().objects.select_for_update().get(pk=request.user.pk)
                positions = list(trade.screenshots.values_list("position", flat=True))
                if len(positions) >= settings.SCREENSHOT_MAX_PER_TRADE:
                    raise shots.ScreenshotError(
                        f"A trade can have at most {settings.SCREENSHOT_MAX_PER_TRADE} screenshots."
                    )
                used = shots.usage(request.user)
                if used["used_bytes"] + len(prepared.data) > used["limit_bytes"]:
                    raise shots.ScreenshotError(
                        f"You have used {shots.size_text(used['used_bytes'])} of your "
                        f"{shots.size_text(used['limit_bytes'])} screenshot storage. "
                        "Delete some screenshots to make room."
                    )
                saved = shots.store(request.user.pk, trade.pk, prepared)
                try:
                    shot = Screenshot.objects.create(
                        trade=trade, key=saved.key, thumb_key=saved.thumb_key,
                        content_type=saved.content_type, size=saved.size,
                        width=saved.width, height=saved.height,
                        caption=caption, position=max(positions, default=0) + 1,
                    )
                except Exception:
                    shots.delete_file(saved.key)  # don't leave orphan files behind
                    shots.delete_file(saved.thumb_key)
                    raise
        except shots.ScreenshotError as exc:
            raise ValidationError({"file": str(exc)})
        return Response(ScreenshotSerializer(shot, context=context).data, status=status.HTTP_201_CREATED)


class TagViewSet(viewsets.ModelViewSet):
    serializer_class = TagSerializer
    pagination_class = None

    def get_queryset(self):
        qs = Tag.objects.filter(user=self.request.user).annotate(trade_count=Count("trades"))
        if kind := self.request.query_params.get("kind"):
            qs = qs.filter(kind=kind)
        return qs.order_by("kind", "name")

    def list(self, request, *args, **kwargs):
        if request.query_params.get("kind") == Tag.Kind.EMOTION:
            ensure_default_emotions(request.user)  # a starter set on first use
        return super().list(request, *args, **kwargs)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

class PlaybookViewSet(viewsets.ModelViewSet):
    serializer_class = PlaybookSerializer
    pagination_class = None

    def get_queryset(self):
        qs = (
            Playbook.objects.filter(user=self.request.user)
            .annotate(trade_count=Count("trades", distinct=True))
            .prefetch_related(
                Prefetch("rules", queryset=PlaybookRule.objects.filter(archived=False))
            )
        )
        if self.action == "list" and not self.request.query_params.get("include_archived"):
            qs = qs.filter(archived=False)
        return qs.order_by("name")

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        # Trades keep their history, so a playbook is archived rather than deleted
        instance.archived = True
        instance.save(update_fields=["archived"])


class PlaybookRuleViewSet(viewsets.ModelViewSet):
    serializer_class = PlaybookRuleSerializer
    pagination_class = None

    def get_queryset(self):
        qs = PlaybookRule.objects.filter(playbook__user=self.request.user)
        if self.action == "list":
            qs = qs.filter(archived=False)
        playbook = int_param(self.request.query_params, "playbook")
        if playbook is not None:
            qs = qs.filter(playbook_id=playbook)
        return qs.order_by("playbook_id", "position", "id")

    def perform_create(self, serializer):
        if "position" in serializer.validated_data:
            serializer.save()
            return
        playbook = serializer.validated_data["playbook"]
        last = playbook.rules.aggregate(top=Max("position"))["top"]
        serializer.save(position=(last or 0) + 1)

    def perform_destroy(self, instance):
        instance.archived = True
        instance.save(update_fields=["archived"])


# ---------- imports ----------

class ImportTemplateViewSet(viewsets.ModelViewSet):
    serializer_class = ImportTemplateSerializer
    pagination_class = None

    def get_queryset(self):
        return ImportTemplate.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


def _prepare_import(request):
    """Shared by preview and commit: validate the input and read the file or pasted text."""
    form = ImportRequestSerializer(data=request.data, context={"request": request})
    form.is_valid(raise_exception=True)
    data = form.validated_data
    template = data.get("template")

    # Request values win, then the template's, then defaults
    timezone_name = data.get("timezone") or (template.timezone if template else "UTC")
    if "date_format" in data:
        date_format = data["date_format"]
    else:
        date_format = template.date_format if template else ""
    if "costs_as_negative" in data:
        costs_as_negative = data["costs_as_negative"]
    else:
        costs_as_negative = template.costs_as_negative if template else True
    separator = data.get("decimal_separator") or (template.decimal_separator if template else "") or None

    try:
        if "file" in data:
            filename = data["file"].name
            headers, rows = read_table(data["file"], filename)
        else:
            filename = "Pasted rows"
            headers, rows = read_pasted(data["text"], has_header=data.get("has_header", True))
    except ImportFileError as exc:
        raise ValidationError({"detail": str(exc)})

    mapping = data.get("mapping") or (template.mapping if template else None) or suggest_mapping(headers)
    return SimpleNamespace(
        data=data, template=template, headers=headers, rows=rows, mapping=mapping,
        filename=filename, decimal_separator=separator,
        options={
            "timezone": timezone_name,
            "date_format": date_format,
            "costs_as_negative": costs_as_negative,
        },
    )


def _plan(prep):
    try:
        return build_plan(
            prep.data["account"], prep.headers, prep.rows, prep.mapping,
            tz_name=prep.options["timezone"], date_format=prep.options["date_format"],
            costs_as_negative=prep.options["costs_as_negative"],
            decimal_separator=prep.decimal_separator,
        )
    except ImportFileError as exc:
        raise ValidationError({"detail": str(exc)})


def _base(prep):
    return {
        "filename": prep.filename,
        "headers": prep.headers,
        "mapping": prep.mapping,
        "sample_rows": sample_rows(prep.headers, prep.rows),
        **prep.options,
    }


def _summary(prep, plan):
    return {
        **_base(prep),
        "decimal_separator": plan.decimal_separator,
        "decimal_separator_detected": plan.detected_decimal_separator,
        "total_rows": plan.total_rows,
        "importable": len(plan.trades),
        "duplicates": plan.duplicates,
        "error_count": len(plan.errors),
        "errors": [
            {"line": line, "message": message} for line, message in plan.errors[:MAX_ERRORS_SHOWN]
        ],
    }


class ImportBatchViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet
):
    serializer_class = ImportBatchSerializer
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get_queryset(self):
        return ImportBatch.objects.filter(account__user=self.request.user)

    @action(detail=False, methods=["post"])
    def preview(self, request):
        """Parse the file or pasted text and report what would happen. Saves nothing."""
        prep = _prepare_import(request)
        missing = missing_fields(prep.mapping)
        if missing:  # the guess was incomplete: hand back what the mapping screen needs
            return Response({**_base(prep), "needs_mapping": True, "missing_fields": missing})
        plan = _plan(prep)
        body = _summary(prep, plan)
        body["needs_mapping"] = False
        body["preview"] = ImportPreviewTradeSerializer(plan.trades[:PREVIEW_ROWS], many=True).data
        return Response(body)

    def create(self, request):
        """Import the file or pasted text for real, in one transaction."""
        prep = _prepare_import(request)
        missing = missing_fields(prep.mapping)
        if missing:
            raise ValidationError({"detail": f"Map these required fields: {', '.join(missing)}."})
        plan = _plan(prep)
        summary = _summary(prep, plan)

        if not plan.trades:
            summary["detail"] = "Nothing to import: there are no new valid trades."
            return Response(summary, status=status.HTTP_400_BAD_REQUEST)

        template = prep.template
        template_name = (prep.data.get("save_template_name") or "").strip()
        with transaction.atomic():
            if template_name:
                template, _ = ImportTemplate.objects.update_or_create(
                    user=request.user,
                    name=template_name,
                    defaults={
                        "mapping": prep.mapping,
                        "timezone": prep.options["timezone"],
                        "date_format": prep.options["date_format"],
                        "costs_as_negative": prep.options["costs_as_negative"],
                        "decimal_separator": plan.decimal_separator,
                    },
                )
            batch = commit_plan(plan, filename=prep.filename, template=template)

        summary["batch"] = ImportBatchSerializer(batch).data
        summary["template_id"] = template.id if template else None
        return Response(summary, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def undo(self, request, pk=None):
        """Remove every trade that came from this import."""
        batch = self.get_object()
        try:
            removed = undo_batch(batch)
        except ImportFileError as exc:
            raise ValidationError({"detail": str(exc)})
        return Response({"removed": removed, "status": "undone"})
    

class ScreenshotViewSet(mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Edit a screenshot's caption and order, or delete it."""

    serializer_class = ScreenshotSerializer
    http_method_names = ["patch", "delete", "head", "options"]

    def get_queryset(self):
        return Screenshot.objects.filter(trade__account__user=self.request.user)