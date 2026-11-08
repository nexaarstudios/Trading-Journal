import csv
import io
from datetime import datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from .emotions import DEFAULT_EMOTIONS
from .models import InstrumentSpec, Tag, Trade, TradingAccount


def at(day, hour=12):
    return datetime(2026, 10, day, hour, 0, tzinfo=dt_tz.utc)


class EmotionTests(TestCase):
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
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)

    def emotion(self, name):
        return Tag.objects.create(user=self.alice, name=name, kind="emotion")

    def trade(self, day, exit_price, tags):
        """1 lot from 1.10000 with the stop at 1.09500, so every trade risks 500."""
        closed = at(day)
        trade = Trade.objects.create(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D(exit_price), lots=D("1"),
            stop_loss=D("1.09500"),
        )
        trade.tags.set(tags)
        return trade

    def test_starter_emotions_are_seeded_once_and_only_when_asked_for(self):
        self.assertEqual(self.client.get("/api/tags/").json(), [])  # other lists never seed anything
        mine = self.client.get("/api/tags/", {"kind": "emotion"}).json()
        self.assertEqual(sorted(t["name"] for t in mine), sorted(DEFAULT_EMOTIONS))
        self.assertTrue(all(t["kind"] == "emotion" for t in mine))
        again = self.client.get("/api/tags/", {"kind": "emotion"}).json()
        self.assertEqual(len(again), len(DEFAULT_EMOTIONS))
        self.assertEqual(len(self.client.get("/api/tags/").json()), len(DEFAULT_EMOTIONS))
        self.assertEqual(self.client.get("/api/tags/", {"kind": "mistake"}).json(), [])

        bobs = self.bob_client.get("/api/tags/", {"kind": "emotion"}).json()
        self.assertEqual(len(bobs), len(DEFAULT_EMOTIONS))
        self.assertFalse({t["id"] for t in bobs} & {t["id"] for t in mine})

    def test_a_user_with_their_own_emotions_gets_no_starter_set(self):
        made = self.client.post("/api/tags/", {"name": "Zen", "kind": "emotion"}, format="json")
        self.assertEqual(made.status_code, 201)
        listed = self.client.get("/api/tags/", {"kind": "emotion"}).json()
        self.assertEqual([t["name"] for t in listed], ["Zen"])

    def test_emotion_tags_follow_the_normal_tag_rules(self):
        def post(**body):
            return self.client.post("/api/tags/", body, format="json")

        self.assertEqual(post(name="  Hopeful ", kind="emotion").json()["name"], "Hopeful")
        self.assertEqual(post(name="hopeful", kind="emotion").status_code, 400)
        self.assertEqual(post(name="Hopeful", kind="mistake").status_code, 201)
        self.assertEqual(post(name="x", kind="feeling").status_code, 400)

    def test_emotion_report_trade_filter_and_csv(self):
        calm, anxious = self.emotion("Calm"), self.emotion("Anxious")
        self.trade(15, "1.10500", [calm])             # +500, 1.00R
        self.trade(16, "1.09500", [anxious])          # -500, -1.00R
        self.trade(17, "1.10250", [anxious, calm])    # +250, 0.50R

        body = self.client.get("/api/reports/tags/", {"kind": "emotion"}).json()
        self.assertEqual(body["kind"], "emotion")
        first, second = body["tags"]
        self.assertEqual(
            (first["name"], first["trade_count"], first["wins"], first["win_rate"], first["net_pnl"],
             first["money_lost"], first["avg_r"]),
            ("Anxious", 2, 1, "50.00", "-250.00", "500.00", "-0.25"),
        )
        self.assertEqual(
            (second["name"], second["trade_count"], second["wins"], second["win_rate"], second["net_pnl"],
             second["money_lost"], second["avg_r"]),
            ("Calm", 2, 2, "100.00", "750.00", "0.00", "0.75"),
        )

        listed = self.client.get("/api/trades/", {"tag": anxious.id}).json()
        self.assertEqual(listed["count"], 2)
        self.assertIn({"id": anxious.id, "name": "Anxious", "kind": "emotion"}, listed["results"][0]["tags"])

        catalog = [c["slug"] for c in self.client.get("/api/reports/").json()]
        self.assertIn("emotions", catalog)
        res = self.client.get("/api/reports/emotions/csv/")
        self.assertEqual(res.status_code, 200)
        raw = b"".join(res.streaming_content).decode("utf-8-sig")
        rows = list(csv.DictReader(io.StringIO(raw, newline="")))
        self.assertEqual((rows[0]["name"], rows[0]["money_lost"]), ("Anxious", "500.00"))