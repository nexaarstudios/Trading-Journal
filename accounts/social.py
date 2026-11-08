import hashlib
import hmac
import secrets
from dataclasses import dataclass

import jwt
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from . import mail
from .models import SocialAccount

PROVIDERS = {
    "google": {
        "label": "Google",
        "jwks": "https://www.googleapis.com/oauth2/v3/certs",
        "issuers": ["https://accounts.google.com", "accounts.google.com"],
        "setting": "GOOGLE_CLIENT_IDS",
    },
    "apple": {
        "label": "Apple",
        "jwks": "https://appleid.apple.com/auth/keys",
        "issuers": ["https://appleid.apple.com"],
        "setting": "APPLE_CLIENT_IDS",
    },
}
GOOGLE_OWN_DOMAINS = ("@gmail.com", "@googlemail.com")
_CONNECTION_ERRORS = (OSError, getattr(jwt.exceptions, "PyJWKClientConnectionError", OSError))


class SocialError(Exception):
    """Everything that can go wrong signing in through a provider. `status` and `code` become the API answer."""

    status = 400
    code = "social_error"


class NotConfigured(SocialError):
    status, code = 503, "provider_not_configured"


class ProviderUnavailable(SocialError):
    status, code = 503, "provider_unavailable"


class InvalidIdentity(SocialError):
    status, code = 400, "invalid_identity"


class AccountExists(SocialError):
    status, code = 409, "account_exists"


class EmailUnverified(SocialError):
    status, code = 403, "email_not_verified"


@dataclass(frozen=True)
class Identity:
    provider: str
    subject: str
    email: str
    email_verified: bool
    authoritative: bool  # can the provider vouch that this person really controls the address?


def label(provider):
    return PROVIDERS[provider]["label"]


def client_ids(provider):
    return [value for value in getattr(settings, PROVIDERS[provider]["setting"], []) if value]


# ---------- checking the token ----------

_clients = {}


def _signing_key(provider, token):
    """The provider's public key for this token. The only place in this module that touches the network."""
    url = PROVIDERS[provider]["jwks"]
    if url not in _clients:
        _clients[url] = jwt.PyJWKClient(url, timeout=5)
    return _clients[url].get_signing_key_from_jwt(token).key


def _truthy(value):
    return value is True or str(value).strip().lower() == "true"  # Apple sometimes sends the string "true"


def _nonce_matches(claimed, nonce):
    if not claimed or not nonce:
        return False
    claimed, nonce = str(claimed), str(nonce)
    hashed = hashlib.sha256(nonce.encode()).hexdigest()  # native apps send the hash to the provider
    return hmac.compare_digest(claimed, nonce) or hmac.compare_digest(claimed, hashed)


def verify(provider, id_token, nonce):
    """Check an ID token from the frontend and say who it is for. Raises SocialError if anything is off."""
    config = PROVIDERS[provider]
    ids = client_ids(provider)
    if not ids:
        raise NotConfigured(f"{config['label']} sign-in is not set up yet.")
    try:
        key = _signing_key(provider, id_token)
    except _CONNECTION_ERRORS:
        raise ProviderUnavailable(f"Could not reach {config['label']} to check your sign-in. Try again in a moment.")
    except jwt.PyJWTError:
        raise InvalidIdentity("That sign-in could not be verified.")
    try:
        claims = jwt.decode(
            id_token, key, algorithms=["RS256"], audience=ids, issuer=config["issuers"], leeway=30,
            options={"require": ["exp", "iat", "iss", "aud", "sub"]},
        )
    except jwt.PyJWTError:
        raise InvalidIdentity("That sign-in could not be verified.")
    if not _nonce_matches(claims.get("nonce"), nonce):
        raise InvalidIdentity("That sign-in could not be verified.")  # guards against replaying a stolen token

    email = str(claims.get("email") or "").strip().lower()
    verified = _truthy(claims.get("email_verified"))
    if provider == "google":
        # Google only vouches for addresses it hosts: Gmail, or a Workspace domain (the "hd" claim)
        authoritative = verified and (email.endswith(GOOGLE_OWN_DOMAINS) or bool(claims.get("hd")))
    else:
        authoritative = verified
    return Identity(provider, str(claims["sub"]), email, verified, authoritative)


# ---------- finding or making the user ----------

def _free_username(email):
    User = get_user_model()
    if not User.objects.filter(username__iexact=email).exists():
        return email
    return f"{email}-{secrets.token_hex(3)}"


def sign_in_user(identity, timezone=None):
    """The user this identity signs in as, connecting or creating an account when that is safe. Returns (user, created)."""
    User = get_user_model()
    name = label(identity.provider)
    lookup = {"provider": identity.provider, "subject": identity.subject}

    link = SocialAccount.objects.select_related("user").filter(**lookup).first()
    if link:
        if not link.user.is_active:
            raise InvalidIdentity("This account is disabled.")
        return link.user, False

    if not identity.email:
        raise InvalidIdentity(f"{name} did not share an email address, so an account can't be created.")
    if not identity.email_verified:
        raise InvalidIdentity(f"Your {name} email address is not verified.")

    try:
        with transaction.atomic():
            existing = User.objects.select_for_update().filter(email__iexact=identity.email).first()
            if existing is not None:
                if not existing.is_active:
                    raise InvalidIdentity("This account is disabled.")
                if not identity.authoritative:
                    raise AccountExists(
                        f"An account with this email already exists. Sign in with your password, "
                        f"then connect {name} from your settings."
                    )
                if SocialAccount.objects.filter(user=existing, provider=identity.provider).exists():
                    raise AccountExists(f"This account is connected to a different {name} account.")
                if not existing.email_verified:
                    # Someone signed up with this address without ever proving it was theirs. This person just did,
                    # so the account is now theirs and the password the squatter chose stops working.
                    existing.set_unusable_password()
                    existing.email_verified = True
                    existing.save(update_fields=["password", "email_verified"])
                user, created = existing, False
            else:
                user = User.objects.create_user(
                    username=_free_username(identity.email), email=identity.email, password=None,
                    email_verified=identity.authoritative, timezone=timezone or "UTC",
                )
                created = True
            SocialAccount.objects.create(
                user=user, provider=identity.provider, subject=identity.subject, email=identity.email
            )
    except IntegrityError:  # two sign-ins raced each other
        link = SocialAccount.objects.select_related("user").filter(**lookup).first()
        if link:
            return link.user, False
        raise InvalidIdentity("Something went wrong signing you in. Please try again.")

    if created and not user.email_verified:
        mail.send_verification(user)
        raise EmailUnverified(
            f"We emailed you a link to confirm {identity.email}. Open it, then sign in with {name} again."
        )
    return user, created


def link(user, identity):
    """Connect an identity to a signed-in user. Returns False if it was already connected to them."""
    name = label(identity.provider)
    existing = SocialAccount.objects.filter(provider=identity.provider, subject=identity.subject).first()
    if existing:
        if existing.user_id == user.pk:
            return False
        raise InvalidIdentity(f"That {name} account is already connected to a different Trading Journal account.")
    if SocialAccount.objects.filter(user=user, provider=identity.provider).exists():
        raise InvalidIdentity(f"A different {name} account is already connected. Disconnect it first.")
    try:
        SocialAccount.objects.create(
            user=user, provider=identity.provider, subject=identity.subject, email=identity.email
        )
    except IntegrityError:
        raise InvalidIdentity(f"That {name} account could not be connected.")
    return True