from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models.functions import Lower


class UserManager(DjangoUserManager):
    def create_superuser(self, username, email=None, password=None, **extra_fields):
        extra_fields.setdefault("email_verified", True)  # staff made on the command line need no email round trip
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    class Theme(models.TextChoices):
        SYSTEM = "system", "System"
        LIGHT = "light", "Light"
        DARK = "dark", "Dark"

    email = models.EmailField(unique=True)
    timezone = models.CharField(max_length=64, default="UTC")
    theme = models.CharField(max_length=10, choices=Theme.choices, default=Theme.SYSTEM)
    email_verified = models.BooleanField(default=False)

    objects = UserManager()

    class Meta(AbstractUser.Meta):
        constraints = [
            # Backs up the lowercase rule below: no two emails may differ only by capitals
            models.UniqueConstraint(Lower("email"), name="unique_email_ignoring_case"),
        ]

    def save(self, *args, **kwargs):
        self.email = (self.email or "").strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.email


class TwoFactor(models.Model):
    """A user's authenticator-app setup. The secret is stored encrypted."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="two_factor")
    secret = models.TextField(editable=False)
    confirmed = models.BooleanField(default=False)  # becomes true once the user proves the app works
    last_used_step = models.BigIntegerField(default=0)  # the 30-second window of the last accepted code
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.user_id} ({'on' if self.confirmed else 'pending'})"


class BackupCode(models.Model):
    """One-time codes for when the phone is lost. Only a keyed hash is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="backup_codes")
    code_hash = models.CharField(max_length=64, unique=True)
    used_at = models.DateTimeField(null=True, blank=True)


class SocialAccount(models.Model):
    """A sign-in identity at an outside provider (Google or Apple) connected to a user."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="social_accounts")
    provider = models.CharField(max_length=10)
    subject = models.CharField(max_length=255)  # the provider's own stable id for the person
    email = models.EmailField(blank=True)       # the address the provider reported when it was connected
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["provider", "subject"], name="unique_social_identity"),
            models.UniqueConstraint(fields=["user", "provider"], name="one_link_per_provider_per_user"),
        ]

    def __str__(self):
        return f"{self.provider}:{self.user_id}"


class TrustedDevice(models.Model):
    """A browser the user chose to remember, so it can skip the two-factor code. Only a keyed hash is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="trusted_devices")
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        ordering = ["-created_at", "-id"]