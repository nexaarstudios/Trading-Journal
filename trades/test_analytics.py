import csv
import io
import time
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from . import analytics
from .models import InstrumentSpec, Playbook, PlaybookRule, Tag, Trade, TradingAccount

NATIVE = [
    "by-weekday", "by-hour", "by-month", "by-symbol", "long-short",
    "holding-time", "r-distribution", "streaks", "by-rating",
]
ALL_FIFTEEN = [
    "summary", "equity", "calendar", "mistakes", "setups", "emotions", "playbooks",
    *NATIVE,
]
UTC = ZoneInfo("UTC")


def at(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_tz.utc)


class AnalyticsMathTests(SimpleTestCase):
    def row(self, net, r=None, seconds=0, day=1):
        closed = datetime(2026, 10, day, 12, tzinfo=dt_tz.utc)
        return analytics.Row(
            day, closed - timedelta(seconds=seconds), closed, D(str(net)),
            None if r is None else D(str(r)), "long", "EURUSD", None, seconds,
        )

    def test_stats_with_no_trades_only_wins_and_only_losses(self):
        empty = analytics.stats([])
        self.assertEqual((empty["trades"], empty["net_pnl"]), (0, 0))
        for key in ("win_rate", "avg_pnl", "profit_factor", "avg_r"):
            self.assertIsNone(empty[key])
        wins_only = analytics.stats([(D("100"), None), (D("50"), D("1.0"))])
        self.assertEqual((wins_only["win_rate"], wins_only["profit_factor"]), (100, None))
        losses_only = analytics.stats([(D("-100"), D("-1.0"))])
        self.assertEqual((losses_only["win_rate"], losses_only["profit_factor"]), (0, 0))

    def test_bucket_edges_belong_to_the_upper_bucket(self):
        seconds = [0, 299, 300, 899, 900, 3599, 3600, 14399, 14400, 86399, 86400, 604799, 604800]
        table, _ = analytics.holding_time([self.row(1, seconds=s) for s in seconds])
        self.assertEqual([b["trades"] for b in table], [2, 2, 2, 2, 2, 2, 1])

        values = [-4, -3, -2.5, -1, 0, 0.99, 1, 3, 10]
        table, _ = analytics.r_distribution([self.row(1, r=v) for v in values])
        self.assertEqual([b["trades"] for b in table], [1, 2, 0, 1, 2, 1, 0, 2])

    def test_a_breakeven_trade_ends_a_streak(self):
        nets = [1, 1, 0, 1, -1, -1, -1]
        rows = [self.row(n, day=i + 1) for i, n in enumerate(nets)]
        table, summary = analytics.streaks(rows, UTC)
        self.assertEqual(
            [(r["kind"], r["length"], r["count"]) for r in table],
            [("win", 1, 1), ("win", 2, 1), ("loss", 3, 1)],
        )
        self.assertEqual(summary["longest_loss_streak"]["length"], 3)
        self.assertEqual(summary["current_streak"], {"kind": "loss", "length": 3})

        rows.append(self.row(0, day=8))
        self.assertIsNone(analytics.streaks(rows, UTC)[1]["current_streak"])


class ReportTests(TestCase):
    """Eight trades with hand-computed results. Every trade risks 500, so R is net P&L / 500."""

    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.account = TradingAccount.objects.create(
            user=self.alice, name="A", currency="USD", starting_balance=D("10000")
        )
        for symbol, base in (("EURUSD", "EUR"), ("GBPUSD", "GBP")):
            InstrumentSpec.objects.create(
                symbol=symbol, asset_class="forex", base_currency=base, quote_currency="USD",
                contract_size=D("100000"), pip_size=D("0.0001"),
            )
        self.trades = {}
        # name, side, opened, closed, exit price, stop, rating, symbol        -> net, R
        self.make("T1", "long", at(12, 9, 10), at(12, 9, 40), "1.10500", "1.09500", 5)   # +500, 1.00
        self.make("T2", "long", at(12, 14), at(12, 14, 2), "1.09750", "1.09500", 2)      # -250, -0.50
        self.make("T3", "short", at(13, 9, 30), at(13, 12, 30), "1.10500", "1.10500", 1)  # -500, -1.00
        self.make("T4", "short", at(14, 9), at(14, 9, 20), "1.09500", "1.10500", None)   # +500, 1.00
        self.make("T5", "long", at(15, 10), at(16, 10), "1.10000", "1.09500", 3, "GBPUSD")  # 0, 0.00
        self.make("T6", "long", at(16, 9), at(16, 9, 3), "1.10500", "1.09500", 5)        # +500, 1.00
        self.make("T7", "long", at(16, 15), at(16, 16), "1.09500", None, None)           # -500, no R
        self.make("T8", "short", at(17, 20), at(18, 2), "1.09750", "1.10500", 4)         # +250, 0.50
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def make(self, name, side, opened, closed, exit_price, stop, rating=None, symbol="EURUSD"):
        self.trades[name] = Trade.objects.create(
            account=self.account, symbol=symbol, side=side, opened_at=opened, closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
            stop_loss=D(stop) if stop else None, rating=rating,
        )

    def get(self, slug, **params):
        res = self.client.get(f"/api/reports/{slug}/", params)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def expect(self, row, **expected):
        self.assertEqual({key: row[key] for key in expected}, expected)

    def csv_rows(self, slug, **params):
        res = self.client.get(f"/api/reports/{slug}/csv/", params)
        self.assertEqual(res.status_code, 200, slug)
        raw = b"".join(res.streaming_content)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        rows = list(reader)
        return res, reader.fieldnames, rows

    # ---- the nine reports ----

    def test_by_weekday(self):
        body = self.get("by-weekday")
        self.assertEqual(body["currency"], "USD")
        rows = body["rows"]
        self.assertEqual([r["name"] for r in rows], analytics.WEEKDAYS)
        self.expect(rows[0], trades=2, wins=1, losses=1, breakeven=0, win_rate="50.00",
                    net_pnl="250.00", avg_pnl="125.00", profit_factor="2.00", avg_r="0.25", r_trade_count=2)
        self.expect(rows[1], trades=1, win_rate="0.00", net_pnl="-500.00", profit_factor="0.00", avg_r="-1.00")
        self.expect(rows[2], trades=1, win_rate="100.00", net_pnl="500.00", profit_factor=None)
        self.expect(rows[3], trades=0, win_rate=None, net_pnl="0.00", avg_pnl=None, avg_r=None)
        self.expect(rows[4], trades=3, wins=1, losses=1, breakeven=1, win_rate="33.33",
                    net_pnl="0.00", profit_factor="1.00", avg_r="0.50", r_trade_count=2)
        self.expect(rows[5], trades=0)
        self.expect(rows[6], trades=1, net_pnl="250.00", avg_r="0.50")
        self.assertEqual([c["key"] for c in body["columns"]][:2], ["name", "trades"])

    def test_by_hour_uses_the_open_time(self):
        rows = self.get("by-hour")["rows"]
        self.assertEqual(len(rows), 24)
        self.assertEqual(rows[9]["label"], "09:00")
        self.expect(rows[9], trades=4, wins=3, losses=1, win_rate="75.00", net_pnl="1000.00",
                    avg_pnl="250.00", profit_factor="3.00", avg_r="0.50", r_trade_count=4)
        self.expect(rows[10], trades=1, wins=0, losses=0, breakeven=1, win_rate="0.00",
                    net_pnl="0.00", profit_factor=None, avg_r="0.00")
        self.expect(rows[14], trades=1, losses=1, net_pnl="-250.00", profit_factor="0.00", avg_r="-0.50")
        self.expect(rows[15], trades=1, net_pnl="-500.00", avg_r=None, r_trade_count=0)
        self.expect(rows[20], trades=1, net_pnl="250.00")
        self.expect(rows[0], trades=0)

    def test_by_month_follows_the_user_timezone(self):
        october = self.get("by-month")["rows"]
        self.assertEqual([(r["month"], r["trades"]) for r in october], [("2026-10", 8)])
        self.expect(october[0], wins=4, losses=3, breakeven=1, win_rate="50.00", net_pnl="500.00",
                    avg_pnl="62.50", profit_factor="1.40", avg_r="0.29", r_trade_count=7)

        self.make("T9", "long", at(31, 22), at(31, 23, 30), "1.10500", "1.09500")
        self.alice.timezone = "Africa/Lagos"  # 23:30 UTC is already November there
        self.alice.save()
        rows = self.get("by-month")["rows"]
        self.assertEqual([(r["month"], r["trades"]) for r in rows], [("2026-10", 8), ("2026-11", 1)])

    def test_by_symbol(self):
        rows = self.get("by-symbol")["rows"]
        self.assertEqual([r["symbol"] for r in rows], ["EURUSD", "GBPUSD"])  # best net P&L first
        self.expect(rows[0], trades=7, wins=4, losses=3, win_rate="57.14", net_pnl="500.00",
                    avg_pnl="71.43", profit_factor="1.40", avg_r="0.33", r_trade_count=6)
        self.expect(rows[1], trades=1, breakeven=1, net_pnl="0.00")

    def test_long_versus_short(self):
        long_row, short_row = self.get("long-short")["rows"]
        self.expect(long_row, side="long", trades=5, wins=2, losses=2, breakeven=1, win_rate="40.00",
                    net_pnl="250.00", avg_pnl="50.00", profit_factor="1.33", avg_r="0.38", r_trade_count=4)
        self.expect(short_row, side="short", trades=3, wins=2, losses=1, win_rate="66.67",
                    net_pnl="250.00", avg_pnl="83.33", profit_factor="1.50", avg_r="0.17", r_trade_count=3)

    def test_rating_versus_result(self):
        rows = self.get("by-rating")["rows"]
        self.assertEqual([r["label"] for r in rows], ["1", "2", "3", "4", "5", "Unrated"])
        self.expect(rows[0], trades=1, net_pnl="-500.00", avg_r="-1.00")
        self.expect(rows[1], trades=1, net_pnl="-250.00", avg_r="-0.50")
        self.expect(rows[2], trades=1, breakeven=1, net_pnl="0.00", avg_r="0.00")
        self.expect(rows[3], trades=1, net_pnl="250.00", avg_r="0.50")
        self.expect(rows[4], trades=2, wins=2, win_rate="100.00", net_pnl="1000.00",
                    avg_pnl="500.00", profit_factor=None, avg_r="1.00")
        self.expect(rows[5], trades=2, wins=1, losses=1, win_rate="50.00", net_pnl="0.00",
                    profit_factor="1.00", avg_r="1.00", r_trade_count=1)

    def test_holding_time(self):
        body = self.get("holding-time")
        rows = body["rows"]
        self.assertEqual(
            [r["label"] for r in rows],
            ["Under 5 minutes", "5 to 15 minutes", "15 to 60 minutes", "1 to 4 hours",
             "4 to 24 hours", "1 to 7 days", "Over 7 days"],
        )
        self.assertEqual((rows[0]["min_seconds"], rows[0]["max_seconds"]), (0, 300))
        self.assertIsNone(rows[6]["max_seconds"])
        self.expect(rows[0], trades=2, wins=1, losses=1, net_pnl="250.00", profit_factor="2.00", avg_r="0.25")
        self.expect(rows[1], trades=0)
        self.expect(rows[2], trades=2, wins=2, win_rate="100.00", net_pnl="1000.00", profit_factor=None, avg_r="1.00")
        self.expect(rows[3], trades=2, losses=2, win_rate="0.00", net_pnl="-1000.00",
                    profit_factor="0.00", avg_r="-1.00", r_trade_count=1)
        self.expect(rows[4], trades=1, net_pnl="250.00", avg_r="0.50")
        self.expect(rows[5], trades=1, breakeven=1)
        self.expect(rows[6], trades=0)
        self.assertEqual(body["summary"], {
            "avg_seconds": 15712, "median_seconds": 2700,
            "avg_win_seconds": 6195, "avg_loss_seconds": 4840,
        })

    def test_r_distribution(self):
        body = self.get("r-distribution")
        rows = body["rows"]
        self.assertEqual([r["trades"] for r in rows], [0, 0, 0, 2, 2, 3, 0, 0])
        self.assertEqual(rows[3]["label"], "Between -1 and 0")  # -1.00R is the lower edge of this bucket
        self.expect(rows[3], from_r=-1, to_r=0, share="28.57", net_pnl="-750.00")
        self.expect(rows[4], share="28.57", net_pnl="250.00")
        self.expect(rows[5], share="42.86", net_pnl="1500.00")
        self.assertIsNone(rows[0]["from_r"])
        self.assertIsNone(rows[7]["to_r"])
        self.assertEqual(body["summary"], {
            "r_trade_count": 7, "unscored_trades": 1, "avg_r": "0.29",
            "median_r": "0.50", "best_r": "1.00", "worst_r": "-1.00",
        })

    def test_streaks(self):
        body = self.get("streaks")
        self.assertEqual(
            [(r["kind"], r["length"], r["count"]) for r in body["rows"]],
            [("win", 1, 2), ("win", 2, 1), ("loss", 1, 1), ("loss", 2, 1)],
        )
        summary = body["summary"]
        self.assertEqual(summary["longest_win_streak"], {
            "length": 2, "net_pnl": "1000.00", "start": "2026-10-14", "end": "2026-10-16"})
        self.assertEqual(summary["longest_loss_streak"], {
            "length": 2, "net_pnl": "-750.00", "start": "2026-10-12", "end": "2026-10-13"})
        self.expect(summary, win_runs=3, loss_runs=2, avg_win_streak="1.33", avg_loss_streak="1.50")
        self.assertEqual(summary["current_streak"], {"kind": "win", "length": 1})

        self.make("T9", "long", at(19, 9), at(19, 10), "1.10000", "1.09500")  # a breakeven trade
        after = self.get("streaks")["summary"]
        self.assertIsNone(after["current_streak"])
        self.assertEqual(after["win_runs"], 3)

    # ---- filters, privacy and bad input ----

    def test_filters_apply_to_every_report(self):
        short = self.get("by-weekday", side="short")["rows"]
        self.assertEqual([r["trades"] for r in short], [0, 1, 1, 0, 0, 0, 1])
        self.assertEqual([r["symbol"] for r in self.get("by-symbol", symbol="GBPUSD")["rows"]], ["GBPUSD"])
        late = self.get("by-month", **{"from": "2026-10-16"})["rows"]
        self.assertEqual((late[0]["month"], late[0]["trades"]), ("2026-10", 4))
        self.assertEqual(self.get("by-hour", outcome="loss")["rows"][9]["trades"], 1)
        self.assertEqual(self.get("by-rating", account=self.account.id)["rows"][4]["trades"], 2)

        tag = Tag.objects.create(user=self.alice, name="Breakout", kind="setup")
        self.trades["T1"].tags.add(tag)
        self.trades["T4"].tags.add(tag)
        tagged = self.get("by-weekday", tag=tag.id)["rows"]
        self.assertEqual([r["trades"] for r in tagged], [1, 0, 1, 0, 0, 0, 0])

    def test_empty_and_private(self):
        bob = APIClient()
        bob.force_authenticate(self.bob)
        for slug in NATIVE:
            res = bob.get(f"/api/reports/{slug}/")
            self.assertEqual(res.status_code, 200, slug)
            self.assertEqual(sum(r.get("trades", r.get("count", 0)) for r in res.json()["rows"]), 0, slug)
        streaks = bob.get("/api/reports/streaks/").json()
        self.assertEqual((streaks["rows"], streaks["summary"]["current_streak"]), ([], None))
        dist = bob.get("/api/reports/r-distribution/").json()["summary"]
        self.assertEqual((dist["r_trade_count"], dist["avg_r"], dist["median_r"]), (0, None, None))
        self.assertIsNone(bob.get("/api/reports/holding-time/").json()["summary"]["avg_seconds"])
        theirs = bob.get("/api/reports/by-weekday/", {"account": self.account.id})
        self.assertEqual(theirs.status_code, 400)  # not bob's account

    def test_bad_requests_and_unknown_reports(self):
        for params in ({"from": "2026-13-45"}, {"from": "2026-10-20", "to": "2026-10-01"},
                       {"account": "abc"}, {"outcome": "huge"}, {"tag": "abc"}):
            res = self.client.get("/api/reports/by-weekday/", params)
            self.assertEqual(res.status_code, 400, params)
        self.assertEqual(self.client.get("/api/reports/nope/").status_code, 404)
        self.assertEqual(self.client.get("/api/reports/nope/csv/").status_code, 404)
        self.assertEqual(self.client.get("/api/reports/mistakes/").status_code, 404)  # CSV-only slug

        anonymous = APIClient()
        for url in ("/api/reports/", "/api/reports/by-hour/", "/api/reports/by-hour/csv/"):
            self.assertEqual(anonymous.get(url).status_code, 401, url)

        TradingAccount.objects.create(user=self.alice, name="EUR", currency="EUR")
        self.assertEqual(self.client.get("/api/reports/by-weekday/").status_code, 400)  # mixed currencies
        self.assertEqual(self.get("by-weekday", account=self.account.id)["currency"], "USD")

    # ---- catalog and CSV ----

    def test_catalog_lists_all_fifteen_and_every_csv_matches_its_columns(self):
        catalog = self.client.get("/api/reports/").json()
        self.assertEqual([c["slug"] for c in catalog], ALL_FIFTEEN)
        by_slug = {c["slug"]: c for c in catalog}
        self.assertEqual(by_slug["mistakes"]["url"], "/api/reports/tags/?kind=mistake")
        self.assertEqual(by_slug["by-hour"]["url"], "/api/reports/by-hour/")
        self.assertEqual(by_slug["by-hour"]["csv_url"], "/api/reports/by-hour/csv/")
        self.assertIn("month", by_slug["calendar"]["filters"])
        self.assertNotIn("from", by_slug["calendar"]["filters"])
        for entry in catalog:
            res, header, _rows = self.csv_rows(entry["slug"])
            self.assertEqual(header, [c["key"] for c in entry["columns"]], entry["slug"])
            self.assertTrue(res["Content-Type"].startswith("text/csv"))
            self.assertRegex(
                res["Content-Disposition"],
                rf'^attachment; filename="report-{entry["slug"]}-\d{{4}}-\d{{2}}-\d{{2}}\.csv"$',
            )

    def test_csv_of_the_new_reports(self):
        _, header, rows = self.csv_rows("by-weekday")
        self.assertEqual(header, ["name", "trades", "wins", "losses", "breakeven", "win_rate",
                                  "net_pnl", "avg_pnl", "profit_factor", "avg_r", "r_trade_count"])
        self.assertEqual(len(rows), 7)
        self.assertEqual((rows[0]["name"], rows[0]["trades"], rows[0]["net_pnl"]), ("Monday", "2", "250.00"))
        self.assertEqual(rows[1]["net_pnl"], "-500.00")  # a loss keeps its minus sign
        self.assertEqual((rows[3]["trades"], rows[3]["win_rate"], rows[3]["avg_r"]), ("0", "", ""))

        _, _, streak_rows = self.csv_rows("streaks")
        self.assertEqual(
            [(r["kind"], r["length"], r["count"]) for r in streak_rows],
            [("win", "1", "2"), ("win", "2", "1"), ("loss", "1", "1"), ("loss", "2", "1")],
        )
        _, _, r_rows = self.csv_rows("r-distribution")
        self.assertEqual([r["trades"] for r in r_rows], ["0", "0", "0", "2", "2", "3", "0", "0"])
        _, _, symbol_rows = self.csv_rows("by-symbol", symbol="GBPUSD")
        self.assertEqual([r["symbol"] for r in symbol_rows], ["GBPUSD"])

    def test_csv_of_summary_equity_and_calendar(self):
        _, header, rows = self.csv_rows("summary")
        self.assertEqual(header, ["metric", "value"])
        values = {r["metric"]: r["value"] for r in rows}
        self.assertEqual((values["total_trades"], values["net_pnl"], values["profit_factor"]),
                         ("8", "500.00", "1.40"))

        _, header, rows = self.csv_rows("equity")
        self.assertEqual(header, ["date", "net_pnl", "trade_count", "equity", "drawdown"])
        self.assertEqual([r["date"] for r in rows],
                         ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-16", "2026-10-18"])
        self.assertEqual([r["equity"] for r in rows],
                         ["10250.00", "9750.00", "10250.00", "10250.00", "10500.00"])
        self.assertEqual([r["drawdown"] for r in rows], ["0.00", "500.00", "0.00", "0.00", "0.00"])

        _, header, rows = self.csv_rows("calendar", month="2026-10")
        self.assertEqual(header, ["date", "net_pnl", "trade_count", "wins", "losses", "r_total"])
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            (rows[0]["date"], rows[0]["net_pnl"], rows[0]["trade_count"], rows[0]["wins"],
             rows[0]["losses"], rows[0]["r_total"]),
            ("2026-10-12", "250.00", "2", "1", "1", "0.50"),
        )
        self.assertEqual(self.client.get("/api/reports/calendar/csv/", {"month": "October"}).status_code, 400)

    def test_csv_of_the_tag_reports_is_safe_to_open_in_a_spreadsheet(self):
        fomo = Tag.objects.create(user=self.alice, name="FOMO", kind="mistake")
        sneaky = Tag.objects.create(user=self.alice, name="=cmd()", kind="mistake")
        breakout = Tag.objects.create(user=self.alice, name="Breakout", kind="setup")
        for name in ("T2", "T3", "T7"):
            self.trades[name].tags.add(fomo)
        self.trades["T7"].tags.add(sneaky)
        for name in ("T1", "T4"):
            self.trades[name].tags.add(breakout)

        _, header, rows = self.csv_rows("mistakes")
        self.assertEqual(header, ["name", "trade_count", "wins", "win_rate", "net_pnl",
                                  "money_lost", "avg_r", "r_trade_count"])
        self.assertEqual([r["name"] for r in rows], ["FOMO", "'=cmd()"])  # the formula is defused
        self.assertEqual(
            (rows[0]["trade_count"], rows[0]["win_rate"], rows[0]["net_pnl"],
             rows[0]["money_lost"], rows[0]["avg_r"], rows[0]["r_trade_count"]),
            ("3", "0.00", "-1250.00", "1250.00", "-0.75", "2"),
        )
        _, _, setups = self.csv_rows("setups")
        self.assertEqual(len(setups), 1)
        self.assertEqual(
            (setups[0]["name"], setups[0]["trade_count"], setups[0]["wins"],
             setups[0]["net_pnl"], setups[0]["money_lost"], setups[0]["avg_r"]),
            ("Breakout", "2", "2", "1000.00", "0.00", "1.00"),
        )

    def test_csv_of_playbook_adherence(self):
        playbook = Playbook.objects.create(user=self.alice, name="Breakout play")
        retest = PlaybookRule.objects.create(playbook=playbook, text="Retest", position=1)
        stop = PlaybookRule.objects.create(playbook=playbook, text="Stop placed", position=2)
        for name, ticked in (("T1", [retest.id, stop.id]), ("T2", [retest.id])):
            trade = self.trades[name]
            trade.playbook = playbook
            trade.save()
            trade.sync_rules(ticked)

        _, header, rows = self.csv_rows("playbooks")
        self.assertEqual(header, [
            "playbook", "rule", "trades", "adherence_score", "followed_trades", "followed_net_pnl",
            "followed_avg_pnl", "followed_win_rate", "broken_trades", "broken_net_pnl",
            "broken_avg_pnl", "broken_win_rate",
        ])
        self.assertEqual([(r["playbook"], r["rule"]) for r in rows],
                         [("Breakout play", ""), ("Breakout play", "Stop placed"), ("Breakout play", "Retest")])
        top, stop_row, retest_row = rows
        self.assertEqual(
            (top["trades"], top["adherence_score"], top["followed_trades"], top["followed_net_pnl"],
             top["followed_avg_pnl"], top["followed_win_rate"], top["broken_trades"],
             top["broken_net_pnl"], top["broken_avg_pnl"], top["broken_win_rate"]),
            ("2", "75.00", "1", "500.00", "500.00", "100.00", "1", "-250.00", "-250.00", "0.00"),
        )
        self.assertEqual(
            (stop_row["adherence_score"], stop_row["followed_trades"], stop_row["broken_trades"],
             stop_row["broken_net_pnl"]),
            ("", "1", "1", "-250.00"),
        )
        self.assertEqual((retest_row["broken_trades"], retest_row["broken_net_pnl"]), ("0", "0.00"))

    def test_ten_thousand_trades_stay_fast(self):
        base = at(1, 0)
        Trade.objects.bulk_create(
            [
                Trade(
                    account=self.account, symbol="EURUSD", side="long" if i % 2 else "short",
                    opened_at=base + timedelta(minutes=i), closed_at=base + timedelta(minutes=i, seconds=90),
                    entry_price=D("1.1"), exit_price=D("1.1"), lots=D("1"), gross_pnl=D("0"),
                    net_pnl=D("25.00") if i % 3 else D("-40.00"),
                    r_multiple=D("0.50") if i % 4 else None, duration_seconds=90, rating=(i % 5) + 1,
                )
                for i in range(10_000)
            ],
            batch_size=1000,
        )
        for slug in NATIVE:
            started = time.perf_counter()
            res = self.client.get(f"/api/reports/{slug}/")
            elapsed = time.perf_counter() - started
            self.assertEqual(res.status_code, 200, slug)
            self.assertLess(elapsed, 1.5, slug)