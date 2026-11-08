from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from . import goals
from .models import Goal, InstrumentSpec, JournalEntry, Playbook, PlaybookRule, Trade, TradingAccount


def utc(month, day, hour=12, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=dt_tz.utc)


class GoalMathTests(SimpleTestCase):
    def test_overlaps_and_active_months(self):
        def m(n):
            return date(2026, n, 1)

        self.assertTrue(goals.overlaps(m(10), None, m(1), None))
        self.assertTrue(goals.overlaps(m(10), m(10), m(10), m(12)))
        self.assertFalse(goals.overlaps(m(9), m(9), m(10), None))
        self.assertFalse(goals.overlaps(m(10), m(10), m(11), m(12)))
        self.assertTrue(goals.active_in(m(9), None, m(12)))
        self.assertFalse(goals.active_in(m(9), None, m(8)))
        self.assertTrue(goals.active_in(m(9), m(10), m(10)))
        self.assertFalse(goals.active_in(m(9), m(10), m(11)))

    def test_status_rules(self):
        month = date(2026, 10, 1)

        def pnl(current, today):
            return goals.evaluate("net_pnl", D("1000"), current, month, today)

        def rate(current, today):
            return goals.evaluate("win_rate", D("60"), current, month, today)

        self.assertEqual(pnl(D("0"), date(2026, 9, 30))["status"], "upcoming")
        self.assertFalse(pnl(D("5000"), date(2026, 9, 30))["achieved"])
        self.assertEqual(pnl(D("1000"), date(2026, 10, 3))["status"], "achieved")
        self.assertEqual(pnl(D("200"), date(2026, 10, 10))["status"], "behind")   # pace line is 322.58
        self.assertEqual(pnl(D("400"), date(2026, 10, 10))["status"], "on_pace")
        self.assertEqual(pnl(D("999.99"), date(2026, 11, 1))["status"], "missed")
        self.assertEqual(pnl(D("1000"), date(2026, 11, 1))["status"], "achieved")
        negative = pnl(D("-50"), date(2026, 10, 10))
        self.assertEqual((negative["status"], negative["percent"]), ("behind", D("-5")))

        self.assertEqual(rate(None, date(2026, 10, 10))["status"], "no_data")
        self.assertEqual(rate(D("59"), date(2026, 10, 10))["status"], "behind")   # rates have no pace line
        self.assertIsNone(rate(D("59"), date(2026, 10, 10))["expected_by_today"])
        self.assertEqual(rate(None, date(2026, 11, 2))["status"], "missed")
        self.assertIsNone(rate(None, date(2026, 11, 2))["percent"])


class GoalTests(TestCase):
    PASSWORD = "pass-12345"

    def setUp(self):
        cache.clear()  # the delete endpoint is rate limited through the cache
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", self.PASSWORD)
        self.bob = User.objects.create_user("bob", "bob@example.com", self.PASSWORD)
        self.account = TradingAccount.objects.create(
            user=self.alice, name="A", currency="USD", starting_balance=D("10000")
        )
        self.second = TradingAccount.objects.create(user=self.alice, name="A2", currency="USD")
        self.bob_account = TradingAccount.objects.create(user=self.bob, name="B", currency="USD")
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )

        playbook = Playbook.objects.create(user=self.alice, name="Breakout")
        r1 = PlaybookRule.objects.create(playbook=playbook, text="Retest", position=1)
        r2 = PlaybookRule.objects.create(playbook=playbook, text="Stop placed", position=2)
        # day, exit price, rules ticked -> +500 (2 of 2), -250 (1 of 2), +500 and 0 (no playbook)
        for day, exit_price, ticked in (
            (5, "1.10500", [r1, r2]), (8, "1.09750", [r1]), (12, "1.10500", None), (14, "1.10000", None),
        ):
            self.trade(utc(10, day), exit_price, playbook if ticked else None, ticked)

        # Three days with real content: the 3rd, 5th (twice) and 6th. A blank entry and a weekly review don't count.
        for kind, day, answer in (
            ("post_market", 3, "Stuck to the plan"), ("pre_market", 5, "Watching EURUSD"),
            ("post_market", 5, "Fine"), ("post_market", 6, "Patient"),
            ("post_market", 7, ""), ("weekly", 12, "Good week"),
        ):
            JournalEntry.objects.create(
                user=self.alice, kind=kind, date=date(2026, 10, day),
                sections=[{"prompt": "p", "answer": answer}],
            )

        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def trade(self, closed, exit_price, playbook=None, ticked=None):
        trade = Trade.objects.create(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"), playbook=playbook,
        )
        if ticked:
            trade.sync_rules([r.id for r in ticked])
        return trade

    def make(self, kind, target, **extra):
        body = {"kind": kind, "target": str(target), "start_month": "2026-10"}
        body.update(extra)
        res = self.client.post("/api/goals/", body, format="json")
        self.assertEqual(res.status_code, 201, res.content)
        return res.json()

    def raw_progress(self, month="2026-10", today="2026-10-20", **params):
        res = self.client.get("/api/goals/progress/", {"month": month, "date": today, **params})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def progress(self, **kwargs):
        return {g["kind"]: g for g in self.raw_progress(**kwargs)["goals"]}

    def check(self, row, **expected):
        self.assertEqual({key: row[key] for key in expected}, expected)

    # ---- progress ----

    def test_progress_numbers_mid_month(self):
        self.make("net_pnl", 1000, account=self.account.id)
        self.make("win_rate", 60)
        self.make("journal_days", 10)
        self.make("rule_adherence", 80)

        body = self.raw_progress()
        self.assertEqual((body["month"], body["today"]), ("2026-10", "2026-10-20"))
        got = {g["kind"]: g for g in body["goals"]}
        self.check(got["net_pnl"], account=self.account.id, currency="USD", target="1000.00",
                   current="750.00", percent="75.00", achieved=False, status="on_pace",
                   expected_by_today="645.16")  # 1000 x 20 / 31
        self.check(got["win_rate"], account=None, currency=None, target="60.00", current="50.00",
                   percent="83.33", achieved=False, status="behind", expected_by_today=None)
        self.check(got["journal_days"], current="3.00", percent="30.00",
                   status="behind", expected_by_today="6.45")
        self.check(got["rule_adherence"], current="75.00", percent="93.75", status="behind")

    def test_achieved_and_on_pace(self):
        self.make("net_pnl", 500, account=self.account.id)
        self.make("journal_days", 4)
        got = self.progress()
        self.check(got["net_pnl"], percent="150.00", achieved=True, status="achieved")
        self.check(got["journal_days"], current="3.00", percent="75.00", achieved=False,
                   status="on_pace", expected_by_today="2.58")

    def test_recurring_one_off_past_and_future_months(self):
        self.make("net_pnl", 1000, account=self.account.id, start_month="2026-09")
        self.make("win_rate", 40, start_month="2026-09")
        self.make("rule_adherence", 70, end_month="2026-10")  # October only

        past = self.progress(today="2026-11-05")
        self.check(past["net_pnl"], status="missed", achieved=False)
        self.check(past["win_rate"], status="achieved", achieved=True)
        self.check(past["rule_adherence"], status="achieved", achieved=True)

        before = self.progress(today="2026-09-15")
        self.assertEqual(len(before), 3)
        for goal in before.values():
            self.check(goal, status="upcoming", achieved=False)

        november = self.progress(month="2026-11", today="2026-11-05")
        self.assertEqual(sorted(november), ["net_pnl", "win_rate"])  # the one-off is gone
        self.check(november["net_pnl"], current="0.00", percent="0.00", status="behind",
                   expected_by_today="166.67")
        self.check(november["win_rate"], current=None, percent=None, status="no_data")

        self.assertEqual(self.progress(month="2026-08"), {})  # before any goal began

    def test_months_follow_the_user_timezone(self):
        self.trade(utc(10, 31, 23, 30), "1.10500")  # +500: already November in Lagos
        self.alice.timezone = "Africa/Lagos"
        self.alice.save()
        self.make("net_pnl", 1000, account=self.account.id)
        self.check(self.progress(today="2026-10-31")["net_pnl"], current="750.00")
        self.check(self.progress(month="2026-11", today="2026-11-02")["net_pnl"], current="500.00")

    def test_account_filter_and_scoping(self):
        self.make("net_pnl", 1000, account=self.account.id)
        self.make("net_pnl", 100, account=self.second.id)  # one per account is fine
        self.make("win_rate", 60)                           # covers every account

        def view(**params):
            goals_list = self.raw_progress(**params)["goals"]
            return sorted((g["kind"], g["account"]) for g in goals_list)

        self.assertEqual(len(view()), 3)
        self.assertEqual(view(account=self.account.id), [("net_pnl", self.account.id), ("win_rate", None)])
        self.assertEqual(view(account=self.second.id), [("net_pnl", self.second.id), ("win_rate", None)])
        second_goal = next(
            g for g in self.raw_progress(account=self.second.id)["goals"] if g["kind"] == "net_pnl"
        )
        self.check(second_goal, current="0.00", status="behind")
        theirs = self.client.get(
            "/api/goals/progress/", {"month": "2026-10", "account": self.bob_account.id}
        )
        self.assertEqual(theirs.status_code, 400)

    # ---- creating and changing goals ----

    def test_validation(self):
        month = {"start_month": "2026-10"}
        bad = [
            {"kind": "net_pnl", "target": "100", **month},                                      # no account
            {"kind": "net_pnl", "target": "100", "account": self.bob_account.id, **month},    # not his
            {"kind": "journal_days", "target": "10", "account": self.account.id, **month},
            {"kind": "win_rate", "target": "150", **month},
            {"kind": "rule_adherence", "target": "100.5", **month},
            {"kind": "journal_days", "target": "2.5", **month},
            {"kind": "journal_days", "target": "32", **month},
            {"kind": "win_rate", "target": "0", **month},
            {"kind": "win_rate", "target": "-5", **month},
            {"kind": "win_rate", "target": "50", "start_month": "October"},
            {"kind": "win_rate", "target": "50", "start_month": "2026-13"},
            {"kind": "win_rate", "target": "50", "start_month": "2026-10", "end_month": "2026-09"},
            {"kind": "streak", "target": "5", **month},
            {"target": "5", **month},
            {"kind": "win_rate", **month},
        ]
        for body in bad:
            self.assertEqual(self.client.post("/api/goals/", body, format="json").status_code, 400, body)
        self.assertEqual(Goal.objects.count(), 0)

        for kind, target in (("win_rate", "100"), ("rule_adherence", "12.5"), ("journal_days", "31")):
            self.make(kind, target)

    def test_goals_of_the_same_kind_cannot_overlap(self):
        first = self.make("net_pnl", 1000, account=self.account.id)  # from October, repeating
        for start, end in (("2027-01", None), ("2026-10", "2026-10"), ("2026-08", None)):
            body = {"kind": "net_pnl", "target": "500", "account": self.account.id,
                    "start_month": start, "end_month": end}
            self.assertEqual(self.client.post("/api/goals/", body, format="json").status_code, 400, (start, end))

        self.make("net_pnl", 500, account=self.account.id, start_month="2026-08", end_month="2026-09")
        self.make("net_pnl", 500, account=self.second.id)  # another account, same months

        url = f"/api/goals/{first['id']}/"
        self.assertEqual(self.client.patch(url, {"target": "1500"}, format="json").json()["target"], "1500.00")
        self.assertEqual(self.client.patch(url, {"kind": "win_rate"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"end_month": "2026-09"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"end_month": "2026-12"}, format="json").json()["end_month"], "2026-12")
        self.assertIsNone(self.client.patch(url, {"end_month": None}, format="json").json()["end_month"])
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_goals_are_private(self):
        mine = self.make("win_rate", 60)
        bob = APIClient()
        bob.force_authenticate(self.bob)
        url = f"/api/goals/{mine['id']}/"
        self.assertEqual(bob.get("/api/goals/").json(), [])
        self.assertEqual(bob.get(url).status_code, 404)
        self.assertEqual(bob.patch(url, {"target": "10"}, format="json").status_code, 404)
        self.assertEqual(bob.delete(url).status_code, 404)
        self.assertEqual(bob.get("/api/goals/progress/", {"month": "2026-10"}).json()["goals"], [])
        anonymous = APIClient()
        for path in ("/api/goals/", "/api/goals/progress/"):
            self.assertEqual(anonymous.get(path).status_code, 401, path)
        self.assertEqual(Goal.objects.count(), 1)

    def test_bad_progress_requests(self):
        for params in ({"month": "x"}, {"month": "2026-13"}, {"date": "soon"}, {"account": "abc"},
                       {"month": "9999-12"}):
            res = self.client.get("/api/goals/progress/", params)
            self.assertEqual(res.status_code, 400, params)

    def test_deleting_all_data_removes_goals(self):
        self.make("win_rate", 60)
        self.make("net_pnl", 500, account=self.account.id)
        Goal.objects.create(user=self.bob, kind="win_rate", target=D("50"), start_month=date(2026, 10, 1))
        res = self.client.post(
            "/api/data/delete/", {"password": self.PASSWORD, "scope": "data", "confirm": "DELETE"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted"]["goals"], 2)
        self.assertEqual(list(Goal.objects.values_list("user__username", flat=True)), ["bob"])