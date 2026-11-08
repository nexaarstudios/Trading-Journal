import re
from unittest import mock

from django.test import override_settings
from rest_framework.test import APIClient

from .tests import ApiTestCase

HEX_ID = re.compile(r"^[0-9a-f]{32}$")


class HealthTests(ApiTestCase):
    def test_a_healthy_service(self):
        res = self.client.get("/api/health/")
        self.assertEqual((res.status_code, res.json()), (200, {"status": "ok"}))

    def test_a_database_problem(self):
        with mock.patch("config.health.connection") as broken:
            broken.cursor.side_effect = Exception("down")
            res = self.client.get("/api/health/")
        self.assertEqual((res.status_code, res.json()), (503, {"status": "unhealthy", "failing": ["database"]}))

    def test_a_cache_problem(self):
        with mock.patch("config.health.cache") as broken:
            broken.set.side_effect = Exception("down")
            res = self.client.get("/api/health/")
        self.assertEqual((res.status_code, res.json()["failing"]), (503, ["cache"]))

    def test_it_needs_no_login_and_is_never_throttled(self):
        for _ in range(30):
            self.assertEqual(self.client.get("/api/health/").status_code, 200)


class RequestIdTests(ApiTestCase):
    def test_every_response_carries_a_request_id(self):
        first = self.client.get("/api/health/")["X-Request-ID"]
        second = self.client.get("/api/health/")["X-Request-ID"]
        self.assertRegex(first, HEX_ID)
        self.assertNotEqual(first, second)
        self.assertRegex(self.client.get("/api/nothing-here/")["X-Request-ID"], HEX_ID)  # even a 404

    def test_a_sensible_id_from_the_caller_is_kept(self):
        res = self.client.get("/api/health/", HTTP_X_REQUEST_ID="trace-abc-12345")
        self.assertEqual(res["X-Request-ID"], "trace-abc-12345")

    def test_unusable_ids_are_replaced(self):
        for junk in ("short", "has spaces in it!", "x" * 200, "semi;colon-12345"):
            res = self.client.get("/api/health/", HTTP_X_REQUEST_ID=junk)
            self.assertRegex(res["X-Request-ID"], HEX_ID, junk)

    @override_settings(CORS_ALLOWED_ORIGINS=["https://app.example.com"])
    def test_the_browser_is_allowed_to_read_the_request_id(self):
        res = self.client.get("/api/health/", HTTP_ORIGIN="https://app.example.com")
        self.assertEqual(res["Access-Control-Allow-Origin"], "https://app.example.com")
        self.assertIn("X-Request-ID", res["Access-Control-Expose-Headers"])


class RequestLogTests(ApiTestCase):
    def test_log_lines_carry_the_request_id_and_the_user_id(self):
        user = self.make_user()
        token = self.signed_in()["access"]
        with self.assertLogs("config.request", level="INFO") as logs:
            res = self.authed(token).get("/api/auth/me/", HTTP_X_REQUEST_ID="trace-abc-12345")
        self.assertEqual(res.status_code, 200)
        record = logs.records[-1]
        self.assertEqual((record.request_id, record.user_id), ("trace-abc-12345", str(user.pk)))
        self.assertIn("GET /api/auth/me/ 200", record.getMessage())

    def test_anonymous_requests_log_a_dash_and_nothing_carries_over(self):
        self.make_user()
        token = self.signed_in()["access"]
        with self.assertLogs("config.request", level="INFO") as logs:
            self.authed(token).get("/api/auth/me/")
            APIClient().get("/api/auth/me/")  # no token
        signed, anonymous = logs.records[-2:]
        self.assertNotEqual(signed.user_id, "-")
        self.assertEqual(anonymous.user_id, "-")
        self.assertIn("401", anonymous.getMessage())

    def test_query_strings_never_reach_the_log(self):
        with self.assertLogs("config.request", level="INFO") as logs:
            self.client.get("/api/files/screenshot/", {"t": "secret-token-value"})
        self.assertIn("/api/files/screenshot/", logs.output[-1])
        self.assertNotIn("secret-token-value", logs.output[-1])

    def test_health_checks_stay_out_of_the_log(self):
        with self.assertNoLogs("config.request", level="INFO"):
            self.client.get("/api/health/")