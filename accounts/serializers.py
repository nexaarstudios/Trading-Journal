from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.db.models import Q
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer, TokenRefreshSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.utils import get_md5_hash_password

from . import trusted_devices, two_factor
from .exceptions import EmailNotVerified

User = get_user_model()


def _check_timezone(value):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise serializers.ValidationError("Use an IANA timezone name, e.g. Africa/Lagos.")
    return value


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, style={"input_type": "password"})

    class Meta:
        model = User
        fields = ["email", "password", "timezone"]
        extra_kwargs = {"timezone": {"required": False}}

    def validate_email(self, value):
        value = value.strip().lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate_timezone(self, value):
        return _check_timezone(value)

    def validate_password(self, value):
        validate_password(value)
        return value

    def create(self, validated_data):
        email = validated_data["email"]
        # Email is the login, so it doubles as the username
        return User.objects.create_user(
            username=email,
            email=email,
            password=validated_data["password"],
            timezone=validated_data.get("timezone", "UTC"),
        )


class MeSerializer(serializers.ModelSerializer):
    two_factor_enabled = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["id", "email", "timezone", "theme", "email_verified", "two_factor_enabled", "has_password"]
        read_only_fields = ["id", "email", "email_verified"]

    def get_two_factor_enabled(self, obj):
        return two_factor.has_two_factor(obj)

    def get_has_password(self, obj):
        return obj.has_usable_password()  # false for accounts that only ever signed in with Google or Apple

    def validate_timezone(self, value):
        return _check_timezone(value)


class EmailLoginSerializer(TokenObtainPairSerializer):
    """Clients send {"username": "<email>", "password": "...", "device_token"?: "..."}. The email is matched without regard to case."""

    def validate(self, attrs):
        typed = attrs["username"].strip()
        found = User.objects.filter(Q(username__iexact=typed) | Q(email__iexact=typed))
        if found.count() == 1:  # use the stored spelling, so users made in the admin work too
            attrs["username"] = found.first().username
        data = super().validate(attrs)  # raises if the password is wrong
        if not self.user.email_verified:
            raise EmailNotVerified()  # only ever said to someone who already knows the password
        if two_factor.has_two_factor(self.user):
            sent = self.initial_data.get("device_token") if hasattr(self.initial_data, "get") else None
            if not trusted_devices.is_trusted(self.user, sent):
                # The password was right, but no tokens until the second factor is proven
                return {"two_factor_required": True, "challenge": two_factor.make_challenge(self.user)}
            data["trusted_device"] = True  # a remembered device stands in for the code
        return data


class RevocableRefreshSerializer(TokenRefreshSerializer):
    """Refuse refresh tokens issued before the user's last password change, whichever simplejwt version is installed."""

    def validate(self, attrs):
        token = RefreshToken(attrs["refresh"])  # a bad or expired token raises, and the view answers 401
        user = User.objects.filter(pk=token.payload.get(api_settings.USER_ID_CLAIM), is_active=True).first()
        stamp = token.payload.get(api_settings.REVOKE_TOKEN_CLAIM)
        if user is None or stamp != get_md5_hash_password(user.password):
            raise AuthenticationFailed("The user's password has been changed.", code="password_changed")
        return super().validate(attrs)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    new_password = serializers.CharField(trim_whitespace=False)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False)


class TokenSerializer(serializers.Serializer):
    token = serializers.CharField()


class TwoFactorLoginSerializer(serializers.Serializer):
    challenge = serializers.CharField()
    code = serializers.CharField()
    remember_device = serializers.BooleanField(required=False, default=False)
    device_name = serializers.CharField(required=False, allow_blank=True, max_length=80)


class PasswordSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)


class CodeSerializer(serializers.Serializer):
    code = serializers.CharField()


class PasswordAndCodeSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField()


class EmailSerializer(serializers.Serializer):
    email = serializers.EmailField()


class EmailChangeRequestSerializer(serializers.Serializer):
    new_email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(required=False, allow_blank=True)  # needed when two-factor is on


class SocialSignInSerializer(serializers.Serializer):
    id_token = serializers.CharField(max_length=8192)
    nonce = serializers.CharField(max_length=256)  # the random string the frontend gave the provider
    timezone = serializers.CharField(required=False)  # used only when this sign-in creates the account
    device_token = serializers.CharField(required=False, allow_blank=True)

    def validate_timezone(self, value):
        return _check_timezone(value)


class SocialLinkSerializer(serializers.Serializer):
    id_token = serializers.CharField(max_length=8192)
    nonce = serializers.CharField(max_length=256)
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(required=False, allow_blank=True)


class SocialUnlinkSerializer(serializers.Serializer):
    password = serializers.CharField(trim_whitespace=False)
    code = serializers.CharField(required=False, allow_blank=True)