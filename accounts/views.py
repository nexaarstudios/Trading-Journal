from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.utils import timezone
from django.utils.http import urlsafe_base64_decode
from rest_framework import generics, status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from . import lockout, mail, social, trusted_devices, two_factor
from .exceptions import EmailNotVerified
from .models import BackupCode, TwoFactor
from .serializers import (
    CodeSerializer,
    EmailChangeRequestSerializer,
    EmailLoginSerializer,
    EmailSerializer,
    MeSerializer,
    PasswordAndCodeSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    PasswordSerializer,
    RegisterSerializer,
    RevocableRefreshSerializer,
    SocialLinkSerializer,
    SocialSignInSerializer,
    SocialUnlinkSerializer,
    TokenSerializer,
    TwoFactorLoginSerializer,
)

User = get_user_model()


# ---------- small helpers ----------

def issue_tokens(user):
    refresh = RefreshToken.for_user(user)
    return {"access": str(refresh.access_token), "refresh": str(refresh)}


def too_many(seconds):
    minutes = max(1, -(-seconds // 60))
    return Response(
        {
            "detail": f"Too many failed attempts. Try again in {minutes} minute{'s' if minutes != 1 else ''}.",
            "retry_after": seconds,
        },
        status=status.HTTP_429_TOO_MANY_REQUESTS,
        headers={"Retry-After": str(seconds)},
    )


def refused(message):
    return Response({"detail": message}, status=status.HTTP_401_UNAUTHORIZED)


def bad(message, field="detail"):
    return Response({field: [message] if field != "detail" else message}, status=status.HTTP_400_BAD_REQUEST)


def password_problems(password, user):
    """The reasons a new password is unacceptable, or an empty list."""
    try:
        validate_password(password, user)
    except DjangoValidationError as exc:
        return list(exc.messages)
    return []


def social_failure(exc):
    return Response({"detail": str(exc), "code": exc.code}, status=exc.status)


def _confirm_identity(user, password, code=""):
    """Password, plus a second-factor code when two-factor is on. Returns an error response, or None if all is well."""
    if not user.has_usable_password():
        # An account that only ever signed in with Google or Apple has nothing to confirm with yet
        return bad("Set a password first: use 'Forgot password' to get a link, then try again.", "password")
    if not user.check_password(password):
        return bad("Incorrect password.", "password")
    if two_factor.has_two_factor(user):
        if not str(code or "").strip():
            return bad("Enter a code from your authenticator app, or a backup code.", "code")
        identifier = f"2fa:{user.pk}"
        wait = lockout.seconds_locked(identifier)
        if wait:
            return too_many(wait)
        if not two_factor.verify_second_factor(user, code):
            lockout.register_failure(identifier)
            return bad("That code is not right.", "code")
        lockout.clear(identifier)
    return None


class AuthScope:
    """Failed guesses are cheap to try, so every sensitive endpoint shares the sign-in rate limit."""

    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class EmailScope:
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "email"  # anything that sends an email: 5 an hour


# ---------- signing up and in ----------

class RegisterView(AuthScope, generics.CreateAPIView):
    """Create an account and email a verification link. No tokens: the user signs in after verifying."""

    serializer_class = RegisterSerializer
    permission_classes = [AllowAny]
    authentication_classes = []

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        mail.send_verification(user)
        return Response(
            {
                "user": MeSerializer(user).data,
                "detail": "Account created. We emailed you a link. Open it to verify your address, then sign in.",
            },
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    """Sign in with email and password. 403 until the email is verified. Two-factor users get a challenge for /login/2fa/ unless a remembered device_token is sent."""

    serializer_class = EmailLoginSerializer
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request, *args, **kwargs):
        typed = request.data.get("username") if hasattr(request.data, "get") else None
        identifier = str(typed or "").strip().lower()
        if identifier:
            wait = lockout.seconds_locked(identifier)
            if wait:  # locked: even the right password is refused until the lock runs out
                return too_many(wait)
        try:
            response = super().post(request, *args, **kwargs)
        except AuthenticationFailed:
            if identifier:
                lockout.register_failure(identifier)
            raise
        if identifier:
            lockout.clear(identifier)
        return response


class TwoFactorLoginView(AuthScope, APIView):
    """Second step of signing in: the challenge from the first step plus a code. Send remember_device to skip the code on this device for a while."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = TwoFactorLoginSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        user = two_factor.read_challenge(data["challenge"])
        if user is None:
            return refused("This sign-in step expired. Start again with your password.")
        if not user.email_verified:
            raise EmailNotVerified()
        identifier = f"2fa:{user.pk}"
        wait = lockout.seconds_locked(identifier)
        if wait:
            return too_many(wait)
        if not two_factor.verify_second_factor(user, data["code"]):
            lockout.register_failure(identifier)
            return refused("That code is not right.")
        lockout.clear(identifier)
        tokens = issue_tokens(user)
        if data.get("remember_device"):
            label = data.get("device_name") or request.META.get("HTTP_USER_AGENT", "")
            tokens["device_token"] = trusted_devices.remember(user, label)
            tokens["device_expires_in_days"] = settings.TRUSTED_DEVICE_DAYS
        return Response(tokens)


class RefreshView(TokenRefreshView):
    """Trade a refresh token for a new access token. Tokens issued before the last password change are refused."""

    serializer_class = RevocableRefreshSerializer


class MeView(generics.RetrieveUpdateAPIView):
    """The signed-in user's profile: timezone, theme, and whether email, password and two-factor are set up."""

    serializer_class = MeSerializer

    def get_object(self):
        return self.request.user


# ---------- social sign-in ----------

class SocialSignInView(AuthScope, APIView):
    """Sign in or sign up with a Google or Apple ID token obtained by the frontend. Send the same nonce you gave the provider."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request, provider):
        if provider not in social.PROVIDERS:
            raise Http404
        form = SocialSignInSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        try:
            identity = social.verify(provider, data["id_token"], data["nonce"])
            user, created = social.sign_in_user(identity, data.get("timezone"))
        except social.SocialError as exc:
            return social_failure(exc)
        if not user.email_verified:
            raise EmailNotVerified()
        if two_factor.has_two_factor(user) and not trusted_devices.is_trusted(user, data.get("device_token")):
            return Response({"two_factor_required": True, "challenge": two_factor.make_challenge(user)})
        return Response({**issue_tokens(user), "created": created})


class SocialStatusView(APIView):
    """Which providers are connected, whether the account has a password, and which providers the server is set up for."""

    def get(self, request):
        user = request.user
        return Response({
            "has_password": user.has_usable_password(),
            "linked": [
                {"provider": a.provider, "email": a.email, "linked_at": a.created_at}
                for a in user.social_accounts.order_by("provider")
            ],
            "available": [name for name in social.PROVIDERS if social.client_ids(name)],
        })


class SocialLinkView(AuthScope, APIView):
    """Connect Google or Apple to the signed-in account. Needs the password (and a code if two-factor is on)."""

    def post(self, request, provider):
        if provider not in social.PROVIDERS:
            raise Http404
        form = SocialLinkSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        problem = _confirm_identity(request.user, data["password"], data.get("code", ""))
        if problem:
            return problem
        try:
            identity = social.verify(provider, data["id_token"], data["nonce"])
            social.link(request.user, identity)
        except social.SocialError as exc:
            return social_failure(exc)
        return Response({"linked": True, "provider": provider})


class SocialUnlinkView(AuthScope, APIView):
    """Disconnect Google or Apple. Needs the password (and a code if two-factor is on), so a password is always left to sign in with."""

    def post(self, request, provider):
        if provider not in social.PROVIDERS:
            raise Http404
        form = SocialUnlinkSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        problem = _confirm_identity(request.user, data["password"], data.get("code", ""))
        if problem:
            return problem
        deleted, _ = request.user.social_accounts.filter(provider=provider).delete()
        if not deleted:
            return bad(f"{social.label(provider)} is not connected.")
        return Response({"linked": False, "provider": provider})


# ---------- remembered devices ----------

class DeviceListView(APIView):
    """Devices that skip the two-factor code. GET lists them, DELETE forgets all of them."""

    def get(self, request):
        devices = request.user.trusted_devices.filter(expires_at__gt=timezone.now())
        return Response([
            {
                "id": d.id, "label": d.label, "created_at": d.created_at,
                "last_used_at": d.last_used_at, "expires_at": d.expires_at,
            }
            for d in devices
        ])

    def delete(self, request):
        trusted_devices.revoke_all(request.user)
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeviceDetailView(APIView):
    """Forget one remembered device."""

    def delete(self, request, pk):
        deleted, _ = request.user.trusted_devices.filter(pk=pk).delete()
        if not deleted:
            raise Http404
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------- passwords ----------

class PasswordResetRequestView(EmailScope, APIView):
    """Email a one-hour reset link. The answer is the same whether or not the address has an account."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = PasswordResetRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        for user in User.objects.filter(email__iexact=form.validated_data["email"], is_active=True):
            mail.send_password_reset(user)
        return Response({"detail": "If that email has an account, a reset link is on its way."})


class PasswordResetConfirmView(AuthScope, APIView):
    """Set a new password using the uid and token from the emailed link. The link works once, verifies the email, and forgets remembered devices."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = PasswordResetConfirmSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        try:
            user = User.objects.get(pk=urlsafe_base64_decode(data["uid"]).decode(), is_active=True)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None
        if user is None or not default_token_generator.check_token(user, data["token"]):
            return bad("This reset link is invalid or has expired. Ask for a new one.")
        problems = password_problems(data["new_password"], user)
        if problems:
            return Response({"new_password": problems}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(data["new_password"])
        user.email_verified = True  # only the owner of the inbox could have opened the link
        user.save(update_fields=["password", "email_verified"])  # also ends every session issued before now
        trusted_devices.revoke_all(user)
        lockout.clear(user.email)  # a reset is the way out of a lockout
        lockout.clear(user.username)
        return Response({"detail": "Your password has been changed. Sign in with the new one."})


class PasswordChangeView(AuthScope, APIView):
    """Change the password while signed in. Needs the current one. Returns fresh tokens, because the old ones stop working."""

    def post(self, request):
        form = PasswordChangeSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        user = request.user
        if not user.has_usable_password():
            return bad("You don't have a password yet. Use 'Forgot password' to set one.", "current_password")
        if not user.check_password(data["current_password"]):
            return bad("Incorrect password.", "current_password")
        problems = password_problems(data["new_password"], user)
        if problems:
            return Response({"new_password": problems}, status=status.HTTP_400_BAD_REQUEST)
        user.set_password(data["new_password"])
        user.save(update_fields=["password"])
        trusted_devices.revoke_all(user)
        return Response({"detail": "Password changed.", **issue_tokens(user)})


# ---------- email verification ----------

class EmailVerifyResendView(EmailScope, APIView):
    """Send a new verification link. Open to anyone, since an unverified user can't sign in, and it reveals nothing about which addresses exist."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = EmailSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        unverified = User.objects.filter(
            email__iexact=form.validated_data["email"], is_active=True, email_verified=False
        )
        for user in unverified:
            mail.send_verification(user)
        return Response({"detail": "If that address has an account waiting to be verified, a new link is on its way."})


class EmailVerifyConfirmView(AuthScope, APIView):
    """Confirm an email address with the token from the emailed link."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = TokenSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        user = mail.read_verification_token(form.validated_data["token"])
        if user is None:
            return bad("This verification link is invalid or has expired.")
        if not user.email_verified:
            user.email_verified = True
            user.save(update_fields=["email_verified"])
        return Response({"detail": "Your email is verified. You can sign in now.", "email_verified": True})


# ---------- changing the email address ----------

class _Stale(Exception):
    """A change-email link that can no longer be used."""


class EmailChangeRequestView(EmailScope, APIView):
    """Start changing the account email. A link goes to the new address, and nothing changes until it is opened."""

    def post(self, request):
        form = EmailChangeRequestSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        data = form.validated_data
        user = request.user
        problem = _confirm_identity(user, data["password"], data.get("code", ""))
        if problem:
            return problem
        new = data["new_email"].strip().lower()
        if new == user.email:
            return bad("That is already your email address.", "new_email")
        # The answer is the same when the address belongs to someone else, so this can't be used to look people up
        if not User.objects.filter(email__iexact=new).exclude(pk=user.pk).exists():
            mail.send_email_change(user, new)
            mail.send_email_change_notice(user, new)
        return Response({"detail": "If that address can be used, a confirmation link is on its way to it."})


class EmailChangeConfirmView(AuthScope, APIView):
    """Finish the change with the token from the link sent to the new address."""

    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        form = TokenSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        found = mail.read_email_change_token(form.validated_data["token"])
        if found is None:
            return bad("This link is invalid or has expired. Start the change again.")
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().filter(pk=found["user_id"], is_active=True).first()
                if user is None or user.email != found["old"]:
                    raise _Stale("This link is no longer valid. Start the change again.")
                if User.objects.filter(email__iexact=found["new"]).exclude(pk=user.pk).exists():
                    raise _Stale("That address is already used by another account.")
                if user.username.lower() == user.email.lower():
                    user.username = found["new"]  # accounts made through sign-up log in with their email
                user.email = found["new"]
                user.email_verified = True  # opening the link proved the new inbox is theirs
                user.save(update_fields=["username", "email", "email_verified"])
        except _Stale as exc:
            return bad(str(exc))
        except IntegrityError:
            return bad("That address is already used by another account.")
        return Response({"detail": "Your email address has been changed. Sign in with the new one.", "email": user.email})


# ---------- two-factor settings ----------

def _check_password_and_code(request):
    """Shared by the endpoints that change two-factor settings. Returns an error response, or None if all is well."""
    form = PasswordAndCodeSerializer(data=request.data)
    form.is_valid(raise_exception=True)
    return _confirm_identity(request.user, form.validated_data["password"], form.validated_data["code"])


class TwoFactorStatusView(APIView):
    """Whether two-factor sign-in is on, and how many backup codes are left."""

    def get(self, request):
        record = TwoFactor.objects.filter(user=request.user).first()
        enabled = bool(record and record.confirmed)
        return Response({
            "enabled": enabled,
            "setup_pending": bool(record and not record.confirmed),
            "backup_codes_remaining": (
                BackupCode.objects.filter(user=request.user, used_at__isnull=True).count() if enabled else 0
            ),
        })


class TwoFactorSetupView(AuthScope, APIView):
    """Step 1 of turning two-factor on: returns the secret and the otpauth:// link to show as a QR code."""

    def post(self, request):
        form = PasswordSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        if two_factor.has_two_factor(request.user):
            return bad("Two-factor sign-in is already on. Turn it off first to set it up again.")
        problem = _confirm_identity(request.user, form.validated_data["password"])
        if problem:
            return problem
        secret = two_factor.new_secret()
        TwoFactor.objects.update_or_create(
            user=request.user,
            defaults={"secret": two_factor.encrypt(secret), "confirmed": False, "last_used_step": 0},
        )
        return Response({"secret": secret, "otpauth_uri": two_factor.provisioning_uri(secret, request.user.email)})


class TwoFactorEnableView(AuthScope, APIView):
    """Step 2: prove the app works by sending a current code. Returns 10 backup codes, shown only this once."""

    def post(self, request):
        form = CodeSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        record = TwoFactor.objects.filter(user=request.user, confirmed=False).first()
        if record is None:
            return bad("Start the setup first.")
        identifier = f"2fa:{request.user.pk}"
        wait = lockout.seconds_locked(identifier)
        if wait:
            return too_many(wait)
        try:
            step = two_factor.check_totp(two_factor.decrypt(record.secret), form.validated_data["code"], 0)
        except two_factor.InvalidToken:
            step = None
        if step is None:
            lockout.register_failure(identifier)
            return bad("That code is not right. Check the app and try again.", "code")
        lockout.clear(identifier)
        record.confirmed = True
        record.last_used_step = step
        record.save(update_fields=["confirmed", "last_used_step"])
        return Response({
            "enabled": True,
            "backup_codes": two_factor.issue_backup_codes(request.user),
            "detail": "Save these backup codes now. They are shown only once.",
        })


class TwoFactorDisableView(AuthScope, APIView):
    """Turn two-factor off. Needs the password and a current code (or a backup code). Forgets remembered devices."""

    def post(self, request):
        if not two_factor.has_two_factor(request.user):
            return bad("Two-factor sign-in is not on.")
        problem = _check_password_and_code(request)
        if problem:
            return problem
        TwoFactor.objects.filter(user=request.user).delete()
        BackupCode.objects.filter(user=request.user).delete()
        trusted_devices.revoke_all(request.user)
        return Response({"enabled": False})


class TwoFactorBackupCodesView(AuthScope, APIView):
    """Replace the backup codes with 10 new ones. Needs the password and a current code. The old ones stop working."""

    def post(self, request):
        if not two_factor.has_two_factor(request.user):
            return bad("Two-factor sign-in is not on.")
        problem = _check_password_and_code(request)
        if problem:
            return problem
        return Response({
            "backup_codes": two_factor.issue_backup_codes(request.user),
            "detail": "Save these backup codes now. They are shown only once.",
        })