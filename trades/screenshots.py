import logging
import uuid
from collections import namedtuple
from io import BytesIO

from django.conf import settings
from django.core import signing
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.urls import reverse
from PIL import Image, ImageOps

logger = logging.getLogger(__name__)

SALT = "trades.screenshot"
SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)

Prepared = namedtuple("Prepared", "data content_type extension width height thumb")
Saved = namedtuple("Saved", "key thumb_key content_type size width height")


class ScreenshotError(ValueError):
    """The upload can't be accepted."""


def storage():
    return storages["screenshots"]


def size_text(limit):
    return f"{limit / (1024 * 1024):g} MB" if limit >= 1024 * 1024 else f"{limit} bytes"


def sniff(head):
    """Identify an image by its first bytes. The file name and the declared type are never trusted."""
    for magic, content_type, extension in SIGNATURES:
        if head.startswith(magic):
            return content_type, extension
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def inspect_image(data):
    """Really decode an upload. Returns (width, height, thumbnail JPEG bytes) or raises ScreenshotError."""
    try:
        with Image.open(BytesIO(data)) as source:
            if source.width * source.height > settings.SCREENSHOT_MAX_PIXELS:
                raise ScreenshotError("That image has too many pixels. Resize it and try again.")
            image = ImageOps.exif_transpose(source)  # phone photos keep their rotation in metadata
            width, height = image.size
            thumb = image.copy()
        edge = settings.SCREENSHOT_THUMBNAIL_SIZE
        thumb.thumbnail((edge, edge))  # shrinks only, never enlarges
        has_alpha = thumb.mode in ("RGBA", "LA") or (thumb.mode == "P" and "transparency" in thumb.info)
        if has_alpha:  # JPEG has no transparency: flatten onto white
            rgba = thumb.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            thumb = flat
        else:
            thumb = thumb.convert("RGB")
        out = BytesIO()
        thumb.save(out, "JPEG", quality=80, optimize=True)
        return width, height, out.getvalue()
    except ScreenshotError:
        raise
    except Exception:  # noqa: BLE001 - untrusted data can fail in many different ways
        raise ScreenshotError("That file isn't a readable image.")


def prepare_upload(upload):
    """Check an uploaded image completely before anything is stored."""
    limit = settings.SCREENSHOT_MAX_BYTES
    data = upload.read(limit + 1)  # never read more than one byte past the limit
    if len(data) > limit:
        raise ScreenshotError(f"Images are limited to {size_text(limit)}.")
    found = sniff(data[:16])
    if found is None:
        raise ScreenshotError("Upload a PNG, JPEG, GIF or WebP image.")
    width, height, thumb = inspect_image(data)
    return Prepared(data, found[0], found[1], width, height, thumb)


def store(user_id, trade_id, prepared):
    """Write the image and its thumbnail to storage."""
    base = f"screenshots/{user_id}/{trade_id}/{uuid.uuid4().hex}"
    key = storage().save(f"{base}.{prepared.extension}", ContentFile(prepared.data))
    try:
        thumb_key = storage().save(f"{base}.thumb.jpg", ContentFile(prepared.thumb))
    except Exception:
        delete_file(key)
        raise
    return Saved(key, thumb_key, prepared.content_type, len(prepared.data), prepared.width, prepared.height)


def usage(user):
    """How much screenshot storage a user has used. Only original files count, not thumbnails."""
    from django.db.models import Count, Sum

    from .models import Screenshot

    found = Screenshot.objects.filter(trade__account__user=user).aggregate(used=Sum("size"), count=Count("id"))
    used = found["used"] or 0
    limit = settings.SCREENSHOT_USER_QUOTA_BYTES
    return {
        "used_bytes": used,
        "limit_bytes": limit,
        "remaining_bytes": max(limit - used, 0),
        "count": found["count"],
        "max_per_trade": settings.SCREENSHOT_MAX_PER_TRADE,
        "max_file_bytes": settings.SCREENSHOT_MAX_BYTES,
    }


def delete_file(key):
    """Remove a file from storage. A storage hiccup must never undo or block a database delete."""
    if not key:
        return
    try:
        storage().delete(key)
    except Exception:  # noqa: BLE001
        logger.exception("Could not delete screenshot file %s", key)


def make_token(shot, thumb=False):
    return signing.dumps({"s": shot.pk, "t": 1 if thumb else 0}, salt=SALT)


def read_token(token):
    """(screenshot id, wants the thumbnail) from a valid, unexpired token, or None."""
    if not token:
        return None
    try:
        data = signing.loads(token, salt=SALT, max_age=settings.SCREENSHOT_URL_TTL)
        return int(data["s"]), bool(data.get("t"))
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None


def signed_url(request, shot, thumb=False):
    """A link to the file (or its thumbnail) that stops working after SCREENSHOT_URL_TTL seconds."""
    key = shot.thumb_key if thumb else shot.key
    if not key:
        return None
    if settings.SCREENSHOT_LINKS == "storage":
        return storage().url(key, expire=settings.SCREENSHOT_URL_TTL)
    return request.build_absolute_uri(f"{reverse('screenshot-file')}?t={make_token(shot, thumb)}")