from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import DailyRollup, InstrumentSpec, Tag, Trade, TradingAccount


def utc(day, hour=12):
    return datetime(2026, 10, day, hour, 0, tzinfo=dt_tz.utc)


class TagAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.account = TradingAccount.objects.create(
            user=self.alice, name="A", currency="USD", starting_balance=D("10000")
        )
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        self.fomo = Tag.objects.create(user=self.alice, name="FOMO", kind="mistake")
        self.moved = Tag.objects.create(user=self.alice, name="Moved stop", kind="mistake")
        self.breakout = Tag.objects.create(user=self.alice, name="Breakout", kind="setup")
        self.bob_tag = Tag.objects.create(user=self.bob, name="Revenge", kind="mistake")

        # Every trade risks 500 (stop 1.09500), so R = P&L / 500
        self.t1 = self.trade(15, "1.10500", [self.breakout])                 # +500, 1.00R
        self.t2 = self.trade(16, "1.09750", [self.fomo, self.breakout])      # -250, -0.50R
        self.t3 = self.trade(17, "1.09500", [self.fomo, self.moved])         # -500, -1.00R
        self.t4 = self.trade(18, "1.10500", [self.fomo])                     # +500, 1.00R

        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def trade(self, day, exit_price, tags):
        closed = utc(day)
        trade = Trade.objects.create(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
            stop_loss=D("1.09500"),
        )
        trade.tags.set(tags)
        return trade

    def payload(self, **extra):
        body = {
            "account": self.account.id, "symbol": "EURUSD", "side": "long",
            "opened_at": utc(20, 9).isoformat(), "closed_at": utc(20, 10).isoformat(),
            "entry_price": "1.10000", "exit_price": "1.10500", "lots": "1",
        }
        body.update(extra)
        return body

    def bulk(self, client=None, **body):
        return (client or self.client).post("/api/trades/bulk/", body, format="json")

    # ---- tags ----

    def test_create_tag_trims_and_blocks_case_insensitive_duplicates(self):
        res = self.client.post("/api/tags/", {"name": "  Overtrading ", "kind": "mistake"}, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["name"], "Overtrading")
        dup = self.client.post("/api/tags/", {"name": "overtrading", "kind": "mistake"}, format="json")
        self.assertEqual(dup.status_code, 400)
        other_kind = self.client.post("/api/tags/", {"name": "overtrading", "kind": "condition"}, format="json")
        self.assertEqual(other_kind.status_code, 201)
        bad = self.client.post("/api/tags/", {"name": "x", "kind": "feeling"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_tag_list_is_scoped_filtered_and_counted(self):
        listed = self.client.get("/api/tags/").json()
        self.assertEqual(sorted(t["name"] for t in listed), ["Breakout", "FOMO", "Moved stop"])
        self.assertEqual({t["name"]: t["trade_count"] for t in listed}["FOMO"], 3)
        self.assertEqual(len(self.client.get("/api/tags/", {"kind": "mistake"}).json()), 2)

        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual([t["name"] for t in bob.get("/api/tags/").json()], ["Revenge"])
        self.assertEqual(bob.get(f"/api/tags/{self.fomo.id}/").status_code, 404)

    # ---- tags on trades ----

    def test_trade_create_and_update_with_tags(self):
        res = self.client.post("/api/trades/", self.payload(tag_ids=[self.fomo.id]), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual([t["name"] for t in res.json()["tags"]], ["FOMO"])
        trade_id = res.json()["id"]

        patched = self.client.patch(
            f"/api/trades/{trade_id}/", {"tag_ids": [self.breakout.id, self.moved.id]}, format="json"
        )
        self.assertEqual(sorted(t["name"] for t in patched.json()["tags"]), ["Breakout", "Moved stop"])
        cleared = self.client.patch(f"/api/trades/{trade_id}/", {"tag_ids": []}, format="json")
        self.assertEqual(cleared.json()["tags"], [])

    def test_cannot_use_another_users_tag(self):
        res = self.client.post("/api/trades/", self.payload(tag_ids=[self.bob_tag.id]), format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Trade.objects.filter(closed_at=utc(20, 10)).count(), 0)

    def test_trade_log_filters_by_tag_and_outcome(self):
        def count(**params):
            return self.client.get("/api/trades/", params).json()["count"]

        self.assertEqual(count(tag=self.fomo.id), 3)
        self.assertEqual(count(tag=f"{self.moved.id},{self.breakout.id}"), 3)  # any of the tags
        self.assertEqual(count(outcome="loss"), 2)
        self.assertEqual(count(outcome="win"), 2)
        self.assertEqual(count(tag=self.fomo.id, outcome="win"), 1)
        self.assertEqual(self.client.get("/api/trades/", {"outcome": "huge"}).status_code, 400)
        self.assertEqual(self.client.get("/api/trades/", {"tag": "abc"}).status_code, 400)

    # ---- reports ----

    def test_mistake_report_ranks_by_money_lost(self):
        body = self.client.get("/api/reports/tags/").json()
        self.assertEqual((body["kind"], body["currency"]), ("mistake", "USD"))
        fomo, moved = body["tags"]
        self.assertEqual(fomo["name"], "FOMO")
        self.assertEqual((fomo["trade_count"], fomo["wins"]), (3, 1))
        self.assertEqual(fomo["win_rate"], "33.33")
        self.assertEqual(fomo["net_pnl"], "-250.00")
        self.assertEqual(fomo["money_lost"], "750.00")
        self.assertEqual(fomo["avg_r"], "-0.17")
        self.assertEqual(moved["name"], "Moved stop")
        self.assertEqual((moved["money_lost"], moved["avg_r"]), ("500.00", "-1.00"))

    def test_setup_report_date_window_and_bad_kind(self):
        setup = self.client.get("/api/reports/tags/", {"kind": "setup"}).json()["tags"]
        self.assertEqual(len(setup), 1)
        self.assertEqual(
            (setup[0]["name"], setup[0]["trade_count"], setup[0]["net_pnl"], setup[0]["avg_r"]),
            ("Breakout", 2, "250.00", "0.25"),
        )

        late = self.client.get("/api/reports/tags/", {"from": "2026-10-17"}).json()["tags"]
        self.assertEqual([t["name"] for t in late], ["FOMO", "Moved stop"])  # tie on loss, more trades first
        self.assertEqual((late[0]["net_pnl"], late[0]["money_lost"]), ("0.00", "500.00"))

        self.assertEqual(self.client.get("/api/reports/tags/", {"kind": "feeling"}).status_code, 400)

    def test_reports_respect_the_tag_filter(self):
        summary = self.client.get("/api/reports/summary/", {"tag": self.fomo.id}).json()
        self.assertEqual((summary["total_trades"], summary["net_pnl"]), (3, "-250.00"))
        equity = self.client.get("/api/reports/equity/", {"tag": self.moved.id}).json()["points"]
        self.assertEqual([(p["date"], p["equity"]) for p in equity], [("2026-10-17", "9500.00")])

    # ---- bulk edit ----

    def test_bulk_add_and_remove_tags(self):
        res = self.bulk(ids=[self.t1.id, self.t4.id], action="add_tags", tag_ids=[self.moved.id])
        self.assertEqual(res.json(), {"requested": 2, "affected": 2})
        self.assertEqual(Trade.objects.filter(tags=self.moved).count(), 3)

        self.bulk(ids=[self.t1.id], action="add_tags", tag_ids=[self.moved.id])  # again: no error, no duplicate
        self.assertEqual(Trade.objects.filter(tags=self.moved).count(), 3)

        self.bulk(ids=[self.t1.id, self.t3.id], action="remove_tags", tag_ids=[self.moved.id])
        remaining = list(Trade.objects.filter(tags=self.moved).values_list("id", flat=True))
        self.assertEqual(remaining, [self.t4.id])

    def test_bulk_cannot_touch_other_users_data(self):
        bob = APIClient()
        bob.force_authenticate(self.bob)
        res = self.bulk(client=bob, ids=[self.t1.id], action="delete")
        self.assertEqual(res.json(), {"requested": 1, "affected": 0})
        self.assertTrue(Trade.objects.filter(pk=self.t1.pk).exists())

        stolen = self.bulk(ids=[self.t1.id], action="add_tags", tag_ids=[self.bob_tag.id])
        self.assertEqual(stolen.status_code, 400)
        self.assertEqual(self.bulk(ids=[self.t1.id], action="add_tags").status_code, 400)
        self.assertEqual(self.bulk(ids=[], action="delete").status_code, 400)
        self.assertEqual(self.bulk(ids=[self.t1.id], action="explode").status_code, 400)

    def test_bulk_delete_refreshes_rollups(self):
        self.assertEqual(DailyRollup.objects.filter(account=self.account).count(), 4)
        res = self.bulk(ids=[self.t1.id, self.t2.id], action="delete")
        self.assertEqual(res.json()["affected"], 2)
        days = sorted(r.date.isoformat() for r in DailyRollup.objects.filter(account=self.account))
        self.assertEqual(days, ["2026-10-17", "2026-10-18"])
        self.assertEqual(Trade.objects.count(), 2)