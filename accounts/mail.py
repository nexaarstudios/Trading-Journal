import logging
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

logger = logging.getLogger(__name__)
VERIFY_SALT = "accounts.verify-email"
CHANGE_SALT = "accounts.change-email"


def _link(path, **params):
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}?{urlencode(params)}"


def _send_to(address, subject, body):
    try:
        send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [address])
    except Exception:  # noqa: BLE001 - a mail outage must never break a request; the log says what happened
        logger.exception("Could not send the email '%s'", subject)
        return False
    return True


# ---------- password reset ----------

def send_password_reset(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    link = _link("/reset-password", uid=uid, token=default_token_generator.make_token(user))
    minutes = settings.PASSWORD_RESET_TIMEOUT // 60
    return _send_to(
        user.email, "Reset your Trading Journal password",
        f"Someone asked to reset the password for this account.\n\n"
        f"To choose a new password, open this link within {minutes} minutes:\n{link}\n\n"
        f"If it wasn't you, ignore this email. Your password stays as it is.\n",
    )


# ---------- verifying an email address ----------

def make_verification_token(user):
    return signing.dumps({"u": user.pk, "e": user.email}, salt=VERIFY_SALT)


def send_verification(user):
    link = _link("/verify-email", token=make_verification_token(user))
    return _send_to(
        user.email, "Confirm your Trading Journal email",
        f"Welcome to Trading Journal.\n\nConfirm that this is your email address, then sign in:\n{link}\n\n"
        f"If you didn't create an account, ignore this email.\n",
    )


def read_verification_token(token):
    """The user a verification token belongs to, if it is genuine, recent, and for their current email."""
    User = get_user_model()
    try:
        data = signing.loads(token, salt=VERIFY_SALT, max_age=settings.EMAIL_VERIFICATION_MAX_AGE)
        return User.objects.get(pk=data["u"], email=data["e"])
    except (signing.BadSignature, KeyError, TypeError, ValueError, User.DoesNotExist):
        return None


# ---------- changing the email address ----------

def send_email_change(user, new_email):
    """The confirmation link goes to the NEW address, so only someone who controls it can finish the change."""
    token = signing.dumps({"u": user.pk, "o": user.email, "n": new_email}, salt=CHANGE_SALT)
    link = _link("/confirm-email-change", token=token)
    return _send_to(
        new_email, "Confirm your new Trading Journal email",
        f"Someone asked to use this address for a Trading Journal account.\n\n"
        f"To confirm the change, open this link:\n{link}\n\n"
        f"If it wasn't you, ignore this email and nothing will change.\n",
    )


def send_email_change_notice(user, new_email):
    """A heads-up to the OLD address, so a hijacked session can't quietly move the account."""
    return _send_to(
        user.email, "Your Trading Journal email address is about to change",
        f"A request was made to change the email on your Trading Journal account to {new_email}.\n\n"
        f"Nothing changes until that address confirms it. If this was you, there is nothing to do. "
        f"If it wasn't, change your password now.\n",
    )


def read_email_change_token(token):
    try:
        data = signing.loads(token, salt=CHANGE_SALT, max_age=settings.EMAIL_CHANGE_MAX_AGE)
        return {"user_id": int(data["u"]), "old": str(data["o"]), "new": str(data["n"])}
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None