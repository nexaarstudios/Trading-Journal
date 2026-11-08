import logging

from django.core.cache import cache
from django.db import connection
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


class HealthView(APIView):
    """Is the service up? Checks the database and the cache. Public, and used by the host's health check."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = []

    def get(self, request):
        failing = []
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:  # noqa: BLE001
            logger.exception("Health check: the database is not answering")
            failing.append("database")
        try:
            cache.set("health-check", "ok", 10)
            if cache.get("health-check") != "ok":
                failing.append("cache")
        except Exception:  # noqa: BLE001
            logger.exception("Health check: the cache is not answering")
            failing.append("cache")
        if failing:
            return Response({"status": "unhealthy", "failing": failing}, status=503)
        return Response({"status": "ok"})