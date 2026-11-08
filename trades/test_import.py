import io
import time
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from openpyxl import Workbook

from .importer import (
    REQUIRED, ImportFileError, build_plan, commit_plan,
    read_table, suggest_mapping, undo_batch,
)
from .models import ImportBatch, InstrumentSpec, Trade, TradingAccount

HEADER = "Symbol,Type,Open Time,Close Time,Open Price,Close Price,Size,S / L,Commission,Swap,Profit"
EUR_ROW = "EURUSDm,buy,2026.10.15 14:00:00,2026.10.15 16:00:00,1.10000,1.10500,1,1.09500,-7,0,500"


class ImportTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user("imp", "imp@example.com", "x")
        self.account = TradingAccount.objects.create(user=user, name="T", currency="USD")
        for symbol, base, quote, size in (
            ("EURUSD", "EUR", "USD", "100000"),
            ("GBPJPY", "GBP", "JPY", "100000"),
        ):
            InstrumentSpec.objects.create(
                symbol=symbol, asset_class="forex", base_currency=base,
                quote_currency=quote, contract_size=D(size), pip_size=D("0.0001"),
            )

    def plan_for(self, *lines, tz="UTC", date_format=""):
        text = "\n".join((HEADER, *lines))
        headers, rows = read_table(io.BytesIO(text.encode()), "trades.csv")
        return build_plan(
            self.account, headers, rows, suggest_mapping(headers),
            tz_name=tz, date_format=date_format,
        )

    def test_mapping_is_guessed_for_all_required_fields(self):
        headers, _ = read_table(io.BytesIO((HEADER + "\n" + EUR_ROW).encode()), "t.csv")
        mapping = suggest_mapping(headers)
        for name in REQUIRED:
            self.assertIn(name, mapping)

    def test_timezone_symbol_suffix_and_cost_signs(self):
        plan = self.plan_for(EUR_ROW, tz="Africa/Lagos")
        trade = plan.trades[0]
        self.assertEqual(trade.symbol, "EURUSD")
        self.assertEqual(trade.opened_at, datetime(2026, 10, 15, 13, 0, tzinfo=dt_tz.utc))
        self.assertEqual(trade.commission, D("7.00"))   # MetaTrader's -7 becomes a 7 cost
        self.assertEqual(trade.net_pnl, D("493.00"))
        self.assertEqual(trade.r_multiple, D("0.99"))

    def test_duplicates_skipped_in_file_and_against_database(self):
        plan = self.plan_for(EUR_ROW, EUR_ROW)
        self.assertEqual((len(plan.trades), plan.duplicates), (1, 1))
        commit_plan(plan)
        again = self.plan_for(EUR_ROW)
        self.assertEqual((len(again.trades), again.duplicates), (0, 1))

    def test_bad_rows_are_reported_not_fatal(self):
        bad_symbol = "FOOBAR,buy,2026.10.15 14:00:00,2026.10.15 16:00:00,1.1,1.105,1,,0,0,0"
        bad_side = "EURUSD,sideways,2026.10.15 18:00:00,2026.10.15 19:00:00,1.1,1.105,1,,0,0,0"
        plan = self.plan_for(EUR_ROW, bad_symbol, bad_side)
        self.assertEqual(len(plan.trades), 1)
        self.assertEqual([line for line, _ in plan.errors], [3, 4])

    def test_commit_then_undo(self):
        batch = commit_plan(self.plan_for(EUR_ROW), filename="trades.csv")
        self.assertEqual(Trade.objects.count(), 1)
        self.assertEqual(batch.imported_count, 1)
        self.assertEqual(undo_batch(batch), 1)
        self.assertEqual(Trade.objects.count(), 0)
        batch.refresh_from_db()
        self.assertEqual(batch.status, ImportBatch.Status.UNDONE)
        with self.assertRaises(ImportFileError):
            undo_batch(batch)

    def test_ambiguous_dates_need_a_format(self):
        row = "EURUSD,buy,15/10/2026 14:00,15/10/2026 16:00,1.1,1.105,1,,-7,0,500"
        self.assertEqual(len(self.plan_for(row).errors), 1)
        ok = self.plan_for(row, date_format="%d/%m/%Y %H:%M")
        self.assertEqual(len(ok.trades), 1)

    def test_cross_pair_with_broker_profit_but_no_rate(self):
        row = "GBPJPY,sell,2026.10.15 10:00:00,2026.10.15 11:00:00,190.000,189.000,0.5,190.500,-3,0,330"
        plan = self.plan_for(row)
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.trades[0].net_pnl, D("327.00"))
        self.assertIsNone(plan.trades[0].r_multiple)

    def test_xlsx_file(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(HEADER.split(","))
        sheet.append([
            "EURUSD", "buy", datetime(2026, 10, 15, 14, 0), datetime(2026, 10, 15, 16, 0),
            1.1, 1.105, 1, 1.095, -7, 0, 500,
        ])
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        headers, rows = read_table(buffer, "trades.xlsx")
        plan = build_plan(self.account, headers, rows, suggest_mapping(headers))
        self.assertEqual(plan.errors, [])
        self.assertEqual(plan.trades[0].net_pnl, D("493.00"))

    def test_five_thousand_rows_import_under_twenty_seconds(self):
        base = datetime(2026, 1, 1, tzinfo=dt_tz.utc)
        fmt = "%Y.%m.%d %H:%M:%S"
        lines = [
            f"EURUSD,buy,{(base + timedelta(minutes=i)).strftime(fmt)},"
            f"{(base + timedelta(minutes=i, seconds=30)).strftime(fmt)},"
            "1.10000,1.10500,1,1.09500,-7,0,500"
            for i in range(5000)
        ]
        start = time.perf_counter()
        commit_plan(self.plan_for(*lines), filename="big.csv")
        elapsed = time.perf_counter() - start
        self.assertEqual(Trade.objects.count(), 5000)
        self.assertLess(elapsed, 20)