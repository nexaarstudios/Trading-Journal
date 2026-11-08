from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from . import adherence
from .models import InstrumentSpec, Playbook, PlaybookRule, Trade, TradingAccount

UTC = ZoneInfo("UTC")


def utc(month, day, hour=12, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=dt_tz.utc)


class AdherenceMathTests(SimpleTestCase):
    def test_empty(self):
        self.assertEqual(adherence.streaks([], UTC), {
            "trades": {"current": 0, "longest": 0, "total": 0, "followed": 0},
            "days": {"current": 0, "longest": 0, "total_days": 0, "clean_days": 0, "last_scored_date": None},
            "last_broken": None,
        })

    def test_runs_and_days(self):
        rows = [
            (1, utc(10, 12, 9), True), (2, utc(10, 12, 15), True), (3, utc(10, 13), True),
            (4, utc(10, 14), False), (5, utc(10, 15, 9), True), (6, utc(10, 15, 16), False),
            (7, utc(10, 16), True), (8, utc(10, 19), True),
        ]
        out = adherence.streaks(rows, UTC)
        self.assertEqual(out["trades"], {"current": 2, "longest": 3, "total": 8, "followed": 6})
        self.assertEqual(out["days"], {
            "current": 2, "longest": 2, "total_days": 6, "clean_days": 4, "last_scored_date": date(2026, 10, 19),
        })
        self.assertEqual(out["last_broken"], {"trade_id": 6, "date": date(2026, 10, 15)})

        out = adherence.streaks(rows + [(9, utc(10, 20), False)], UTC)  # a break at the very end
        self.assertEqual((out["trades"]["current"], out["days"]["current"], out["trades"]["longest"]), (0, 0, 3))


class AdherenceAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.account = TradingAccount.objects.create(user=self.alice, name="A", currency="USD")
        self.second = TradingAccount.objects.create(user=self.alice, name="A2", currency="USD")
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        self.pb = Playbook.objects.create(user=self.alice, name="Breakout")
        PlaybookRule.objects.create(playbook=self.pb, text="Retest", position=1)
        PlaybookRule.objects.create(playbook=self.pb, text="Stop placed", position=2)
        self.pb2 = Playbook.objects.create(user=self.alice, name="Reversal")
        PlaybookRule.objects.create(playbook=self.pb2, text="Wait for rejection", position=1)
        self.empty_pb = Playbook.objects.create(user=self.alice, name="No rules yet")
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def trade(self, closed, followed=True, account=None, playbook=None, scored=True):
        playbook = playbook if playbook is not None else self.pb
        trade = Trade.objects.create(
            account=account or self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(minutes=30), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
            playbook=playbook if scored else None,
        )
        if scored:
            rules = list(playbook.rules.all())
            trade.sync_rules([r.id for r in (rules if followed else rules[:1])])
        return trade

    def streak(self, **params):
        res = self.client.get("/api/adherence/streak/", params)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_numbers_ignore_unscored_trades(self):
        for day, hour, followed in ((12, 9, True), (12, 15, True), (13, 12, True), (14, 12, False),
                                    (15, 9, True), (15, 16, False), (16, 12, True), (19, 12, True)):
            broken = self.trade(utc(10, day, hour), followed)
            if (day, hour) == (15, 16):
                last_broken = broken
        self.trade(utc(10, 17), scored=False)                       # no playbook: skipped
        self.trade(utc(10, 18), playbook=self.empty_pb)             # a playbook without rules: skipped

        body = self.streak()
        self.assertEqual(body["trades"], {"current": 2, "longest": 3, "total": 8, "followed": 6})
        self.assertEqual(body["days"], {
            "current": 2, "longest": 2, "total_days": 6, "clean_days": 4, "last_scored_date": "2026-10-19",
        })
        self.assertEqual(body["last_broken"], {"trade_id": last_broken.id, "date": "2026-10-15"})

    def test_filters(self):
        self.trade(utc(10, 12), True)
        self.trade(utc(10, 13), False)
        self.trade(utc(10, 14), True)
        self.trade(utc(10, 15), True, playbook=self.pb2)
        late_break = self.trade(utc(10, 16), False, account=self.second)

        everything = self.streak()
        self.assertEqual(everything["trades"], {"current": 0, "longest": 2, "total": 5, "followed": 3})
        self.assertEqual(everything["days"], {
            "current": 0, "longest": 2, "total_days": 5, "clean_days": 3, "last_scored_date": "2026-10-16",
        })
        self.assertEqual(everything["last_broken"]["trade_id"], late_break.id)

        first_account = self.streak(account=self.account.id)
        self.assertEqual(first_account["trades"], {"current": 2, "longest": 2, "total": 4, "followed": 3})
        self.assertEqual(first_account["last_broken"]["date"], "2026-10-13")

        breakout = self.streak(playbook=self.pb.id)
        self.assertEqual(breakout["trades"], {"current": 0, "longest": 1, "total": 4, "followed": 2})
        reversal = self.streak(playbook=self.pb2.id)
        self.assertEqual(reversal["trades"], {"current": 1, "longest": 1, "total": 1, "followed": 1})
        self.assertIsNone(reversal["last_broken"])
        self.assertEqual(
            self.streak(account=self.second.id)["trades"],
            {"current": 0, "longest": 0, "total": 1, "followed": 0},
        )
        self.assertEqual(self.streak(**{"from": "2026-10-16"})["trades"]["total"], 5)  # date filters are ignored

    def test_days_follow_the_user_timezone(self):
        self.trade(utc(10, 20, 23, 30), True)
        self.trade(utc(10, 21, 0, 30), False)
        in_utc = self.streak()["days"]
        self.assertEqual((in_utc["total_days"], in_utc["clean_days"], in_utc["longest"]), (2, 1, 1))

        self.alice.timezone = "Africa/Lagos"  # UTC+1: both trades happen on the 21st
        self.alice.save()
        in_lagos = self.streak()
        self.assertEqual((in_lagos["days"]["total_days"], in_lagos["days"]["clean_days"]), (1, 0))
        self.assertEqual(in_lagos["last_broken"]["date"], "2026-10-21")

    def test_empty_private_and_login_required(self):
        self.trade(utc(10, 12), True)
        bob = APIClient()
        bob.force_authenticate(self.bob)
        res = bob.get("/api/adherence/streak/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {
            "trades": {"current": 0, "longest": 0, "total": 0, "followed": 0},
            "days": {"current": 0, "longest": 0, "total_days": 0, "clean_days": 0, "last_scored_date": None},
            "last_broken": None,
        })
        self.assertEqual(APIClient().get("/api/adherence/streak/").status_code, 401)