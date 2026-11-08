from . import rollups
import time
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import InstrumentSpec, Trade, TradingAccount


def utc(year, month, day, hour=12, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=dt_tz.utc)


def make_trade(account, closed_at, exit_price):
    """EURUSD long, 1 lot from 1.10000: exit 1.10500 = +500, exit 1.09750 = -250."""
    return Trade.objects.create(
        account=account, symbol="EURUSD", side="long",
        opened_at=closed_at - timedelta(hours=1), closed_at=closed_at,
        entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
    )


class ReportAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.account = TradingAccount.objects.create(
            user=self.alice, name="A", currency="USD", starting_balance=D("10000")
        )
        self.bob_account = TradingAccount.objects.create(
            user=self.bob, name="B", currency="USD", starting_balance=D("5000")
        )
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        make_trade(self.account, utc(2026, 10, 15), "1.10500")  # +500
        make_trade(self.account, utc(2026, 10, 16), "1.09750")  # -250
        make_trade(self.account, utc(2026, 10, 20), "1.10500")  # +500
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def test_summary_numbers(self):
        body = self.client.get("/api/reports/summary/").json()
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["total_trades"], 3)
        self.assertEqual(body["net_pnl"], "750.00")
        self.assertEqual(body["win_rate"], "66.67")
        self.assertEqual(body["profit_factor"], "4.00")
        self.assertEqual(body["expectancy"], "250.00")
        self.assertEqual(body["max_drawdown"], "250.00")
        self.assertEqual(body["max_drawdown_pct"], "2.38")
        self.assertEqual(body["opening_balance"], "10000.00")
        self.assertEqual(body["closing_balance"], "10750.00")

    def test_date_filter_carries_opening_balance(self):
        body = self.client.get("/api/reports/summary/", {"from": "2026-10-16"}).json()
        self.assertEqual(body["total_trades"], 2)
        self.assertEqual(body["net_pnl"], "250.00")
        self.assertEqual(body["win_rate"], "50.00")
        self.assertEqual(body["opening_balance"], "10500.00")  # 10000 plus the +500 before
        self.assertEqual(body["closing_balance"], "10750.00")

        early = self.client.get("/api/reports/summary/", {"to": "2026-10-15"}).json()
        self.assertEqual(early["total_trades"], 1)
        self.assertEqual(early["net_pnl"], "500.00")

    def test_users_only_see_their_own_data(self):
        bob = APIClient()
        bob.force_authenticate(self.bob)
        body = bob.get("/api/reports/summary/").json()
        self.assertEqual(body["total_trades"], 0)
        self.assertEqual(body["opening_balance"], "5000.00")
        res = bob.get("/api/reports/summary/", {"account": self.account.id})
        self.assertEqual(res.status_code, 400)

    def test_mixed_currencies_need_an_account(self):
        TradingAccount.objects.create(user=self.alice, name="EUR", currency="EUR")
        self.assertEqual(self.client.get("/api/reports/summary/").status_code, 400)
        ok = self.client.get("/api/reports/summary/", {"account": self.account.id})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["currency"], "USD")

    def test_equity_points(self):
        body = self.client.get("/api/reports/equity/").json()
        points = body["points"]
        self.assertEqual([p["date"] for p in points], ["2026-10-15", "2026-10-16", "2026-10-20"])
        self.assertEqual([p["equity"] for p in points], ["10500.00", "10250.00", "10750.00"])
        self.assertEqual([p["drawdown"] for p in points], ["0.00", "250.00", "0.00"])

    def test_calendar_days_weeks_and_totals(self):
        body = self.client.get("/api/reports/calendar/", {"month": "2026-10"}).json()
        self.assertEqual(body["month"], "2026-10")
        self.assertEqual(body["opening_balance"], "10000.00")
        self.assertEqual(
            [d["date"] for d in body["days"]], ["2026-10-15", "2026-10-16", "2026-10-20"]
        )
        self.assertEqual(body["days"][0]["net_pnl"], "500.00")
        self.assertEqual(len(body["weeks"]), 2)
        self.assertEqual(body["weeks"][0]["week_start"], "2026-10-12")
        self.assertEqual(body["weeks"][0]["net_pnl"], "250.00")
        self.assertEqual(body["totals"]["net_pnl"], "750.00")
        self.assertEqual(body["totals"]["trade_count"], 3)

    def test_calendar_uses_user_timezone(self):
        # 23:30 UTC on Oct 31 is already Nov 1 in Lagos (UTC+1)
        make_trade(self.account, utc(2026, 10, 31, 23, 30), "1.10500")
        self.alice.timezone = "Africa/Lagos"
        self.alice.save()

        november = self.client.get("/api/reports/calendar/", {"month": "2026-11"}).json()
        self.assertEqual([d["date"] for d in november["days"]], ["2026-11-01"])
        self.assertEqual(november["opening_balance"], "10750.00")

        october = self.client.get("/api/reports/calendar/", {"month": "2026-10"}).json()
        self.assertEqual(
            [d["date"] for d in october["days"]], ["2026-10-15", "2026-10-16", "2026-10-20"]
        )

    def test_trade_log_uses_the_same_date_window(self):
        make_trade(self.account, utc(2026, 10, 31, 23, 30), "1.10500")
        self.alice.timezone = "Africa/Lagos"
        self.alice.save()
        self.assertEqual(self.client.get("/api/trades/", {"from": "2026-11-01"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/trades/", {"to": "2026-10-31"}).json()["count"], 3)

    def test_bad_inputs_rejected(self):
        for url, params in (
            ("/api/reports/summary/", {"from": "2026-13-45"}),
            ("/api/reports/summary/", {"from": "2026-10-20", "to": "2026-10-01"}),
            ("/api/reports/summary/", {"account": "abc"}),
            ("/api/reports/calendar/", {"month": "October"}),
        ):
            self.assertEqual(self.client.get(url, params).status_code, 400, (url, params))

    def test_ten_thousand_trades_load_under_1_5_seconds(self):
        base = utc(2026, 1, 1, 0, 0)
        Trade.objects.bulk_create(
            [
                Trade(
                    account=self.account, symbol="EURUSD", side="long",
                    opened_at=base + timedelta(minutes=i),
                    closed_at=base + timedelta(minutes=i, seconds=30),
                    entry_price=D("1.1"), exit_price=D("1.1"), lots=D("1"),
                    gross_pnl=D("0"), net_pnl=D("25.00") if i % 3 else D("-40.00"),
                )
                for i in range(10_000)
            ],
            batch_size=1000,
        )
        rollups.rebuild_account(self.account)  # bulk_create skips signals
        for url in ("/api/reports/summary/", "/api/reports/equity/"):
            started = time.perf_counter()
            res = self.client.get(url)
            elapsed = time.perf_counter() - started
            self.assertEqual(res.status_code, 200)
            self.assertLess(elapsed, 1.5, url)
        self.assertGreater(len(self.client.get("/api/reports/equity/").json()["points"]), 0)