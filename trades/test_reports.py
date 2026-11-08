from datetime import date, datetime, timezone as dt_tz
from decimal import Decimal as D
from zoneinfo import ZoneInfo

from django.test import SimpleTestCase

from .reports import daily_buckets, equity_curve, jsonable, summarize, weekly_totals


def at(day, hour=12, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=dt_tz.utc)


ROWS = [
    (at(1), D("100"), D("1.00")),
    (at(2), D("-50"), D("-0.50")),
    (at(3), D("200"), D("2.00")),
    (at(4), D("-150"), D("-1.00")),
    (at(5), D("0"), None),
]


class ReportMathTests(SimpleTestCase):
    def test_summary_known_numbers(self):
        out = jsonable(summarize(ROWS, D("1000")))
        self.assertEqual(out["total_trades"], 5)
        self.assertEqual((out["wins"], out["losses"], out["breakeven"]), (2, 2, 1))
        self.assertEqual(out["win_rate"], "40.00")
        self.assertEqual(out["net_pnl"], "100.00")
        self.assertEqual(out["gross_profit"], "300.00")
        self.assertEqual(out["gross_loss"], "200.00")
        self.assertEqual(out["profit_factor"], "1.50")
        self.assertEqual(out["avg_win"], "150.00")
        self.assertEqual(out["avg_loss"], "-100.00")
        self.assertEqual(out["expectancy"], "20.00")
        self.assertEqual(out["avg_r"], "0.38")
        self.assertEqual(out["r_trade_count"], 4)
        self.assertEqual(out["largest_win"], "200.00")
        self.assertEqual(out["largest_loss"], "-150.00")
        self.assertEqual(out["max_drawdown"], "150.00")
        self.assertEqual(out["max_drawdown_pct"], "12.00")
        self.assertEqual(out["closing_balance"], "1100.00")

    def test_empty_summary(self):
        out = jsonable(summarize([], D("500")))
        self.assertEqual(out["total_trades"], 0)
        for key in ("win_rate", "profit_factor", "expectancy", "avg_r", "largest_win"):
            self.assertIsNone(out[key])
        self.assertEqual(out["net_pnl"], "0.00")
        self.assertEqual(out["max_drawdown"], "0.00")
        self.assertEqual(out["closing_balance"], "500.00")

    def test_no_losses_means_no_profit_factor(self):
        rows = [(at(1), D("100"), None), (at(2), D("50"), None)]
        out = jsonable(summarize(rows, D("0")))
        self.assertIsNone(out["profit_factor"])
        self.assertIsNone(out["avg_loss"])
        self.assertEqual(out["win_rate"], "100.00")
        self.assertEqual(out["max_drawdown_pct"], "0.00")

    def test_days_follow_the_user_timezone(self):
        rows = [(at(15, 23, 30), D("10"), None)]
        self.assertEqual(daily_buckets(rows, ZoneInfo("UTC"))[0]["date"], date(2026, 10, 15))
        self.assertEqual(
            daily_buckets(rows, ZoneInfo("Africa/Lagos"))[0]["date"], date(2026, 10, 16)
        )

    def test_equity_curve_tracks_drawdown(self):
        buckets = daily_buckets(ROWS[:4], ZoneInfo("UTC"))
        points = jsonable(equity_curve(buckets, D("1000")))
        self.assertEqual(
            [p["equity"] for p in points], ["1100.00", "1050.00", "1250.00", "1100.00"]
        )
        self.assertEqual(
            [p["drawdown"] for p in points], ["0.00", "50.00", "0.00", "150.00"]
        )

    def test_weeks_start_on_monday(self):
        rows = [
            (at(15), D("10"), D("1.00")),
            (at(16), D("20"), None),
            (at(19), D("-5"), D("-0.50")),
        ]
        weeks = jsonable(weekly_totals(daily_buckets(rows, ZoneInfo("UTC"))))
        self.assertEqual([w["week_start"] for w in weeks], ["2026-10-12", "2026-10-19"])
        self.assertEqual(weeks[0]["net_pnl"], "30.00")
        self.assertEqual(weeks[0]["trade_count"], 2)
        self.assertEqual(weeks[1]["net_pnl"], "-5.00")