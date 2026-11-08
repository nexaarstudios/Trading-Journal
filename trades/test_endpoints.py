from django.test import SimpleTestCase

from .endpoints import collect


class EndpointListTests(SimpleTestCase):
    def test_reads_the_real_routes(self):
        rows = collect()
        index = {(r["method"], r["path"]): r for r in rows}
        self.assertEqual(len(index), len(rows))  # no route listed twice
        for key in (
            ("POST", "/api/auth/login/"), ("GET", "/api/trades/"), ("POST", "/api/trades/"),
            ("PATCH", "/api/trades/{pk}/"), ("POST", "/api/trades/bulk/"),
            ("GET", "/api/trades/{pk}/screenshots/"), ("POST", "/api/trades/{pk}/screenshots/"),
            ("PATCH", "/api/screenshots/{pk}/"), ("DELETE", "/api/screenshots/{pk}/"),
            ("GET", "/api/files/screenshot/"), ("GET", "/api/reports/{slug}/csv/"),
            ("POST", "/api/imports/preview/"), ("POST", "/api/imports/{pk}/undo/"),
            ("GET", "/api/adherence/streak/"), ("GET", "/api/goals/progress/"),
            ("POST", "/api/data/delete/"),
        ):
            self.assertIn(key, index)

        self.assertNotIn(("PUT", "/api/screenshots/{pk}/"), index)   # the viewset doesn't allow it
        self.assertFalse([r for r in rows if "{format}" in r["path"]])
        self.assertTrue(all(r["path"].startswith("/api/") for r in rows))
        public = {r["path"] for r in rows if r["auth"] == "public"}
        self.assertEqual(public, {
            "/api/auth/register/", "/api/auth/login/", "/api/auth/login/2fa/", "/api/auth/refresh/",
            "/api/auth/social/{provider}/",
            "/api/auth/password-reset/", "/api/auth/password-reset/confirm/",
            "/api/auth/verify-email/resend/", "/api/auth/verify-email/confirm/",
            "/api/auth/email/change/confirm/", "/api/files/screenshot/", "/api/health/",
        })
        self.assertEqual(index[("POST", "/api/auth/login/")]["throttle"], "auth")
        self.assertGreaterEqual(len(rows), 80)