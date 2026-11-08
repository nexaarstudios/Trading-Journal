from django.conf import settings
from rest_framework import serializers

from . import screenshots as shots
from .models import Screenshot


class ScreenshotSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()
    url_expires_in = serializers.SerializerMethodField()

    class Meta:
        model = Screenshot
        fields = [
            "id", "trade", "caption", "position", "content_type", "size", "width", "height",
            "created_at", "url", "thumbnail_url", "url_expires_in",
        ]
        read_only_fields = ["id", "trade", "content_type", "size", "width", "height", "created_at"]
        extra_kwargs = {"position": {"max_value": 99}}

    def get_url(self, obj):
        return shots.signed_url(self.context["request"], obj)

    def get_thumbnail_url(self, obj):
        return shots.signed_url(self.context["request"], obj, thumb=True)

    def get_url_expires_in(self, obj):
        return settings.SCREENSHOT_URL_TTL