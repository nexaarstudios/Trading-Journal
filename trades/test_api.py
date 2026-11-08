from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import InstrumentSpec, Trade, TradingAccount


class TradeAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.alice_acc = TradingAccount.objects.create(user=self.alice, name="A", currency="USD")
        self.bob_acc = TradingAccount.objects.create(user=self.bob, name="B", currency="USD")
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        now = timezone.now()
        self.trade = Trade.objects.create(
            account=self.alice_acc, symbol="EURUSD", side="long",
            opened_at=now, closed_at=now + timedelta(hours=1),
            entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
        )
        self.client = APIClient()

    def payload(self, account_id):
        now = timezone.now()
        return {
            "account": account_id, "symbol": "eurusd", "side": "long",
            "opened_at": now.isoformat(),
            "closed_at": (now + timedelta(hours=1)).isoformat(),
            "entry_price": "1.10000", "exit_price": "1.10500",
            "lots": "1", "commission": "7",
        }

    def test_requires_login(self):
        self.assertEqual(self.client.get("/api/trades/").status_code, 401)

    def test_user_only_sees_own_trades(self):
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get("/api/trades/").json()["count"], 0)
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.get("/api/trades/").json()["count"], 1)

    def test_cannot_read_another_users_trade(self):
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.get(f"/api/trades/{self.trade.id}/").status_code, 404)

    def test_cannot_create_trade_on_another_users_account(self):
        self.client.force_authenticate(self.bob)
        res = self.client.post("/api/trades/", self.payload(self.alice_acc.id), format="json")
        self.assertEqual(res.status_code, 400)

    def test_create_computes_pnl(self):
        self.client.force_authenticate(self.alice)
        res = self.client.post("/api/trades/", self.payload(self.alice_acc.id), format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["net_pnl"], "493.00")
        self.assertEqual(res.json()["symbol"], "EURUSD")