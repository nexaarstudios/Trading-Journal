from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .models import InstrumentSpec, Playbook, PlaybookRule, Trade, TradingAccount


def utc(day, hour=12):
    return datetime(2026, 10, day, hour, 0, tzinfo=dt_tz.utc)


class PlaybookTests(TestCase):
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
        self.pb = Playbook.objects.create(user=self.alice, name="Breakout")
        self.r1 = PlaybookRule.objects.create(playbook=self.pb, text="Waited for retest", position=1)
        self.r2 = PlaybookRule.objects.create(playbook=self.pb, text="Risk 1% or less", position=2)
        self.r3 = PlaybookRule.objects.create(playbook=self.pb, text="Stop placed", position=3)
        self.bob_pb = Playbook.objects.create(user=self.bob, name="Bobs")
        self.bob_rule = PlaybookRule.objects.create(playbook=self.bob_pb, text="Bob rule", position=1)
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def payload(self, **extra):
        body = {
            "account": self.account.id, "symbol": "EURUSD", "side": "long",
            "opened_at": utc(20, 9).isoformat(), "closed_at": utc(20, 10).isoformat(),
            "entry_price": "1.10000", "exit_price": "1.10500", "lots": "1",
        }
        body.update(extra)
        return body

    def post_trade(self, **extra):
        return self.client.post("/api/trades/", self.payload(**extra), format="json")

    def logged(self, day, exit_price, ticked, playbook=None):
        """A trade logged through the model: 1 lot from 1.10000."""
        closed = utc(day)
        trade = Trade.objects.create(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"), playbook=playbook,
        )
        if playbook is not None:
            trade.sync_rules([r.id for r in ticked])
        return trade

    def build_history(self):
        r1, r2, r3 = self.r1, self.r2, self.r3
        self.logged(15, "1.10500", [r1, r2, r3], self.pb)   # +500, followed
        self.logged(16, "1.09750", [r1, r2], self.pb)       # -250, broke "Stop placed"
        self.logged(17, "1.09500", [r1], self.pb)           # -500, broke two rules
        self.logged(18, "1.09750", [r1, r2, r3], self.pb)   # -250, followed
        self.logged(19, "1.10500", [], None)                # +500, no playbook

    # ---- playbooks and rules ----

    def test_playbooks_and_rules_crud(self):
        res = self.client.post("/api/playbooks/", {"name": "Reversal", "description": "Fade extremes"}, format="json")
        self.assertEqual(res.status_code, 201)
        pid = res.json()["id"]
        dup = self.client.post("/api/playbooks/", {"name": "reversal"}, format="json")
        self.assertEqual(dup.status_code, 400)

        first = self.client.post("/api/playbook-rules/", {"playbook": pid, "text": "Wait for rejection"}, format="json")
        second = self.client.post("/api/playbook-rules/", {"playbook": pid, "text": "Confirm on 15m"}, format="json")
        self.assertEqual([first.json()["position"], second.json()["position"]], [1, 2])
        rules = self.client.get(f"/api/playbooks/{pid}/").json()["rules"]
        self.assertEqual([r["text"] for r in rules], ["Wait for rejection", "Confirm on 15m"])

        foreign = self.client.post("/api/playbook-rules/", {"playbook": self.bob_pb.id, "text": "x"}, format="json")
        self.assertEqual(foreign.status_code, 400)
        rid = first.json()["id"]
        edited = self.client.patch(f"/api/playbook-rules/{rid}/", {"text": "Wait for a rejection candle"}, format="json")
        self.assertEqual(edited.status_code, 200)
        moved = self.client.patch(f"/api/playbook-rules/{rid}/", {"playbook": self.pb.id}, format="json")
        self.assertEqual(moved.status_code, 400)

        self.assertEqual(self.client.delete(f"/api/playbook-rules/{rid}/").status_code, 204)
        self.assertTrue(PlaybookRule.objects.get(pk=rid).archived)
        rules = self.client.get(f"/api/playbooks/{pid}/").json()["rules"]
        self.assertEqual([r["text"] for r in rules], ["Confirm on 15m"])

        self.assertEqual(self.client.delete(f"/api/playbooks/{pid}/").status_code, 204)
        active = [p["name"] for p in self.client.get("/api/playbooks/").json()]
        everything = [p["name"] for p in self.client.get("/api/playbooks/", {"include_archived": "1"}).json()]
        self.assertNotIn("Reversal", active)
        self.assertIn("Reversal", everything)

        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(bob.get(f"/api/playbooks/{self.pb.id}/").status_code, 404)

    # ---- ticking rules on trades ----

    def test_trade_snapshots_rules_and_ticks(self):
        res = self.post_trade(playbook=self.pb.id, checked_rule_ids=[self.r1.id, self.r2.id])
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual((body["rules_total"], body["rules_checked"]), (3, 2))
        ticks = {c["text"]: c["checked"] for c in body["rule_checks"]}
        self.assertEqual(ticks, {"Waited for retest": True, "Risk 1% or less": True, "Stop placed": False})

        url = f"/api/trades/{body['id']}/"
        everything = self.client.patch(url, {"checked_rule_ids": [self.r1.id, self.r2.id, self.r3.id]}, format="json")
        self.assertEqual(everything.json()["rules_checked"], 3)
        none = self.client.patch(url, {"checked_rule_ids": []}, format="json")
        self.assertEqual((none.json()["rules_total"], none.json()["rules_checked"]), (3, 0))
        untouched = self.client.patch(url, {"notes": "Clean entry"}, format="json")
        self.assertEqual(untouched.json()["rules_total"], 3)

    def test_new_rules_do_not_change_old_trades(self):
        old = self.post_trade(playbook=self.pb.id).json()
        PlaybookRule.objects.create(playbook=self.pb, text="Checked news", position=4)
        self.assertEqual(self.client.get(f"/api/trades/{old['id']}/").json()["rules_total"], 3)
        self.assertEqual(self.post_trade(playbook=self.pb.id).json()["rules_total"], 4)

    def test_archived_rule_stays_on_old_trades_only(self):
        old = self.post_trade(playbook=self.pb.id).json()
        self.assertEqual(self.client.delete(f"/api/playbook-rules/{self.r3.id}/").status_code, 204)
        self.assertEqual(len(self.client.get(f"/api/trades/{old['id']}/").json()["rule_checks"]), 3)
        self.assertEqual(self.post_trade(playbook=self.pb.id).json()["rules_total"], 2)

    def test_changing_or_removing_the_playbook(self):
        other = Playbook.objects.create(user=self.alice, name="London")
        PlaybookRule.objects.create(playbook=other, text="Only trade London", position=1)
        trade = self.post_trade(playbook=self.pb.id, checked_rule_ids=[self.r1.id]).json()
        url = f"/api/trades/{trade['id']}/"

        switched = self.client.patch(url, {"playbook": other.id}, format="json").json()
        self.assertEqual((switched["rules_total"], switched["rules_checked"]), (1, 0))
        self.assertEqual([c["text"] for c in switched["rule_checks"]], ["Only trade London"])

        cleared = self.client.patch(url, {"playbook": None}, format="json").json()
        self.assertEqual((cleared["rules_total"], cleared["rule_checks"]), (0, []))

        back = self.client.patch(
            url, {"playbook": self.pb.id, "checked_rule_ids": [self.r1.id]}, format="json"
        ).json()
        self.assertEqual((back["rules_total"], back["rules_checked"]), (3, 1))

    def test_rejects_bad_rule_ticks(self):
        cases = (
            {"checked_rule_ids": [self.r1.id]},                                  # no playbook
            {"playbook": self.pb.id, "checked_rule_ids": [self.bob_rule.id]},    # someone else's rule
            {"playbook": self.pb.id, "checked_rule_ids": [999999]},              # unknown rule
            {"playbook": self.bob_pb.id},                                         # someone else's playbook
        )
        for extra in cases:
            self.assertEqual(self.post_trade(**extra).status_code, 400, extra)
        self.assertEqual(Trade.objects.count(), 0)  # nothing half-saved

    def test_rating_and_notes(self):
        self.assertEqual(self.post_trade(rating=6).status_code, 400)
        self.assertEqual(self.post_trade(rating=0).status_code, 400)
        ok = self.post_trade(rating=4, notes="Waited for the retest.")
        self.assertEqual(ok.status_code, 201)
        self.assertEqual((ok.json()["rating"], ok.json()["notes"]), (4, "Waited for the retest."))

    def test_filter_by_playbook(self):
        self.build_history()
        self.assertEqual(self.client.get("/api/trades/", {"playbook": self.pb.id}).json()["count"], 4)
        self.assertEqual(self.client.get("/api/trades/", {"playbook": self.bob_pb.id}).json()["count"], 0)
        self.assertEqual(self.client.get("/api/trades/").json()["count"], 5)

    # ---- the report ----

    def test_playbook_report(self):
        self.build_history()
        body = self.client.get("/api/reports/playbooks/").json()
        self.assertEqual(len(body["playbooks"]), 1)
        pb = body["playbooks"][0]
        self.assertEqual((pb["name"], pb["trade_count"], pb["unscored_trades"]), ("Breakout", 4, 0))
        self.assertEqual(pb["net_pnl"], "-500.00")
        self.assertEqual(pb["adherence_score"], "75.00")      # 9 of 12 rules ticked
        self.assertEqual(pb["followed"], {
            "trades": 2, "net_pnl": "250.00", "avg_pnl": "125.00", "win_rate": "50.00",
        })
        self.assertEqual(pb["broken"], {
            "trades": 2, "net_pnl": "-750.00", "avg_pnl": "-375.00", "win_rate": "0.00",
        })

        rules = pb["rules"]  # worst cost first
        self.assertEqual([r["text"] for r in rules], ["Stop placed", "Risk 1% or less", "Waited for retest"])
        stop, risk, retest = rules
        self.assertEqual((stop["broken_trades"], stop["net_when_broken"], stop["avg_when_broken"]),
                         (2, "-750.00", "-375.00"))
        self.assertEqual((stop["followed_trades"], stop["net_when_followed"], stop["avg_when_followed"]),
                         (2, "250.00", "125.00"))
        self.assertEqual((risk["broken_trades"], risk["net_when_broken"]), (1, "-500.00"))
        self.assertEqual((risk["followed_trades"], risk["net_when_followed"]), (3, "0.00"))
        self.assertEqual((retest["broken_trades"], retest["avg_when_broken"]), (0, None))
        self.assertEqual(retest["avg_when_followed"], "-125.00")

    def test_playbook_report_window_and_scoping(self):
        self.build_history()
        late = self.client.get("/api/reports/playbooks/", {"from": "2026-10-17"}).json()["playbooks"][0]
        self.assertEqual(late["trade_count"], 2)              # the no-playbook trade is never counted
        self.assertEqual(late["adherence_score"], "66.67")    # 4 of 6
        self.assertEqual((late["followed"]["trades"], late["followed"]["net_pnl"]), (1, "-250.00"))
        self.assertEqual((late["broken"]["trades"], late["broken"]["net_pnl"]), (1, "-500.00"))

        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(bob.get("/api/reports/playbooks/").json()["playbooks"], [])