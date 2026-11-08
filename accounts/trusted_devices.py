import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import TrustedDevice


def _hash(token):
    return hmac.new(settings.SECRET_KEY.encode(), token.encode(), hashlib.sha256).hexdigest()


def remember(user, label=""):
    """Remember a device. Returns the secret token, which is shown once and never stored in plain form."""
    now = timezone.now()
    TrustedDevice.objects.filter(user=user, expires_at__lte=now).delete()
    keep = settings.TRUSTED_DEVICE_MAX - 1
    surplus = list(
        TrustedDevice.objects.filter(user=user).order_by("-created_at", "-id").values_list("pk", flat=True)[keep:]
    )
    if surplus:
        TrustedDevice.objects.filter(pk__in=surplus).delete()
    token = secrets.token_urlsafe(32)
    TrustedDevice.objects.create(
        user=user, token_hash=_hash(token), label=(label or "")[:120],
        expires_at=now + timedelta(days=settings.TRUSTED_DEVICE_DAYS),
    )
    return token


def is_trusted(user, token):
    """True if this token belongs to one of the user's unexpired remembered devices."""
    if not token or not isinstance(token, str):
        return False
    now = timezone.now()
    found = TrustedDevice.objects.filter(
        user=user, token_hash=_hash(token), expires_at__gt=now
    ).update(last_used_at=now)
    return found == 1


def revoke_all(user):
    """Forget every remembered device. Used whenever the password changes or two-factor is turned off."""
    return TrustedDevice.objects.filter(user=user).delete()[0]