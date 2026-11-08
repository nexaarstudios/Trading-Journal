import hashlib
import time
from unittest import mock

import jwt
import pyotp
from cryptography.hazmat.primitives.asymmetric import rsa
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import override_settings

from . import two_factor
from .models import SocialAccount, TwoFactor
from .tests import PASSWORD, ApiTestCase, email_params

User = get_user_model()
PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
NONCE = "client-made-random-nonce"
ISSUERS = {
    "google": ("https://accounts.google.com", "google-client"),
    "apple": ("https://appleid.apple.com", "apple-client"),
}


def make_token(provider="google", key=PRIVATE_KEY, **overrides):
    """An ID token signed with a test key. A claim set to None is left out."""
    issuer, audience = ISSUERS[provider]
    now = int(time.time())
    claims = {
        "iss": issuer, "aud": audience, "sub": "subject-1", "email": "sam@gmail.com",
        "email_verified": True, "nonce": NONCE, "iat": now, "exp": now + 3600,
    }
    claims.update(overrides)
    claims = {name: value for name, value in claims.items() if value is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


@override_settings(
    GOOGLE_CLIENT_IDS=["google-client"], APPLE_CLIENT_IDS=["apple-client"], FRONTEND_URL="https://app.example.com"
)
class SocialTestCase(ApiTestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch(
            "accounts.social._signing_key", side_effect=lambda provider, token: PRIVATE_KEY.public_key()
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def social(self, provider="google", token=None, nonce=NONCE, **extra):
        body = {"id_token": token or make_token(provider), "nonce": nonce, **extra}
        return self.client.post(f"/api/auth/social/{provider}/", body, format="json")


class SocialSignInTests(SocialTestCase):
    def test_a_new_gmail_user_gets_an_account_and_tokens(self):
        res = self.social()
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body["created"])
        self.assertIn("access", body)
        self.assertIn("refresh", body)
        user = User.objects.get(email="sam@gmail.com")
        self.assertTrue(user.email_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(SocialAccount.objects.get(user=user).subject, "subject-1")
        me = self.authed(body["access"]).get("/api/auth/me/").json()
        self.assertEqual((me["has_password"], me["email_verified"]), (False, True))
        self.assertEqual(len(mail.outbox), 0)

        again = self.social().json()
        self.assertFalse(again["created"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(self.login("sam@gmail.com", "anything").status_code, 401)  # there is no password to use

    def test_apple_sends_booleans_as_strings_and_can_hide_the_email(self):
        token = make_token(
            "apple", sub="apple-1", email="abc123@privaterelay.appleid.com",
            email_verified="true", is_private_email="true",
        )
        res = self.social("apple", token)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(User.objects.get(email="abc123@privaterelay.appleid.com").email_verified)
        self.assertEqual(SocialAccount.objects.get().provider, "apple")

    def test_an_existing_verified_account_is_connected_not_duplicated(self):
        user = self.make_user("sam@gmail.com")
        res = self.social()
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.json()["created"])
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(SocialAccount.objects.get().user, user)
        self.assertEqual(self.login("sam@gmail.com").status_code, 200)  # the password still works

    def test_an_unverified_squatter_loses_the_account_to_the_real_owner(self):
        squatter = self.make_user("sam@gmail.com", verified=False)
        self.assertEqual(self.social().status_code, 200)
        squatter.refresh_from_db()
        self.assertTrue(squatter.email_verified)
        self.assertFalse(squatter.has_usable_password())
        self.assertEqual(self.login("sam@gmail.com").status_code, 401)  # the squatter's password is dead

    def test_google_addresses_google_does_not_own_never_connect_to_an_existing_account(self):
        self.make_user("sam@company.example")
        res = self.social(token=make_token(email="sam@company.example"))
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["code"], "account_exists")
        self.assertEqual(SocialAccount.objects.count(), 0)

    def test_new_accounts_from_those_addresses_must_verify_by_email(self):
        token = make_token(email="new@company.example")
        res = self.social(token=token)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.json()["code"], "email_not_verified")
        self.assertNotIn("access", res.json())
        self.assertFalse(User.objects.get(email="new@company.example").email_verified)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.social(token=token).status_code, 403)  # still refused until the link is used
        verify = self.client.post(
            "/api/auth/verify-email/confirm/", {"token": email_params()["token"]}, format="json"
        )
        self.assertEqual(verify.status_code, 200)
        self.assertEqual(self.social(token=token).status_code, 200)

    def test_workspace_accounts_are_trusted(self):
        res = self.social(token=make_token(email="sam@company.example", hd="company.example"))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(User.objects.get(email="sam@company.example").email_verified)

    def test_bad_tokens_are_refused(self):
        now = int(time.time())
        cases = {
            "wrong audience": make_token(aud="someone-elses-client"),
            "wrong issuer": make_token(iss="https://evil.example"),
            "expired": make_token(exp=now - 600, iat=now - 4000),
            "no nonce in the token": make_token(nonce=None),
            "a different nonce": make_token(nonce="not-the-clients"),
            "email not verified": make_token(email_verified=False),
            "no email": make_token(email=None),
            "signed with another key": make_token(key=OTHER_KEY),
            "no subject": make_token(sub=None),
        }
        for name, token in cases.items():
            res = self.social(token=token)
            self.assertEqual(res.status_code, 400, name)
            self.assertEqual(res.json()["code"], "invalid_identity", name)
        self.assertEqual(User.objects.count(), 0)

    def test_unknown_providers_and_garbage(self):
        unknown = self.client.post("/api/auth/social/facebook/", {"id_token": "x", "nonce": "y"}, format="json")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(self.social(token="not-a-jwt").status_code, 400)
        self.assertEqual(self.client.post("/api/auth/social/google/", {"nonce": "y"}, format="json").status_code, 400)

    def test_the_nonce_may_be_the_hash_of_what_the_client_sent(self):
        token = make_token(nonce=hashlib.sha256(NONCE.encode()).hexdigest())
        self.assertEqual(self.social(token=token).status_code, 200)

    def test_a_provider_that_is_not_set_up(self):
        with override_settings(GOOGLE_CLIENT_IDS=[]):
            res = self.social()
        self.assertEqual((res.status_code, res.json()["code"]), (503, "provider_not_configured"))

    def test_a_provider_outage(self):
        with mock.patch("accounts.social._signing_key", side_effect=OSError("down")):
            res = self.social()
        self.assertEqual((res.status_code, res.json()["code"]), (503, "provider_unavailable"))

    def test_two_factor_still_applies(self):
        user = self.make_user("sam@gmail.com")
        secret = pyotp.random_base32()
        TwoFactor.objects.create(user=user, secret=two_factor.encrypt(secret), confirmed=True)
        first = self.social().json()
        self.assertTrue(first["two_factor_required"])
        self.assertNotIn("access", first)
        moment = 1_800_000_000
        with mock.patch("accounts.two_factor._now", return_value=moment):
            done = self.client.post(
                "/api/auth/login/2fa/",
                {"challenge": first["challenge"], "code": pyotp.TOTP(secret).at(moment)}, format="json",
            )
        self.assertEqual(done.status_code, 200)
        self.assertIn("access", done.json())

    def test_disabled_accounts_are_refused(self):
        inactive = self.make_user("sam@gmail.com")
        inactive.is_active = False
        inactive.save()
        self.assertEqual(self.social().status_code, 400)  # an existing account that is switched off

        self.assertEqual(self.social(token=make_token(sub="subject-2", email="kim@gmail.com")).status_code, 200)
        User.objects.filter(email="kim@gmail.com").update(is_active=False)
        again = self.social(token=make_token(sub="subject-2", email="kim@gmail.com"))
        self.assertEqual(again.status_code, 400)  # a connected account that was switched off later

    def test_the_timezone_is_used_only_for_new_accounts(self):
        self.assertEqual(self.social(timezone="Africa/Lagos").status_code, 200)
        user = User.objects.get(email="sam@gmail.com")
        self.assertEqual(user.timezone, "Africa/Lagos")
        self.social(timezone="Europe/Paris")
        user.refresh_from_db()
        self.assertEqual(user.timezone, "Africa/Lagos")
        self.assertEqual(self.social(timezone="Mars/Base").status_code, 400)


class SocialLinkTests(SocialTestCase):
    def link(self, client, provider="google", password=PASSWORD, **extra):
        token = extra.pop("token", None) or make_token(provider)
        body = {"id_token": token, "nonce": NONCE, "password": password, **extra}
        return client.post(f"/api/auth/social/{provider}/link/", body, format="json")

    def test_connecting_and_disconnecting(self):
        self.make_user()
        client = self.authed(self.signed_in()["access"])
        other_google = make_token(email="other@elsewhere.example", sub="g-9")

        self.assertEqual(self.link(client, password="nope").status_code, 400)
        self.assertEqual(self.link(client, token=other_google).status_code, 200)  # any address: they are signed in
        self.assertEqual(self.link(client, token=other_google).status_code, 200)  # connecting twice is harmless
        self.assertEqual(self.link(client).status_code, 400)  # a different Google account is already connected

        kim = self.make_user("kim@example.com")
        SocialAccount.objects.create(user=kim, provider="apple", subject="apple-77", email="kim@example.com")
        taken = self.link(client, "apple", token=make_token("apple", sub="apple-77"))
        self.assertEqual(taken.status_code, 400)  # already someone else's

        status = client.get("/api/auth/social/").json()
        self.assertTrue(status["has_password"])
        self.assertEqual([(a["provider"], a["email"]) for a in status["linked"]], [("google", "other@elsewhere.example")])
        self.assertEqual(status["available"], ["google", "apple"])

        unlink = "/api/auth/social/google/unlink/"
        self.assertEqual(client.post(unlink, {"password": "nope"}, format="json").status_code, 400)
        self.assertEqual(client.post(unlink, {"password": PASSWORD}, format="json").status_code, 200)
        self.assertEqual(client.get("/api/auth/social/").json()["linked"], [])
        self.assertEqual(client.post(unlink, {"password": PASSWORD}, format="json").status_code, 400)  # not connected

    def test_accounts_without_a_password_must_set_one_first(self):
        tokens = self.social().json()  # created through Google, so it has no password
        client = self.authed(tokens["access"])
        self.assertEqual(self.link(client, "apple", token=make_token("apple", sub="a-1")).status_code, 400)
        self.assertEqual(client.post("/api/auth/social/google/unlink/", {"password": ""}, format="json").status_code, 400)
        self.assertEqual(
            client.post("/api/auth/password/change/", {"current_password": "x", "new_password": PASSWORD}, format="json").status_code,
            400,
        )
        self.assertEqual(
            client.post("/api/auth/2fa/setup/", {"password": ""}, format="json").status_code, 400
        )

        user = User.objects.get(email="sam@gmail.com")  # a password set the way a real user would: through a reset
        user.set_password(PASSWORD)
        user.save()
        client = self.authed(self.social().json()["access"])  # the old tokens ended with the password change
        self.assertEqual(self.link(client, "apple", token=make_token("apple", sub="a-1")).status_code, 200)
        self.assertEqual(client.post("/api/auth/social/google/unlink/", {"password": PASSWORD}, format="json").status_code, 200)
        status = client.get("/api/auth/social/").json()
        self.assertEqual(([a["provider"] for a in status["linked"]], status["has_password"]), (["apple"], True))

    def test_a_login_is_required(self):
        from rest_framework.test import APIClient

        anonymous = APIClient()
        self.assertEqual(anonymous.get("/api/auth/social/").status_code, 401)
        for path in ("google/link/", "google/unlink/"):
            self.assertEqual(anonymous.post(f"/api/auth/social/{path}", {}, format="json").status_code, 401, path)