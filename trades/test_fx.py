import io
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import fx
from .importer import build_plan, read_table, suggest_mapping
from .models import FxRate, InstrumentSpec, Trade, TradingAccount
from .pnl import PnLError
from .test_import import HEADER

JPY_ROW = "GBPJPY,sell,2026.10.15 10:00:00,2026.10.15 11:00:00,190.000,189.000,0.5,190.500,-3,0,330"
CLOSED = datetime(2026, 10, 15, 11, 0, tzinfo=dt_tz.utc)


class FxTests(TestCase):
    def setUp(self):
        fx._cooldowns.clear()
        self.user = get_user_model().objects.create_user("fx", "fx@example.com", "x")
        self.account = TradingAccount.objects.create(
            user=self.user, name="USD", currency="USD", starting_balance=D("10000")
        )
        for symbol, base, quote in (
            ("GBPJPY", "GBP", "JPY"), ("USDJPY", "USD", "JPY"), ("EURUSD", "EUR", "USD"),
        ):
            InstrumentSpec.objects.create(
                symbol=symbol, asset_class="forex", base_currency=base, quote_currency=quote,
                contract_size=D("100000"), pip_size=D("0.01"),
            )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def rate(self, day, value="150", base="USD", quote="JPY"):
        return FxRate.objects.create(
            base_currency=base, quote_currency=quote, date=day, rate=D(value)
        )

    def cross_trade(self, closed=CLOSED, **extra):
        """GBPJPY short, 0.5 lots, 190.000 -> 189.000: a 50,000 JPY gain."""
        data = dict(
            account=self.account, symbol="GBPJPY", side="short",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("190.000"), exit_price=D("189.000"), lots=D("0.5"),
        )
        data.update(extra)
        return Trade(**data)

    def post(self, **extra):
        body = {
            "account": self.account.id, "symbol": "GBPJPY", "side": "short",
            "opened_at": (CLOSED - timedelta(hours=1)).isoformat(), "closed_at": CLOSED.isoformat(),
            "entry_price": "190.000", "exit_price": "189.000", "lots": "0.5",
        }
        body.update(extra)
        return self.client.post("/api/trades/", body, format="json")

    # ---- the rate book ----

    def test_lookup_direction_window_and_misses(self):
        self.rate(date(2026, 10, 15), "150")
        book = fx.RateBook()

        found = book.lookup("JPY", "USD", date(2026, 10, 15))  # asked in reverse: 1 / 150
        self.assertEqual((found.rate, found.day), (D("0.00666667"), date(2026, 10, 15)))
        self.assertEqual(book.lookup("USD", "JPY", date(2026, 10, 15)).rate, D("150"))
        self.assertEqual(book.lookup("JPY", "USD", date(2026, 10, 17)).day, date(2026, 10, 15))  # a Saturday
        self.assertEqual(book.lookup("JPY", "USD", date(2026, 10, 22)).day, date(2026, 10, 15))  # 7 days back
        self.assertEqual(fx.RateBook().lookup("USD", "USD", date(2026, 10, 15)).rate, 1)

        self.assertIsNone(book.lookup("JPY", "USD", date(2026, 10, 23)))  # 8 days back is too old
        self.assertIsNone(book.lookup("JPY", "USD", date(2026, 10, 10)))  # before the first rate
        self.assertIsNone(book.lookup("CHF", "USD", date(2026, 10, 15)))
        self.assertEqual(len(book.misses), 3)

        self.rate(date(2026, 10, 16), "0.0067", base="JPY", quote="USD")
        fresher = fx.RateBook().lookup("JPY", "USD", date(2026, 10, 16))
        self.assertEqual((fresher.rate, fresher.day), (D("0.0067"), date(2026, 10, 16)))

    # ---- trades ----

    def test_cross_trade_gets_its_rate_and_pnl(self):
        self.rate(date(2026, 10, 15))
        trade = self.cross_trade(stop_loss=D("190.500"))
        trade.save()
        trade.refresh_from_db()
        self.assertEqual((trade.quote_to_account_rate, trade.rate_source), (D("0.00666667"), "auto"))
        self.assertEqual((trade.gross_pnl, trade.net_pnl), (D("333.33"), D("333.33")))
        self.assertEqual((trade.risk_amount, trade.r_multiple), (D("166.67"), D("2.00")))

    def test_non_cross_trades_need_no_rate(self):
        trade = Trade(
            account=self.account, symbol="EURUSD", side="long",
            opened_at=CLOSED - timedelta(hours=1), closed_at=CLOSED,
            entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
        )
        trade.save()
        self.assertEqual((trade.quote_to_account_rate, trade.rate_source, trade.net_pnl),
                         (None, "", D("500.00")))

    def test_missing_rate_is_a_clear_error(self):
        with self.assertRaises(PnLError) as caught:
            self.cross_trade().save()
        self.assertIn("JPY to USD", str(caught.exception))
        self.assertEqual(Trade.objects.count(), 0)

    def test_manual_rate_is_kept(self):
        self.rate(date(2026, 10, 15))
        trade = self.cross_trade(quote_to_account_rate=D("0.0066"))
        trade.save()
        self.assertEqual((trade.rate_source, trade.net_pnl), ("manual", D("330.00")))
        self.rate(date(2026, 10, 20), "160")
        trade.closed_at = datetime(2026, 10, 20, 11, 0, tzinfo=dt_tz.utc)
        trade.save()
        self.assertEqual((trade.quote_to_account_rate, trade.rate_source), (D("0.0066"), "manual"))

    def test_auto_rate_follows_the_close_date(self):
        self.rate(date(2026, 10, 15))
        self.rate(date(2026, 10, 20), "160")
        trade = self.cross_trade()
        trade.save()
        self.assertEqual(trade.quote_to_account_rate, D("0.00666667"))
        trade.closed_at = datetime(2026, 10, 20, 11, 0, tzinfo=dt_tz.utc)
        trade.opened_at = trade.closed_at - timedelta(hours=1)
        trade.save()
        self.assertEqual((trade.quote_to_account_rate, trade.rate_source), (D("0.00625000"), "auto"))
        self.assertEqual(trade.net_pnl, D("312.50"))

    def test_api_auto_rate_override_and_reset(self):
        self.rate(date(2026, 10, 15))
        made = self.post().json()
        self.assertEqual(
            (made["quote_to_account_rate"], made["rate_source"], made["net_pnl"]),
            ("0.00666667", "auto", "333.33"),
        )
        url = f"/api/trades/{made['id']}/"
        manual = self.client.patch(url, {"quote_to_account_rate": "0.0070"}, format="json").json()
        self.assertEqual(
            (manual["quote_to_account_rate"], manual["rate_source"], manual["net_pnl"]),
            ("0.00700000", "manual", "350.00"),
        )
        back = self.client.patch(url, {"quote_to_account_rate": None}, format="json").json()
        self.assertEqual((back["quote_to_account_rate"], back["rate_source"]), ("0.00666667", "auto"))

    def test_api_missing_rate_is_a_400(self):
        res = self.post()
        self.assertEqual(res.status_code, 400)
        self.assertIn("JPY to USD", res.json()["detail"])
        self.assertEqual(Trade.objects.count(), 0)

    # ---- downloading ----

    @mock.patch("trades.fx._http_get_json")
    def test_range_download_is_idempotent_and_skips_bad_rows(self, fake):
        fake.return_value = {"base": "USD", "rates": {
            "2026-10-12": {"JPY": 150.5}, "2026-10-13": {"JPY": 151.0},
            "2026-10-14": {"EUR": 0.9}, "not-a-date": {"JPY": 1},
        }}
        stored = fx.fetch_range("USD", "JPY", date(2026, 10, 12), date(2026, 10, 16))
        self.assertEqual(stored, 2)
        url = fake.call_args.args[0]
        for part in ("2026-10-12..2026-10-16", "base=USD", "symbols=JPY"):
            self.assertIn(part, url)

        fake.return_value = {"rates": {"2026-10-12": {"JPY": 152}}}
        fx.fetch_range("USD", "JPY", date(2026, 10, 12), date(2026, 10, 16))
        self.assertEqual(FxRate.objects.count(), 2)
        self.assertEqual(FxRate.objects.get(date=date(2026, 10, 12)).rate, D("152"))

    @mock.patch("trades.fx._http_get_json")
    def test_single_day_and_long_ranges(self, fake):
        fake.return_value = {"base": "USD", "date": "2026-10-15", "rates": {"JPY": 150}}
        fx.fetch_range("USD", "JPY", date(2026, 10, 15), date(2026, 10, 15))
        self.assertIn("/v1/2026-10-15?", fake.call_args.args[0])
        self.assertTrue(FxRate.objects.filter(date=date(2026, 10, 15)).exists())

        fake.reset_mock()
        fake.return_value = {"rates": {}}
        fx.fetch_range("USD", "JPY", date(2026, 1, 1), date(2026, 5, 10))  # 130 days: 60 + 60 + 10
        self.assertEqual(fake.call_count, 3)

    def test_only_real_currency_pairs_are_fetched(self):
        for base, symbol in (("US", "JPY"), ("USD", "J1Y"), ("USD", "USD"), ("usd", "JPY")):
            with self.assertRaises(fx.FxFetchError):
                fx.fetch_range(base, symbol, date(2026, 10, 1), date(2026, 10, 2))

    @override_settings(FX_AUTO_FETCH=True)
    @mock.patch("trades.fx._http_get_json")
    def test_missing_rate_is_downloaded_once(self, fake):
        fake.return_value = {"base": "USD", "rates": {"2026-10-15": {"JPY": 150.0}}}
        first = self.cross_trade()
        first.save()
        self.assertEqual((first.quote_to_account_rate, first.rate_source), (D("0.00666667"), "auto"))
        self.assertEqual(fake.call_count, 1)
        self.assertIn("2026-10-08..2026-10-15", fake.call_args.args[0])

        self.cross_trade().save()  # same day again: served from the table
        self.assertEqual(fake.call_count, 1)

    @override_settings(FX_AUTO_FETCH=True)
    @mock.patch("trades.fx._http_get_json", side_effect=fx.FxFetchError("down"))
    def test_provider_outage_is_a_clear_error_and_backs_off(self, fake):
        for _ in range(2):
            with self.assertRaises(PnLError):
                self.cross_trade().save()
        self.assertEqual(fake.call_count, 1)  # the second save did not wait on the provider again

    # ---- imports ----

    def plan(self):
        text = "\n".join((HEADER, JPY_ROW))
        headers, rows = read_table(io.BytesIO(text.encode()), "t.csv")
        return build_plan(self.account, headers, rows, suggest_mapping(headers))

    def test_import_uses_cached_rates_for_r(self):
        self.rate(date(2026, 10, 15))
        plan = self.plan()
        trade = plan.trades[0]
        self.assertEqual(plan.errors, [])
        self.assertEqual((trade.rate_source, trade.quote_to_account_rate), ("auto", D("0.00666667")))
        self.assertEqual((trade.net_pnl, trade.r_multiple), (D("327.00"), D("1.96")))  # the broker's 330 still rules

    @override_settings(FX_AUTO_FETCH=True)
    @mock.patch("trades.fx._http_get_json")
    def test_import_downloads_missing_rates_once(self, fake):
        fake.return_value = {"base": "USD", "rates": {"2026-10-15": {"JPY": 150.0}}}
        plan = self.plan()
        self.assertEqual(fake.call_count, 1)
        self.assertEqual((plan.trades[0].rate_source, plan.trades[0].r_multiple), ("auto", D("1.96")))

    def test_recompute_picks_up_rates_that_arrived_later(self):
        trade = self.cross_trade(stop_loss=D("190.500"), broker_profit=D("330.00"))
        trade.save()
        self.assertEqual((trade.quote_to_account_rate, trade.r_multiple), (None, None))
        self.rate(date(2026, 10, 15))
        call_command("recompute_trades", stdout=io.StringIO())
        trade.refresh_from_db()
        self.assertEqual(
            (trade.quote_to_account_rate, trade.rate_source, trade.r_multiple),
            (D("0.00666667"), "auto", D("1.98")),
        )

    # ---- the endpoint and the command ----

    def test_rate_endpoint(self):
        self.rate(date(2026, 10, 15))
        res = self.client.get("/api/fx/rate/", {"from": "jpy", "to": "usd", "date": "2026-10-17"})
        self.assertEqual(res.json(), {
            "from": "JPY", "to": "USD", "date": "2026-10-17",
            "rate": "0.00666667", "rate_date": "2026-10-15",
        })
        same = self.client.get("/api/fx/rate/", {"from": "USD", "to": "USD", "date": "2026-10-17"}).json()
        self.assertEqual((same["rate"], same["rate_date"]), ("1.00000000", "2026-10-17"))
        none = self.client.get("/api/fx/rate/", {"from": "CHF", "to": "USD", "date": "2026-10-17"}).json()
        self.assertEqual((none["rate"], none["rate_date"]), (None, None))
        for params in ({"from": "JP", "to": "USD"}, {"from": "JPY"},
                       {"from": "JPY", "to": "USD", "date": "soon"}):
            self.assertEqual(self.client.get("/api/fx/rate/", params).status_code, 400, params)
        self.assertEqual(APIClient().get("/api/fx/rate/", {"from": "JPY", "to": "USD"}).status_code, 401)

    @mock.patch("trades.fx._http_get_json")
    def test_fetch_command_downloads_only_the_pairs_needed(self, fake):
        fake.return_value = {"base": "USD", "rates": {"2026-10-14": {"JPY": 150.0}}}
        call_command("fetch_fx_rates", stdout=io.StringIO())
        urls = [call.args[0] for call in fake.call_args_list]
        self.assertTrue(urls)
        self.assertTrue(all("base=USD" in u and "symbols=JPY" in u for u in urls))
        self.assertTrue(FxRate.objects.filter(
            base_currency="USD", quote_currency="JPY", date=date(2026, 10, 14)
        ).exists())

    @mock.patch("trades.fx._http_get_json", side_effect=fx.FxFetchError("down"))
    def test_fetch_command_survives_provider_errors(self, fake):
        err = io.StringIO()
        call_command("fetch_fx_rates", stdout=io.StringIO(), stderr=err)
        self.assertIn("USD/JPY", err.getvalue())