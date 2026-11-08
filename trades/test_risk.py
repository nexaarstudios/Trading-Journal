import io
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from .models import DailyRollup, InstrumentSpec, RiskRule, Trade, TradingAccount
from .risk import level_for, worst


def utc(day, hour):
    return datetime(2026, 10, day, hour, 0, tzinfo=dt_tz.utc)


class LevelTests(SimpleTestCase):
    def test_thresholds(self):
        cases = (
            ("0", "ok"), ("0.69", "ok"), ("0.70", "warning"), ("0.89", "warning"),
            ("0.90", "alert"), ("0.99", "alert"), ("1", "breached"), ("2.5", "breached"),
        )
        for ratio, expected in cases:
            self.assertEqual(level_for(D(ratio)), expected, ratio)

    def test_worst(self):
        self.assertEqual(worst([]), "ok")
        self.assertEqual(worst(["ok", "alert", "warning"]), "alert")


class RiskTests(TestCase):
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
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def trade(self, day, hour, exit_price, stop="1.09500"):
        """1 lot from 1.10000 with the stop at 1.09500, so every trade risks 500."""
        closed = utc(day, hour)
        return Trade.objects.create(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
            stop_loss=D(stop) if stop else None,
        )

    def history(self):
        self.trade(14, 10, "1.10500")  # +500, equity 10500 (the peak)
        self.trade(15, 9, "1.09700")   # -300, equity 10200
        self.trade(15, 11, "1.09600")  # -400, equity 9800

    def rule(self, kind, unit, limit):
        return RiskRule.objects.create(
            account=self.account, kind=kind, unit=unit, limit_value=D(limit)
        )

    def status(self, client=None, **params):
        params.setdefault("account", self.account.id)
        return (client or self.client).get("/api/risk/status/", params)

    def by_kind(self, response):
        return {r["kind"]: r for r in response.json()["rules"]}

    # ---- stored risk ----

    def test_trade_stores_its_risk(self):
        self.assertEqual(self.trade(14, 10, "1.10500").risk_amount, D("500.00"))
        self.assertIsNone(self.trade(14, 12, "1.10500", stop=None).risk_amount)

    def test_recompute_command_restores_risk_and_pnl(self):
        trade = self.trade(14, 10, "1.10500")
        Trade.objects.filter(pk=trade.pk).update(risk_amount=None, net_pnl=D("0"))
        call_command("recompute_trades", stdout=io.StringIO())
        trade.refresh_from_db()
        self.assertEqual((trade.risk_amount, trade.net_pnl), (D("500.00"), D("500.00")))
        self.assertEqual(DailyRollup.objects.get(account=self.account).net_pnl, D("500.00"))

    # ---- status ----

    def test_status_numbers(self):
        self.history()
        self.rule("daily_loss", "money", "1000")
        self.rule("max_drawdown", "percent", "10")
        self.rule("trades_per_day", "count", "5")
        self.rule("risk_per_trade", "percent", "1")
        res = self.status(date="2026-10-15")
        self.assertEqual(res.status_code, 200)
        body, rules = res.json(), self.by_kind(res)

        loss = rules["daily_loss"]
        self.assertEqual((loss["used"], loss["limit"], loss["remaining"]), ("700.00", "1000.00", "300.00"))
        self.assertEqual((loss["percent_of_limit"], loss["level"]), ("70.00", "warning"))

        drawdown = rules["max_drawdown"]
        self.assertEqual((drawdown["used"], drawdown["limit"], drawdown["remaining"]), ("7.00", "10.00", "3.00"))
        self.assertEqual((drawdown["percent_of_limit"], drawdown["level"]), ("70.00", "warning"))

        count = rules["trades_per_day"]
        self.assertEqual((count["used"], count["limit"], count["remaining"]), (2, 5, 3))
        self.assertEqual((count["percent_of_limit"], count["level"]), ("40.00", "ok"))

        per_trade = rules["risk_per_trade"]  # 500 risked on a balance of 10200 when the last trade opened
        self.assertEqual((per_trade["used"], per_trade["limit"]), ("4.90", "1.00"))
        self.assertEqual((per_trade["percent_of_limit"], per_trade["level"]), ("490.20", "breached"))

        self.assertEqual(body["status"], "breached")
        self.assertEqual(body["context"], {
            "starting_balance": "10000.00", "equity": "9800.00", "peak_equity": "10500.00",
            "drawdown": "700.00", "day_net_pnl": "-700.00", "day_trade_count": 2,
        })

    def test_levels_follow_the_limit(self):
        self.history()
        rule = self.rule("daily_loss", "money", "2000")
        for limit, level in (("2000", "ok"), ("800", "warning"), ("750", "alert"), ("700", "breached")):
            RiskRule.objects.filter(pk=rule.pk).update(limit_value=D(limit))
            got = self.by_kind(self.status(date="2026-10-15"))["daily_loss"]["level"]
            self.assertEqual(got, level, limit)

    def test_money_units(self):
        self.history()
        self.rule("max_drawdown", "money", "1000")
        self.rule("risk_per_trade", "money", "600")
        rules = self.by_kind(self.status(date="2026-10-15"))
        self.assertEqual((rules["max_drawdown"]["used"], rules["max_drawdown"]["level"]), ("700.00", "warning"))
        self.assertEqual(
            (rules["risk_per_trade"]["used"], rules["risk_per_trade"]["percent_of_limit"],
             rules["risk_per_trade"]["level"]),
            ("500.00", "83.33", "warning"),
        )

    def test_trades_per_day_levels(self):
        self.history()
        rule = self.rule("trades_per_day", "count", "3")
        self.assertEqual(self.by_kind(self.status(date="2026-10-15"))["trades_per_day"]["level"], "ok")
        RiskRule.objects.filter(pk=rule.pk).update(limit_value=D("2"))
        self.assertEqual(self.by_kind(self.status(date="2026-10-15"))["trades_per_day"]["level"], "breached")
        self.assertEqual(self.by_kind(self.status(date="2026-10-14"))["trades_per_day"]["used"], 1)

    def test_status_for_an_earlier_day(self):
        self.history()
        self.rule("daily_loss", "money", "500")
        self.rule("max_drawdown", "percent", "10")
        self.rule("risk_per_trade", "percent", "1")
        res = self.status(date="2026-10-14")
        rules = self.by_kind(res)
        self.assertEqual(rules["daily_loss"]["used"], "0.00")       # a winning day is not a loss
        self.assertEqual(rules["max_drawdown"]["used"], "0.00")     # nothing later is counted
        self.assertEqual(rules["risk_per_trade"]["used"], "5.00")   # 500 on a balance of 10000
        self.assertEqual(res.json()["context"]["equity"], "10500.00")

    def test_drawdown_carries_over_to_a_quiet_day(self):
        self.history()
        self.rule("daily_loss", "money", "500")
        self.rule("max_drawdown", "percent", "10")
        self.rule("trades_per_day", "count", "5")
        rules = self.by_kind(self.status(date="2026-10-20"))
        self.assertEqual(rules["max_drawdown"]["used"], "7.00")
        self.assertEqual(rules["daily_loss"]["used"], "0.00")
        self.assertEqual(rules["trades_per_day"]["used"], 0)

    def test_disabled_rules_are_left_out(self):
        self.history()
        rule = self.rule("daily_loss", "money", "500")
        RiskRule.objects.filter(pk=rule.pk).update(enabled=False)
        body = self.status(date="2026-10-15").json()
        self.assertEqual((body["rules"], body["status"]), ([], "ok"))

    def test_status_inputs_and_ownership(self):
        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(self.status(client=bob).status_code, 400)  # not his account
        self.assertEqual(self.client.get("/api/risk/status/").status_code, 400)
        self.assertEqual(self.status(date="2026-13-45").status_code, 400)
        self.assertEqual(self.status(account="abc").status_code, 400)

    # ---- managing rules ----

    def create(self, **body):
        body.setdefault("account", self.account.id)
        return self.client.post("/api/risk-rules/", body, format="json")

    def test_validation(self):
        empty_account = TradingAccount.objects.create(user=self.alice, name="Zero", starting_balance=D("0"))
        cases = (
            {"kind": "daily_loss", "limit_value": "0"},
            {"kind": "daily_loss", "limit_value": "-5"},
            {"kind": "daily_loss", "unit": "count", "limit_value": "5"},
            {"kind": "daily_loss", "unit": "percent", "limit_value": "150"},
            {"kind": "trades_per_day", "unit": "money", "limit_value": "3"},
            {"kind": "trades_per_day", "limit_value": "2.5"},
            {"kind": "nonsense", "limit_value": "5"},
            {"kind": "daily_loss", "limit_value": "5", "account": self.bob_account.id},
            {"kind": "daily_loss", "unit": "percent", "limit_value": "2", "account": empty_account.id},
        )
        for body in cases:
            self.assertEqual(self.create(**body).status_code, 400, body)
        self.assertEqual(RiskRule.objects.count(), 0)
        ok = self.create(kind="risk_per_trade", unit="percent", limit_value="1", account=empty_account.id)
        self.assertEqual(ok.status_code, 201)  # per-trade percent doesn't need a starting balance

    def test_create_update_delete(self):
        made = self.create(kind="daily_loss", limit_value="500")
        self.assertEqual(made.status_code, 201)
        self.assertEqual(made.json()["unit"], "money")
        self.assertEqual(self.create(kind="daily_loss", limit_value="300").status_code, 400)  # one per kind

        count = self.create(kind="trades_per_day", limit_value="4")
        self.assertEqual((count.status_code, count.json()["unit"]), (201, "count"))

        url = f"/api/risk-rules/{made.json()['id']}/"
        self.assertEqual(self.client.patch(url, {"limit_value": "750"}, format="json").json()["limit_value"], "750.00")
        switched = self.client.patch(url, {"unit": "percent", "limit_value": "5"}, format="json")
        self.assertEqual((switched.json()["unit"], switched.json()["limit_value"]), ("percent", "5.00"))
        self.assertEqual(self.client.patch(url, {"kind": "max_drawdown"}, format="json").status_code, 400)
        second = TradingAccount.objects.create(user=self.alice, name="Second", starting_balance=D("1000"))
        self.assertEqual(self.client.patch(url, {"account": second.id}, format="json").status_code, 400)
        self.assertEqual(self.client.patch(url, {"enabled": False}, format="json").json()["enabled"], False)

        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertEqual(RiskRule.objects.count(), 1)

    def test_rules_are_private(self):
        made = self.create(kind="daily_loss", limit_value="500").json()
        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(bob.get("/api/risk-rules/").json(), [])
        self.assertEqual(bob.get(f"/api/risk-rules/{made['id']}/").status_code, 404)
        self.assertEqual(bob.delete(f"/api/risk-rules/{made['id']}/").status_code, 404)
        self.assertEqual(len(self.client.get("/api/risk-rules/", {"account": self.account.id}).json()), 1)
        self.assertEqual(self.client.get("/api/risk-rules/", {"account": self.bob_account.id}).json(), [])