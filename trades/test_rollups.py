import io
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.db.models import Sum
from django.test import TestCase
from rest_framework.test import APIClient

from . import rollups
from .importer import build_plan, commit_plan, read_table, suggest_mapping, undo_batch
from .models import DailyRollup, InstrumentSpec, Trade, TradingAccount
from .test_import import EUR_ROW, HEADER


def utc(month, day, hour=12, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=dt_tz.utc)


class RollupTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("r", "r@example.com", "x")
        self.account = TradingAccount.objects.create(user=self.user, name="R", currency="USD")
        for symbol, base in (("EURUSD", "EUR"), ("GBPUSD", "GBP")):
            InstrumentSpec.objects.create(
                symbol=symbol, asset_class="forex", base_currency=base, quote_currency="USD",
                contract_size=D("100000"), pip_size=D("0.0001"),
            )

    def trade(self, closed_at, exit_price="1.10500", symbol="EURUSD"):
        """1 lot from 1.10000: exit 1.10500 is +500, exit 1.09750 is -250."""
        return Trade.objects.create(
            account=self.account, symbol=symbol, side="long",
            opened_at=closed_at - timedelta(hours=1), closed_at=closed_at,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
        )

    def rows(self):
        return {r.date.isoformat(): r for r in DailyRollup.objects.filter(account=self.account)}

    def test_saving_a_trade_creates_its_day(self):
        self.trade(utc(10, 15))
        row = self.rows()["2026-10-15"]
        self.assertEqual((row.net_pnl, row.trade_count, row.wins, row.losses),
                         (D("500.00"), 1, 1, 0))

    def test_two_trades_on_one_day_merge(self):
        self.trade(utc(10, 15, 9))
        self.trade(utc(10, 15, 15), exit_price="1.09750")
        row = self.rows()["2026-10-15"]
        self.assertEqual((row.net_pnl, row.trade_count, row.wins, row.losses),
                         (D("250.00"), 2, 1, 1))

    def test_editing_the_close_time_moves_the_trade(self):
        trade = self.trade(utc(10, 15))
        trade.opened_at, trade.closed_at = utc(10, 18, 11), utc(10, 18)
        trade.save()
        self.assertEqual(list(self.rows()), ["2026-10-18"])

    def test_deleting_a_trade_removes_an_empty_day(self):
        first = self.trade(utc(10, 15))
        self.trade(utc(10, 16))
        first.delete()
        self.assertEqual(list(self.rows()), ["2026-10-16"])

    def test_import_then_undo(self):
        headers, data = read_table(io.BytesIO((HEADER + "\n" + EUR_ROW).encode()), "t.csv")
        plan = build_plan(self.account, headers, data, suggest_mapping(headers))
        batch = commit_plan(plan)
        row = self.rows()["2026-10-15"]
        self.assertEqual((row.net_pnl, row.trade_count), (D("493.00"), 1))
        undo_batch(batch)
        self.assertEqual(self.rows(), {})

    def test_timezone_change_rebuilds_day_boundaries(self):
        self.trade(utc(10, 31, 23, 30))
        self.assertEqual(list(self.rows()), ["2026-10-31"])
        self.user.timezone = "Africa/Lagos"  # UTC+1, so 23:30 UTC is already Nov 1
        self.user.save()
        self.assertEqual(list(self.rows()), ["2026-11-01"])

    def test_full_rebuild_matches_incremental_updates(self):
        for day, exit_price in ((10, "1.10500"), (11, "1.10500"), (11, "1.09750"), (12, "1.10500")):
            self.trade(utc(10, day), exit_price=exit_price)

        def snapshot():
            return {
                k: (r.net_pnl, r.trade_count, r.wins, r.losses, r.r_total)
                for k, r in self.rows().items()
            }

        before = snapshot()
        rollups.rebuild_account(self.account)
        self.assertEqual(before, snapshot())
        total = DailyRollup.objects.aggregate(t=Sum("net_pnl"))["t"]
        self.assertEqual(total, Trade.objects.aggregate(t=Sum("net_pnl"))["t"])

    def test_symbol_filter_bypasses_the_rollup(self):
        self.trade(utc(10, 15))
        self.trade(utc(10, 16), symbol="GBPUSD")
        client = APIClient()
        client.force_authenticate(self.user)
        everything = client.get("/api/reports/equity/").json()["points"]
        gbp_only = client.get("/api/reports/equity/", {"symbol": "GBPUSD"}).json()["points"]
        self.assertEqual([p["date"] for p in everything], ["2026-10-15", "2026-10-16"])
        self.assertEqual([p["date"] for p in gbp_only], ["2026-10-16"])

    def test_deleting_an_account_removes_its_rollups(self):
        self.trade(utc(10, 15))
        self.account.delete()
        self.assertEqual(DailyRollup.objects.count(), 0)