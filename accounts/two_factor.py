import base64
import hashlib
import hmac
import logging
import secrets
import time

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core import signing
from django.db import transaction
from django.utils import timezone

from .models import BackupCode, TwoFactor

logger = logging.getLogger(__name__)

INTERVAL = 30  # seconds a code is valid for
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # no look-alike characters
CHALLENGE_SALT = "accounts.two-factor-challenge"


def _now():
    return time.time()


# ---------- the secret ----------

def _fernet():
    material = settings.TWO_FACTOR_KEY or settings.SECRET_KEY
    key = base64.urlsafe_b64encode(hashlib.sha256(("two-factor|" + material).encode()).digest())
    return Fernet(key)


def encrypt(secret):
    return _fernet().encrypt(secret.encode()).decode()


def decrypt(stored):
    return _fernet().decrypt(stored.encode()).decode()  # raises InvalidToken if the key changed


def new_secret():
    return pyotp.random_base32()


def provisioning_uri(secret, email):
    """What the frontend turns into a QR code for the authenticator app."""
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=settings.TWO_FACTOR_ISSUER)


# ---------- checking codes ----------

def check_totp(secret, code, last_step):
    """The 30-second step a code belongs to, or None. A step can only be used once."""
    digits = "".join(str(code).split())
    if not (digits.isdigit() and len(digits) == 6):
        return None
    current = int(_now()) // INTERVAL
    totp = pyotp.TOTP(secret, interval=INTERVAL)
    for step in (current, current - 1, current + 1):  # one step either side allows for clock drift
        if step > last_step and hmac.compare_digest(totp.at(step * INTERVAL), digits):
            return step
    return None


def _hash_code(code):
    clean = "".join(ch for ch in str(code).lower() if ch.isalnum())
    return hmac.new(settings.SECRET_KEY.encode(), clean.encode(), hashlib.sha256).hexdigest()


def issue_backup_codes(user, count=10):
    """Replace the user's backup codes with a fresh set. The plain codes are returned once, never stored."""
    codes = [
        "-".join("".join(secrets.choice(CODE_ALPHABET) for _ in range(4)) for _ in range(3))
        for _ in range(count)
    ]
    with transaction.atomic():
        BackupCode.objects.filter(user=user).delete()
        BackupCode.objects.bulk_create([BackupCode(user=user, code_hash=_hash_code(c)) for c in codes])
    return codes


def use_backup_code(user, code):
    used = BackupCode.objects.filter(
        user=user, code_hash=_hash_code(code), used_at__isnull=True
    ).update(used_at=timezone.now())
    return used == 1


def has_two_factor(user):
    return TwoFactor.objects.filter(user=user, confirmed=True).exists()


def verify_second_factor(user, code):
    """Accept an authenticator code or a backup code. True if it was valid, and it can't be used again."""
    record = TwoFactor.objects.filter(user=user, confirmed=True).first()
    if record is None:
        return False
    cleaned = "".join(str(code).split())
    if cleaned.isdigit() and len(cleaned) == 6:
        try:
            secret = decrypt(record.secret)
        except InvalidToken:
            logger.error("Cannot decrypt the two-factor secret of user %s: was TWO_FACTOR_KEY changed?", user.pk)
            return False
        step = check_totp(secret, cleaned, record.last_used_step)
        if step is None:
            return False
        # Claim the step in one atomic update, so two requests can't both use the same code
        return TwoFactor.objects.filter(pk=record.pk, last_used_step__lt=step).update(last_used_step=step) == 1
    return use_backup_code(user, cleaned)


# ---------- the step between password and code ----------

def _fingerprint(user):
    return hashlib.sha256(user.password.encode()).hexdigest()[:16]


def make_challenge(user):
    """A signed, short-lived note saying "this person already gave the right password"."""
    return signing.dumps({"u": user.pk, "p": _fingerprint(user)}, salt=CHALLENGE_SALT)


def read_challenge(token):
    from django.contrib.auth import get_user_model

    User = get_user_model()
    try:
        data = signing.loads(token, salt=CHALLENGE_SALT, max_age=settings.TWO_FACTOR_CHALLENGE_SECONDS)
        user = User.objects.get(pk=data["u"], is_active=True)
        fingerprint = str(data["p"])
    except (signing.BadSignature, KeyError, TypeError, ValueError, User.DoesNotExist):
        return None
    # A password change since the challenge was issued makes it worthless
    return user if hmac.compare_digest(_fingerprint(user), fingerprint) else None