import re
import time
from unittest import mock
from urllib.parse import parse_qs, unquote, urlparse

import pyotp
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework.test import APIClient

from . import two_factor
from .models import BackupCode, TwoFactor

User = get_user_model()
PASSWORD = "a-Str0ng-pass!"
NEW_PASSWORD = "an0ther-Str0ng-pass!"


def email_params(message=None):
    """The query parameters of the link in an email (the newest one unless told otherwise)."""
    body = (message or mail.outbox[-1]).body
    url = re.search(r"https?://\S+", body).group(0)
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


def sent_to(address):
    return next(message for message in mail.outbox if message.to == [address])


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()  # throttles and lockouts live in the cache
        self.client = APIClient()

    def make_user(self, email="sam@example.com", password=PASSWORD, verified=True, **extra):
        return User.objects.create_user(email, email, password, email_verified=verified, **extra)

    def authed(self, token):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        return client

    def register(self, email="sam@example.com", password=PASSWORD):
        return self.client.post("/api/auth/register/", {"email": email, "password": password}, format="json")

    def login(self, email="sam@example.com", password=PASSWORD):
        return self.client.post("/api/auth/login/", {"username": email, "password": password}, format="json")

    def signed_in(self, email="sam@example.com", password=PASSWORD):
        res = self.login(email, password)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()


class AuthTests(ApiTestCase):
    def test_register_makes_an_unverified_account_and_no_tokens(self):
        res = self.register(email="Sam@Example.com")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(body["user"]["email"], "sam@example.com")
        self.assertEqual((body["user"]["email_verified"], body["user"]["two_factor_enabled"]), (False, False))
        self.assertNotIn("access", body)
        self.assertNotIn("refresh", body)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["sam@example.com"])

    def test_duplicate_email_rejected_case_insensitively(self):
        self.register()
        self.assertEqual(self.register(email="SAM@example.com").status_code, 400)

    def test_weak_password_rejected(self):
        self.assertEqual(self.register(password="12345678").status_code, 400)

    def test_unverified_users_cannot_sign_in(self):
        self.register()
        for _ in range(6):  # the right password, again and again: refused, but never counted as a failed guess
            res = self.login()
            self.assertEqual(res.status_code, 403)
            self.assertEqual(res.json()["code"], "email_not_verified")
            self.assertNotIn("access", res.json())
        self.assertEqual(self.login(password="nope").status_code, 401)  # a wrong password is still just wrong

    def test_signing_in_works_once_the_link_is_used(self):
        self.register()
        self.assertEqual(self.login().status_code, 403)
        confirm = self.client.post(
            "/api/auth/verify-email/confirm/", {"token": email_params()["token"]}, format="json"
        )
        self.assertEqual(confirm.status_code, 200)
        tokens = self.login()
        self.assertEqual(tokens.status_code, 200)
        me = self.authed(tokens.json()["access"]).get("/api/auth/me/").json()
        self.assertTrue(me["email_verified"])

    def test_superusers_made_on_the_command_line_are_verified(self):
        admin = User.objects.create_superuser("admin", "admin@example.com", "pw-12345-Admin!")
        self.assertTrue(admin.email_verified)
        self.assertFalse(User.objects.create_user("plain", "plain@example.com", PASSWORD).email_verified)

    def test_login_with_email_any_case(self):
        self.make_user()
        res = self.login("SAM@example.com")
        self.assertEqual(res.status_code, 200)
        self.assertIn("access", res.json())

    def test_wrong_password_rejected(self):
        self.make_user()
        self.assertEqual(self.login(password="nope").status_code, 401)

    def test_token_unlocks_protected_endpoints(self):
        self.make_user()
        access = self.signed_in()["access"]
        self.assertEqual(self.client.get("/api/trades/").status_code, 401)
        self.assertEqual(self.authed(access).get("/api/trades/").status_code, 200)

    def test_me_can_update_timezone_and_theme(self):
        self.make_user()
        client = self.authed(self.signed_in()["access"])
        res = client.patch("/api/auth/me/", {"timezone": "Africa/Lagos", "theme": "dark"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["timezone"], "Africa/Lagos")
        self.assertEqual(client.patch("/api/auth/me/", {"timezone": "Mars/Base"}, format="json").status_code, 400)
        locked = client.patch("/api/auth/me/", {"email_verified": False, "email": "x@example.com"}, format="json")
        self.assertEqual((locked.json()["email_verified"], locked.json()["email"]), (True, "sam@example.com"))

    def test_login_is_rate_limited(self):
        for _ in range(10):
            self.login("x@example.com", "bad")
        self.assertEqual(self.login("x@example.com", "bad").status_code, 429)

    def test_login_works_for_users_made_outside_the_register_endpoint(self):
        User.objects.create_user("Trader@Mail.com", "Trader@Mail.com", "duolingo-1", email_verified=True)
        for typed in ("Trader@Mail.com", "trader@mail.com", "TRADER@MAIL.COM", "  trader@mail.com "):
            self.assertEqual(self.login(typed, "duolingo-1").status_code, 200, typed)
        self.assertEqual(self.login("trader@mail.com", "nope").status_code, 401)

    def test_emails_are_stored_lowercase_and_cannot_collide_by_case(self):
        User.objects.create_user("one", "Same@Example.com", PASSWORD)
        self.assertEqual(User.objects.get(username="one").email, "same@example.com")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.create_user("two", "SAME@example.com", PASSWORD)

    def test_refresh_gives_a_new_access_token_and_rejects_bad_ones(self):
        self.make_user()
        tokens = self.signed_in()
        refreshed = self.client.post("/api/auth/refresh/", {"refresh": tokens["refresh"]}, format="json")
        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(self.authed(refreshed.json()["access"]).get("/api/auth/me/").status_code, 200)
        for bad in ("garbage", tokens["access"], ""):  # a made-up token, an access token, and nothing
            res = self.client.post("/api/auth/refresh/", {"refresh": bad}, format="json")
            self.assertIn(res.status_code, (400, 401), bad)


@override_settings(FRONTEND_URL="https://app.example.com")
class PasswordResetTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user("sam", "sam@example.com", PASSWORD, email_verified=True)

    def ask(self, email="sam@example.com"):
        return self.client.post("/api/auth/password-reset/", {"email": email}, format="json")

    def confirm(self, uid, token, password=NEW_PASSWORD):
        body = {"uid": uid, "token": token, "new_password": password}
        return self.client.post("/api/auth/password-reset/confirm/", body, format="json")

    def link(self):
        params = email_params()
        return params["uid"], params["token"]

    def test_same_answer_for_known_and_unknown_emails(self):
        known, unknown = self.ask(), self.ask("nobody@example.com")
        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["sam@example.com"])
        self.assertIn("https://app.example.com/reset-password?uid=", mail.outbox[0].body)
        self.assertEqual(self.ask("not-an-email").status_code, 400)

        self.user.is_active = False
        self.user.save()
        self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(len(mail.outbox), 1)  # no email for a disabled account

    def test_asking_is_limited_to_five_an_hour(self):
        for _ in range(5):
            self.assertEqual(self.ask().status_code, 200)
        self.assertEqual(self.ask().status_code, 429)
        self.assertEqual(len(mail.outbox), 5)

    def test_a_mail_outage_does_not_change_the_answer(self):
        with mock.patch("accounts.mail.send_mail", side_effect=OSError("down")):
            self.assertEqual(self.ask().status_code, 200)

    def test_reset_changes_the_password_and_ends_old_sessions(self):
        old = self.login().json()
        self.ask()
        uid, token = self.link()
        self.assertEqual(self.confirm(uid, token).status_code, 200)
        self.assertEqual(self.login(password=PASSWORD).status_code, 401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)
        self.assertEqual(self.authed(old["access"]).get("/api/auth/me/").status_code, 401)
        refresh = self.client.post("/api/auth/refresh/", {"refresh": old["refresh"]}, format="json")
        self.assertEqual(refresh.status_code, 401)
        self.assertEqual(self.confirm(uid, token, "yet-an0ther-Str0ng-one!").status_code, 400)  # works once

    def test_bad_links_and_weak_passwords(self):
        self.ask()
        uid, token = self.link()
        for bad_uid, bad_token in ((uid, token[:-2] + "xx"), ("bad", token), ("", token), (uid, "")):
            self.assertEqual(self.confirm(bad_uid, bad_token).status_code, 400, (bad_uid, bad_token))
        other = User.objects.create_user("other", "other@example.com", PASSWORD)
        other_uid = urlsafe_base64_encode(force_bytes(other.pk))
        self.assertEqual(self.confirm(other_uid, token).status_code, 400)  # a link only works for its own user
        for weak in ("12345678", "short", "password"):
            self.assertEqual(self.confirm(uid, token, weak).status_code, 400, weak)
        self.assertEqual(self.confirm(uid, token).status_code, 200)  # failed tries don't use the link up

        with override_settings(PASSWORD_RESET_TIMEOUT=-1):  # expired
            self.ask()
            uid2, token2 = self.link()
            self.assertEqual(self.confirm(uid2, token2, "once-m0re-Str0ng-one!").status_code, 400)

    def test_reset_unlocks_a_locked_account(self):
        for _ in range(5):
            self.login(password="wrong")
        self.assertEqual(self.login().status_code, 429)
        self.ask()
        uid, token = self.link()
        self.assertEqual(self.confirm(uid, token).status_code, 200)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)

    def test_reset_does_not_skip_two_factor(self):
        TwoFactor.objects.create(
            user=self.user, secret=two_factor.encrypt(pyotp.random_base32()), confirmed=True
        )
        self.ask()
        uid, token = self.link()
        self.assertEqual(self.confirm(uid, token).status_code, 200)
        body = self.login(password=NEW_PASSWORD).json()
        self.assertTrue(body["two_factor_required"])
        self.assertNotIn("access", body)

    def test_a_reset_also_verifies_the_email(self):
        User.objects.filter(pk=self.user.pk).update(email_verified=False)
        self.assertEqual(self.login().status_code, 403)
        self.ask()
        uid, token = self.link()
        self.assertEqual(self.confirm(uid, token).status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.email_verified)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)


class PasswordChangeTests(ApiTestCase):
    def test_change_password(self):
        self.make_user()
        tokens = self.signed_in()
        client = self.authed(tokens["access"])
        path = "/api/auth/password/change/"

        def change(current, new):
            return client.post(path, {"current_password": current, "new_password": new}, format="json")

        self.assertEqual(change("nope", NEW_PASSWORD).status_code, 400)
        self.assertEqual(change(PASSWORD, "12345678").status_code, 400)
        ok = change(PASSWORD, NEW_PASSWORD)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(client.get("/api/auth/me/").status_code, 401)  # the old token is dead
        self.assertEqual(self.authed(ok.json()["access"]).get("/api/auth/me/").status_code, 200)
        self.assertEqual(self.login(password=PASSWORD).status_code, 401)
        self.assertEqual(self.login(password=NEW_PASSWORD).status_code, 200)
        self.assertEqual(APIClient().post(path, {}, format="json").status_code, 401)


@override_settings(FRONTEND_URL="https://app.example.com")
class EmailVerificationTests(ApiTestCase):
    def confirm(self, token):
        return self.client.post("/api/auth/verify-email/confirm/", {"token": token}, format="json")

    def resend(self, email="sam@example.com"):
        return self.client.post("/api/auth/verify-email/resend/", {"email": email}, format="json")

    def test_the_link_in_the_welcome_email_verifies_the_address(self):
        self.register()
        self.assertIn("https://app.example.com/verify-email?token=", mail.outbox[0].body)
        params = email_params()
        self.assertEqual(list(params), ["token"])
        self.assertEqual(self.confirm(params["token"]).status_code, 200)
        self.assertTrue(User.objects.get(email="sam@example.com").email_verified)
        self.assertEqual(self.confirm(params["token"]).status_code, 200)  # a second click is harmless

    def test_bad_links(self):
        self.register()
        token = email_params()["token"]
        for bad_token in (token[:-2] + "xx", "garbage", ""):
            self.assertEqual(self.confirm(bad_token).status_code, 400, bad_token)
        with override_settings(EMAIL_VERIFICATION_MAX_AGE=-1):
            self.assertEqual(self.confirm(token).status_code, 400)  # expired
        User.objects.filter(email="sam@example.com").update(email="new@example.com")
        self.assertEqual(self.confirm(token).status_code, 400)  # the email changed since the link was sent

    def test_resending_is_open_to_anyone_and_reveals_nothing(self):
        self.register()  # one email so far
        known, unknown = self.resend(), self.resend("ghost@example.com")
        self.assertEqual((known.status_code, unknown.status_code), (200, 200))
        self.assertEqual(known.json(), unknown.json())
        self.assertEqual([m.to for m in mail.outbox], [["sam@example.com"], ["sam@example.com"]])
        self.assertEqual(self.resend("not-an-email").status_code, 400)

        self.confirm(email_params()["token"])
        self.resend()
        self.assertEqual(len(mail.outbox), 2)  # verified: nothing more to send
        User.objects.filter(email="sam@example.com").update(email_verified=False, is_active=False)
        self.resend()
        self.assertEqual(len(mail.outbox), 2)  # a disabled account gets nothing either

    def test_resending_is_limited_and_a_mail_outage_does_not_break_signup(self):
        self.register()
        for _ in range(5):
            self.assertEqual(self.resend().status_code, 200)
        self.assertEqual(self.resend().status_code, 429)
        with mock.patch("accounts.mail.send_mail", side_effect=OSError("down")):
            self.assertEqual(self.register(email="kim@example.com").status_code, 201)


@override_settings(FRONTEND_URL="https://app.example.com")
class EmailChangeTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.client_a = self.authed(self.signed_in()["access"])

    def request_change(self, new="new@example.com", password=PASSWORD, **extra):
        body = {"new_email": new, "password": password, **extra}
        return self.client_a.post("/api/auth/email/change/", body, format="json")

    def confirm(self, token):
        return self.client.post("/api/auth/email/change/confirm/", {"token": token}, format="json")

    def test_the_link_goes_to_the_new_address_and_nothing_changes_until_it_is_used(self):
        self.assertEqual(self.request_change(password="nope").status_code, 400)
        self.assertEqual(self.request_change("not-an-email").status_code, 400)
        self.assertEqual(self.request_change("sam@example.com").status_code, 400)  # already the address
        self.assertEqual(self.request_change("SAM@example.com").status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

        self.assertEqual(self.request_change("New@Example.com").status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        to_new, notice = sent_to("new@example.com"), sent_to("sam@example.com")
        self.assertIn("https://app.example.com/confirm-email-change?token=", to_new.body)
        self.assertNotIn("token=", notice.body)  # the old address is only warned
        self.assertIn("new@example.com", notice.body)
        self.assertEqual(self.login().status_code, 200)  # the old address still signs in
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "sam@example.com")

    def test_confirming_switches_the_login(self):
        self.request_change("new@example.com")
        token = email_params(sent_to("new@example.com"))["token"]
        self.assertEqual(self.confirm(token).status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(
            (self.user.email, self.user.username, self.user.email_verified),
            ("new@example.com", "new@example.com", True),
        )
        self.assertEqual(self.login("sam@example.com").status_code, 401)  # the old address no longer signs in
        self.assertEqual(self.login("new@example.com").status_code, 200)
        self.assertEqual(self.confirm(token).status_code, 400)  # the link works once

    def test_usernames_that_were_not_the_email_stay_put(self):
        other = User.objects.create_user("samuel", "samuel@example.com", PASSWORD, email_verified=True)
        client = self.authed(self.signed_in("samuel@example.com")["access"])
        body = {"new_email": "sam2@example.com", "password": PASSWORD}
        self.assertEqual(client.post("/api/auth/email/change/", body, format="json").status_code, 200)
        token = email_params(sent_to("sam2@example.com"))["token"]
        self.assertEqual(self.confirm(token).status_code, 200)
        other.refresh_from_db()
        self.assertEqual((other.username, other.email), ("samuel", "sam2@example.com"))
        self.assertEqual(self.login("samuel").status_code, 200)
        self.assertEqual(self.login("sam2@example.com").status_code, 200)

    def test_addresses_that_belong_to_someone_else_get_the_same_answer_and_no_email(self):
        self.make_user("other@example.com")
        taken = self.request_change("OTHER@example.com")
        free = self.request_change("free@example.com")
        self.assertEqual((taken.status_code, free.status_code), (200, 200))
        self.assertEqual(taken.json(), free.json())
        self.assertEqual([m.to for m in mail.outbox], [["free@example.com"], ["sam@example.com"]])

    def test_bad_and_stale_links(self):
        self.request_change("new@example.com")
        token = email_params(sent_to("new@example.com"))["token"]
        for bad_token in (token[:-2] + "xx", "garbage", ""):
            self.assertEqual(self.confirm(bad_token).status_code, 400, bad_token)
        with override_settings(EMAIL_CHANGE_MAX_AGE=-1):  # expired
            self.assertEqual(self.confirm(token).status_code, 400)
        self.make_user("new@example.com")  # someone else took the address in the meantime
        self.assertEqual(self.confirm(token).status_code, 400)

        self.request_change("second@example.com")
        later = email_params(sent_to("second@example.com"))["token"]
        User.objects.filter(pk=self.user.pk).update(email="elsewhere@example.com")  # changed some other way
        self.assertEqual(self.confirm(later).status_code, 400)

    def test_two_factor_users_must_also_give_a_code(self):
        secret = pyotp.random_base32()
        TwoFactor.objects.create(user=self.user, secret=two_factor.encrypt(secret), confirmed=True)
        moment = 1_800_000_000
        with mock.patch("accounts.two_factor._now", return_value=moment):
            code = pyotp.TOTP(secret).at(moment)
            wrong = f"{(int(code) + 1) % 1_000_000:06d}"
            self.assertEqual(self.request_change().status_code, 400)
            self.assertEqual(self.request_change(code=wrong).status_code, 400)
            self.assertEqual(len(mail.outbox), 0)
            self.assertEqual(self.request_change(code=code).status_code, 200)
        self.assertEqual(len(mail.outbox), 2)
        anonymous = APIClient().post(
            "/api/auth/email/change/", {"new_email": "a@example.com", "password": PASSWORD}, format="json"
        )
        self.assertEqual(anonymous.status_code, 401)


class LockoutTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        User.objects.create_user("sam", "sam@example.com", PASSWORD, email_verified=True)

    def fail(self, times, email="sam@example.com"):
        for _ in range(times):
            self.login(email, "wrong")

    def test_five_failures_lock_the_account_even_for_the_right_password(self):
        self.fail(5)
        locked = self.login()
        self.assertEqual(locked.status_code, 429)
        self.assertIn("Retry-After", locked)
        self.assertGreater(locked.json()["retry_after"], 0)
        self.make_user("kim@example.com")
        self.assertEqual(self.login("kim@example.com").status_code, 200)  # other accounts are unaffected

    def test_the_lock_runs_out(self):
        self.fail(5)
        self.assertEqual(self.login().status_code, 429)
        with mock.patch("time.time", return_value=time.time() + 16 * 60):
            self.assertEqual(self.login().status_code, 200)

    def test_a_good_login_resets_the_count(self):
        self.fail(3)
        self.assertEqual(self.login().status_code, 200)
        self.fail(3)
        self.assertEqual(self.login().status_code, 200)

    def test_spelling_variants_count_together(self):
        for _ in range(3):
            self.login(" Sam@Example.com ", "wrong")
        self.fail(2)
        self.assertEqual(self.login().status_code, 429)

    def test_unknown_emails_lock_the_same_way(self):
        self.fail(5, "ghost@example.com")
        self.assertEqual(self.login("ghost@example.com", "anything").status_code, 429)


class TwoFactorTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.user = self.make_user()
        self.tokens = self.signed_in()
        self.client_a = self.authed(self.tokens["access"])
        self.clock = 1_800_000_000  # a fixed moment, so codes are predictable
        patcher = mock.patch("accounts.two_factor._now", side_effect=lambda: self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def code(self, secret):
        return pyotp.TOTP(secret).at(self.clock)

    def wrong_code(self, secret):
        return f"{(int(self.code(secret)) + 1) % 1_000_000:06d}"

    def setup2fa(self, password=PASSWORD):
        return self.client_a.post("/api/auth/2fa/setup/", {"password": password}, format="json")

    def turn_on(self):
        secret = self.setup2fa().json()["secret"]
        res = self.client_a.post("/api/auth/2fa/enable/", {"code": self.code(secret)}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.clock += 30  # the next code is a new one
        return secret, res.json()["backup_codes"]

    def answer(self, challenge, code):
        return self.client.post("/api/auth/login/2fa/", {"challenge": challenge, "code": code}, format="json")

    def test_setup_and_enable(self):
        self.assertEqual(
            self.client_a.get("/api/auth/2fa/").json(),
            {"enabled": False, "setup_pending": False, "backup_codes_remaining": 0},
        )
        self.assertEqual(self.setup2fa("nope").status_code, 400)
        res = self.setup2fa()
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertRegex(body["secret"], r"^[A-Z2-7]{32}$")
        self.assertTrue(body["otpauth_uri"].startswith("otpauth://totp/"))
        self.assertIn("Trading Journal", unquote(body["otpauth_uri"]))
        self.assertIn("sam@example.com", unquote(body["otpauth_uri"]))

        record = TwoFactor.objects.get(user=self.user)
        self.assertNotEqual(record.secret, body["secret"])  # stored encrypted
        self.assertEqual(two_factor.decrypt(record.secret), body["secret"])
        self.assertTrue(self.client_a.get("/api/auth/2fa/").json()["setup_pending"])
        self.assertIn("access", self.login().json())  # not on yet, so signing in is unchanged

        enable = "/api/auth/2fa/enable/"
        self.assertEqual(self.client_a.post(enable, {"code": self.wrong_code(body["secret"])}, format="json").status_code, 400)
        ok = self.client_a.post(enable, {"code": self.code(body["secret"])}, format="json")
        self.assertEqual(ok.status_code, 200)
        codes = ok.json()["backup_codes"]
        self.assertEqual(len(set(codes)), 10)
        for code in codes:
            self.assertRegex(code, r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertEqual(
            self.client_a.get("/api/auth/2fa/").json(),
            {"enabled": True, "setup_pending": False, "backup_codes_remaining": 10},
        )
        self.assertTrue(self.client_a.get("/api/auth/me/").json()["two_factor_enabled"])
        self.assertEqual(self.setup2fa().status_code, 400)  # already on

    def test_signing_in_takes_a_code_that_works_once(self):
        secret, _ = self.turn_on()
        first = self.login().json()
        self.assertEqual(set(first), {"two_factor_required", "challenge"})
        self.assertTrue(first["two_factor_required"])
        challenge = first["challenge"]

        self.assertEqual(self.answer(challenge, self.wrong_code(secret)).status_code, 401)
        ok = self.answer(challenge, self.code(secret))
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.authed(ok.json()["access"]).get("/api/auth/me/").status_code, 200)
        self.assertEqual(self.answer(challenge, self.code(secret)).status_code, 401)  # the same code, again

        self.clock += 30
        self.assertEqual(self.answer(challenge, self.code(secret)).status_code, 200)  # the next code is fine
        self.clock += 30
        spaced = self.code(secret)
        self.assertEqual(self.answer(challenge, f"{spaced[:3]} {spaced[3:]}").status_code, 200)

    def test_challenge_problems(self):
        secret, _ = self.turn_on()
        challenge = self.login().json()["challenge"]
        right = self.code(secret)
        for bad_challenge in ("garbage", challenge + "x", challenge[:-2] + "xx"):
            self.assertEqual(self.answer(bad_challenge, right).status_code, 401, bad_challenge)
        self.assertEqual(self.answer("", right).status_code, 400)
        with override_settings(TWO_FACTOR_CHALLENGE_SECONDS=-1):  # expired
            self.assertEqual(self.answer(challenge, right).status_code, 401)
        self.user.refresh_from_db()
        self.user.set_password("a-different-Str0ng-one!")  # a password change voids older challenges
        self.user.save()
        self.assertEqual(self.answer(challenge, right).status_code, 401)

    def test_backup_codes_work_once_each(self):
        _, backups = self.turn_on()
        challenge = self.login().json()["challenge"]
        self.assertEqual(self.answer(challenge, backups[0].upper()).status_code, 200)  # capitals don't matter
        self.assertEqual(self.answer(challenge, backups[0]).status_code, 401)
        self.assertEqual(self.answer(challenge, backups[1]).status_code, 200)
        self.assertEqual(self.client_a.get("/api/auth/2fa/").json()["backup_codes_remaining"], 8)

    def test_new_backup_codes_replace_the_old_ones(self):
        secret, backups = self.turn_on()
        path = "/api/auth/2fa/backup-codes/"

        def regenerate(**body):
            return self.client_a.post(path, body, format="json")

        self.assertEqual(regenerate(password="nope", code=self.code(secret)).status_code, 400)
        self.assertEqual(regenerate(password=PASSWORD, code=self.wrong_code(secret)).status_code, 400)
        fresh = regenerate(password=PASSWORD, code=self.code(secret))
        self.assertEqual(fresh.status_code, 200)
        new = fresh.json()["backup_codes"]
        self.assertEqual(len(new), 10)
        self.assertTrue(set(new).isdisjoint(backups))

        challenge = self.login().json()["challenge"]
        self.assertEqual(self.answer(challenge, backups[0]).status_code, 401)  # the old ones are dead
        self.assertEqual(self.answer(challenge, new[0]).status_code, 200)

    def test_turning_it_off(self):
        secret, _ = self.turn_on()
        path = "/api/auth/2fa/disable/"

        def disable(**body):
            return self.client_a.post(path, body, format="json")

        self.assertEqual(disable(password="nope", code=self.code(secret)).status_code, 400)
        self.assertEqual(disable(password=PASSWORD, code=self.wrong_code(secret)).status_code, 400)
        self.assertTrue(TwoFactor.objects.filter(user=self.user).exists())
        self.assertEqual(disable(password=PASSWORD, code=self.code(secret)).status_code, 200)
        self.assertFalse(TwoFactor.objects.filter(user=self.user).exists())
        self.assertEqual(BackupCode.objects.filter(user=self.user).count(), 0)
        self.assertIn("access", self.login().json())  # back to a plain sign-in
        self.assertEqual(disable(password=PASSWORD, code="123456").status_code, 400)  # nothing to turn off

    def test_five_wrong_codes_lock_the_second_step(self):
        secret, _ = self.turn_on()
        challenge = self.login().json()["challenge"]
        for _ in range(5):
            self.assertEqual(self.answer(challenge, self.wrong_code(secret)).status_code, 401)
        self.assertEqual(self.answer(challenge, self.code(secret)).status_code, 429)

    def test_losing_the_encryption_key_fails_cleanly(self):
        secret, _ = self.turn_on()
        with override_settings(TWO_FACTOR_KEY="a-different-key"):
            challenge = self.login().json()["challenge"]
            self.assertEqual(self.answer(challenge, self.code(secret)).status_code, 401)  # refused, not a crash

    def test_everything_needs_a_login(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get("/api/auth/2fa/").status_code, 401)
        for path in ("2fa/setup/", "2fa/enable/", "2fa/disable/", "2fa/backup-codes/", "password/change/"):
            self.assertEqual(anonymous.post(f"/api/auth/{path}", {}, format="json").status_code, 401, path)