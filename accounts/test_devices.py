from datetime import timedelta
from unittest import mock

import pyotp
from django.contrib.auth.tokens import default_token_generator
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from . import trusted_devices, two_factor
from .models import TrustedDevice, TwoFactor
from .test_social import SocialTestCase
from .tests import NEW_PASSWORD, PASSWORD


class DeviceTests(SocialTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.secret = pyotp.random_base32()
        TwoFactor.objects.create(user=self.user, secret=two_factor.encrypt(self.secret), confirmed=True)
        self.clock = 1_800_000_000
        patcher = mock.patch("accounts.two_factor._now", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def code(self):
        return pyotp.TOTP(self.secret).at(self.clock)

    def sign_in_with_code(self, password=PASSWORD, **extra):
        challenge = self.login(password=password).json()["challenge"]
        body = {"challenge": challenge, "code": self.code(), **extra}
        res = self.client.post("/api/auth/login/2fa/", body, format="json")
        self.clock += 30  # the next code is a new one
        return res

    def password_login(self, token, email="sam@example.com", password=PASSWORD):
        body = {"username": email, "password": password, "device_token": token}
        return self.client.post("/api/auth/login/", body, format="json")

    def test_a_remembered_device_skips_the_code_next_time(self):
        plain = self.sign_in_with_code()
        self.assertNotIn("device_token", plain.json())  # not asked to remember, so nothing is handed out

        res = self.sign_in_with_code(remember_device=True, device_name="My laptop")
        self.assertEqual(res.status_code, 200)
        token = res.json()["device_token"]
        self.assertEqual(res.json()["device_expires_in_days"], 30)
        device = TrustedDevice.objects.get(user=self.user)
        self.assertNotEqual(device.token_hash, token)  # only a hash is stored
        self.assertEqual(device.label, "My laptop")

        trusted = self.password_login(token)
        self.assertEqual(trusted.status_code, 200)
        self.assertIn("access", trusted.json())
        self.assertTrue(trusted.json()["trusted_device"])
        for other in ("", "garbage", token + "x"):
            self.assertTrue(self.password_login(other).json()["two_factor_required"], other)
        self.assertEqual(self.password_login(token, password="nope").status_code, 401)  # the password is still needed

    def test_one_users_token_means_nothing_to_another(self):
        kim = self.make_user("kim@example.com")
        TwoFactor.objects.create(user=kim, secret=two_factor.encrypt(pyotp.random_base32()), confirmed=True)
        token = trusted_devices.remember(self.user, "Laptop")
        res = self.password_login(token, email="kim@example.com")
        self.assertTrue(res.json()["two_factor_required"])

    def test_listing_and_forgetting_devices(self):
        first = trusted_devices.remember(self.user, "Laptop")
        trusted_devices.remember(self.user, "Phone")
        client = self.authed(self.sign_in_with_code().json()["access"])
        listed = client.get("/api/auth/devices/").json()
        self.assertEqual(sorted(d["label"] for d in listed), ["Laptop", "Phone"])
        self.assertEqual(set(listed[0]), {"id", "label", "created_at", "last_used_at", "expires_at"})

        laptop = next(d for d in listed if d["label"] == "Laptop")
        self.assertEqual(client.delete(f"/api/auth/devices/{laptop['id']}/").status_code, 204)
        self.assertTrue(self.password_login(first).json()["two_factor_required"])  # the code is asked for again
        self.assertEqual(client.delete(f"/api/auth/devices/{laptop['id']}/").status_code, 404)

        stranger = self.make_user("kim@example.com")
        theirs = TrustedDevice.objects.create(
            user=stranger, token_hash="x" * 64, expires_at=timezone.now() + timedelta(days=1)
        )
        self.assertEqual(client.delete(f"/api/auth/devices/{theirs.pk}/").status_code, 404)  # not yours to forget

        self.assertEqual(client.delete("/api/auth/devices/").status_code, 204)
        self.assertEqual(client.get("/api/auth/devices/").json(), [])
        self.assertTrue(TrustedDevice.objects.filter(pk=theirs.pk).exists())
        self.assertEqual(APIClient().get("/api/auth/devices/").status_code, 401)

    def test_expired_devices_are_ignored(self):
        token = trusted_devices.remember(self.user, "Old laptop")
        TrustedDevice.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertTrue(self.password_login(token).json()["two_factor_required"])
        client = self.authed(self.sign_in_with_code().json()["access"])
        self.assertEqual(client.get("/api/auth/devices/").json(), [])

    def test_password_changes_resets_and_turning_two_factor_off_forget_every_device(self):
        token = trusted_devices.remember(self.user, "Laptop")
        client = self.authed(self.password_login(token).json()["access"])
        change = client.post(
            "/api/auth/password/change/", {"current_password": PASSWORD, "new_password": NEW_PASSWORD}, format="json"
        )
        self.assertEqual(change.status_code, 200)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

        trusted_devices.remember(self.user, "Laptop")
        self.user.refresh_from_db()
        reset_password = "yet-an0ther-Str0ng-one!"
        reset = self.client.post(
            "/api/auth/password-reset/confirm/",
            {
                "uid": urlsafe_base64_encode(force_bytes(self.user.pk)),
                "token": default_token_generator.make_token(self.user),
                "new_password": reset_password,
            },
            format="json",
        )
        self.assertEqual(reset.status_code, 200)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

        client = self.authed(self.sign_in_with_code(password=reset_password).json()["access"])
        trusted_devices.remember(self.user, "Laptop")
        off = client.post("/api/auth/2fa/disable/", {"password": reset_password, "code": self.code()}, format="json")
        self.assertEqual(off.status_code, 200, off.content)
        self.assertEqual(TrustedDevice.objects.filter(user=self.user).count(), 0)

    def test_only_the_newest_devices_are_kept(self):
        for number in range(12):
            trusted_devices.remember(self.user, f"Device {number}")
        labels = set(TrustedDevice.objects.filter(user=self.user).values_list("label", flat=True))
        self.assertEqual(len(labels), 10)
        self.assertNotIn("Device 0", labels)
        self.assertNotIn("Device 1", labels)
        self.assertIn("Device 11", labels)

    def test_social_sign_in_honours_a_remembered_device(self):
        google_user = self.make_user("sam@gmail.com")
        TwoFactor.objects.create(
            user=google_user, secret=two_factor.encrypt(pyotp.random_base32()), confirmed=True
        )
        self.assertTrue(self.social().json()["two_factor_required"])
        token = trusted_devices.remember(google_user, "Laptop")
        res = self.social(device_token=token)
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.json())