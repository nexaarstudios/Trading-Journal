from django.http import FileResponse, Http404
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from . import screenshots as shots
from .models import Screenshot


class ScreenshotFileView(APIView):
    """Serve one screenshot or thumbnail from a signed, expiring link. The link is the credential, so no login header is needed."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def perform_content_negotiation(self, request, force=False):
        # Image requests send odd Accept headers. This endpoint only ever returns the file.
        renderer = self.get_renderers()[0]
        return renderer, renderer.media_type

    def get(self, request):
        found = shots.read_token(request.query_params.get("t", ""))
        shot = Screenshot.objects.filter(pk=found[0]).first() if found else None
        if shot is None:
            raise Http404
        thumb = found[1]
        key = shot.thumb_key if thumb else shot.key
        if not key:
            raise Http404
        try:
            handle = shots.storage().open(key, "rb")
        except OSError:
            raise Http404
        response = FileResponse(handle, content_type="image/jpeg" if thumb else shot.content_type)
        response["Content-Disposition"] = "inline"
        response["X-Content-Type-Options"] = "nosniff"
        response["Cache-Control"] = "private, max-age=300"
        return response


class ScreenshotUsageView(APIView):
    """How much of the user's screenshot storage is used, and the upload limits."""

    def get(self, request):
        return Response(shots.usage(request.user))