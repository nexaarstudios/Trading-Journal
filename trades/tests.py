from datetime import timedelta
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from .models import InstrumentSpec, Trade, TradingAccount
from .pnl import PnLError, compute_pnl


def calc(**kw):
    defaults = dict(account_currency="USD", contract_size=D("100000"), lots=D("1"))
    defaults.update(kw)
    return compute_pnl(**defaults)


class PnLEngineTests(SimpleTestCase):
    def test_eurusd_long_win_with_commission_and_r(self):
        r = calc(side="long", entry=D("1.10000"), exit_price=D("1.10500"),
                 base_currency="EUR", quote_currency="USD",
                 commission=D("7"), stop=D("1.09500"))
        self.assertEqual(r.gross, D("500.00"))
        self.assertEqual(r.net, D("493.00"))
        self.assertEqual(r.risk, D("500.00"))
        self.assertEqual(r.r_multiple, D("0.99"))

    def test_eurusd_short_loss(self):
        r = calc(side="short", entry=D("1.10000"), exit_price=D("1.10500"),
                 base_currency="EUR", quote_currency="USD")
        self.assertEqual(r.gross, D("-500.00"))
        self.assertEqual(r.net, D("-500.00"))
        self.assertIsNone(r.r_multiple)

    def test_usdjpy_divides_by_exit_price(self):
        r = calc(side="long", entry=D("150.00"), exit_price=D("151.00"),
                 base_currency="USD", quote_currency="JPY")
        self.assertEqual(r.gross, D("662.25"))

    def test_gbpjpy_cross_uses_stored_rate(self):
        r = calc(side="short", lots=D("0.5"), entry=D("190.00"), exit_price=D("189.00"),
                 base_currency="GBP", quote_currency="JPY",
                 quote_to_account_rate=D("0.0066"))
        self.assertEqual(r.gross, D("330.00"))

    def test_cross_without_rate_raises_clear_error(self):
        with self.assertRaises(PnLError):
            calc(side="long", entry=D("190.00"), exit_price=D("191.00"),
                 base_currency="GBP", quote_currency="JPY")

    def test_gold_contract_size_100(self):
        r = calc(side="long", contract_size=D("100"), entry=D("2000.00"),
                 exit_price=D("2010.50"), base_currency="XAU", quote_currency="USD")
        self.assertEqual(r.gross, D("1050.00"))

    def test_broker_profit_overrides_computed_gross(self):
        r = calc(side="long", entry=D("1.10000"), exit_price=D("1.10500"),
                 base_currency="EUR", quote_currency="USD",
                 broker_profit=D("480.00"), commission=D("7"))
        self.assertEqual(r.gross, D("480.00"))
        self.assertEqual(r.net, D("473.00"))

    def test_swap_credit_increases_net(self):
        r = calc(side="long", entry=D("1.10000"), exit_price=D("1.10500"),
                 base_currency="EUR", quote_currency="USD",
                 commission=D("7"), swap=D("-2"), fees=D("1"))
        self.assertEqual(r.net, D("494.00"))


class TradeModelTests(TestCase):
    def setUp(self):
        user = get_user_model().objects.create_user(
            username="tester", email="tester@example.com", password="x"
        )
        self.account = TradingAccount.objects.create(
            user=user, name="Test", starting_balance=D("10000"), currency="USD"
        )
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )

    def _trade(self, symbol="eurusd"):
        opened = timezone.now()
        return Trade(
            account=self.account, symbol=symbol, side="long",
            opened_at=opened, closed_at=opened + timedelta(hours=1),
            entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
            stop_loss=D("1.09500"), commission=D("7"),
        )

    def test_save_computes_pnl_fields(self):
        trade = self._trade()
        trade.save()
        trade.refresh_from_db()
        self.assertEqual(trade.symbol, "EURUSD")
        self.assertEqual(trade.gross_pnl, D("500.00"))
        self.assertEqual(trade.net_pnl, D("493.00"))
        self.assertEqual(trade.r_multiple, D("0.99"))
        self.assertEqual(trade.duration_seconds, 3600)

    def test_unknown_symbol_fails_validation(self):
        with self.assertRaises(ValidationError):
            self._trade(symbol="FOOBAR").clean()