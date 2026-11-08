import csv
import io
from datetime import date, datetime, timedelta, timezone as dt_tz
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from rest_framework.test import APIClient

from .models import (
    DailyRollup, ImportTemplate, InstrumentSpec, JournalEntry, JournalTemplate,
    Playbook, PlaybookRule, RiskRule, Tag, Trade, TradingAccount,
)


def utc(day, hour=12):
    return datetime(2026, 10, day, hour, 0, tzinfo=dt_tz.utc)


class DataTests(TestCase):
    PASSWORD = "pass-12345"
    TRADES = "/api/export/trades/"
    JOURNAL = "/api/export/journal/"
    DELETE = "/api/data/delete/"

    def setUp(self):
        cache.clear()  # throttle counters live in the cache
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", self.PASSWORD)
        self.bob = User.objects.create_user("bob", "bob@example.com", self.PASSWORD)
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)
        self.alice_account, self.alice_trade = self.fill(self.alice)
        self.bob_account, self.bob_trade = self.fill(self.bob, tag="Revenge")

    def fill(self, user, tag="FOMO"):
        """One of everything a user can own, including a trade with ticked playbook rules."""
        account = TradingAccount.objects.create(
            user=user, name="Main", currency="USD", starting_balance=D("10000")
        )
        tag_obj = Tag.objects.create(user=user, name=tag, kind="mistake")
        playbook = Playbook.objects.create(user=user, name="Breakout")
        r1 = PlaybookRule.objects.create(playbook=playbook, text="Retest", position=1)
        PlaybookRule.objects.create(playbook=playbook, text="Stop placed", position=2)
        closed = utc(15)
        trade = Trade.objects.create(
            account=account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
            stop_loss=D("1.09500"), commission=D("7"),
            playbook=playbook, rating=4, notes="Clean entry",
        )
        trade.tags.set([tag_obj])
        trade.sync_rules([r1.id])
        template = JournalTemplate.objects.create(
            user=user, kind="post_market", name="Review", prompts=["How did it go?"]
        )
        JournalEntry.objects.create(
            user=user, kind="post_market", date=date(2026, 10, 15), template=template,
            sections=[{"prompt": "How did it go?", "answer": "Fine"}],
        )
        RiskRule.objects.create(account=account, kind="daily_loss", unit="money", limit_value=D("500"))
        ImportTemplate.objects.create(user=user, name="MT", mapping={})
        return account, trade

    def loser(self, **extra):
        closed = utc(16)
        return Trade.objects.create(
            account=self.alice_account, symbol="EURUSD", side="long",
            opened_at=closed - timedelta(hours=1), closed_at=closed,
            entry_price=D("1.10000"), exit_price=D("1.09750"), lots=D("1"), **extra,
        )

    def read(self, response):
        raw = b"".join(response.streaming_content)
        reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
        rows = list(reader)
        return reader.fieldnames, rows, raw

    # ---- trade export ----

    def test_trade_export_contents(self):
        res = self.client.get(self.TRADES)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res["Content-Type"].startswith("text/csv"))
        self.assertRegex(
            res["Content-Disposition"], r'^attachment; filename="trades-\d{4}-\d{2}-\d{2}\.csv"$'
        )
        header, rows, raw = self.read(res)
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(header[:6], ["id", "account", "currency", "symbol", "side", "opened_at_utc"])
        self.assertEqual(len(rows), 1)  # bob's trade is not in alice's export

        row = rows[0]
        self.assertEqual((row["account"], row["currency"], row["symbol"], row["side"]),
                         ("Main", "USD", "EURUSD", "long"))
        self.assertEqual(row["closed_at_utc"], "2026-10-15T12:00:00+00:00")
        self.assertEqual(row["closed_date_local"], "2026-10-15")
        self.assertEqual((row["entry_price"], row["lots"], row["stop_loss"], row["take_profit"]),
                         ("1.100000", "1.0000", "1.095000", ""))
        self.assertEqual((row["commission"], row["net_pnl"], row["r_multiple"]),
                         ("7.00", "493.00", "0.99"))
        self.assertEqual((row["playbook"], row["rules_checked"], row["rules_total"], row["rating"]),
                         ("Breakout", "1", "2", "4"))
        self.assertEqual((row["tags"], row["notes"]), ("mistake:FOMO", "Clean entry"))

    def test_local_date_follows_the_user_timezone(self):
        self.alice.timezone = "Africa/Lagos"  # UTC+1
        self.alice.save()
        Trade.objects.filter(pk=self.alice_trade.pk).update(closed_at=utc(15, 23))
        _, rows, _ = self.read(self.client.get(self.TRADES))
        self.assertEqual(rows[0]["closed_at_utc"], "2026-10-15T23:00:00+00:00")
        self.assertEqual(rows[0]["closed_date_local"], "2026-10-16")

    def test_trade_export_filters(self):
        self.loser()

        def count(**params):
            return len(self.read(self.client.get(self.TRADES, params))[1])

        tag = Tag.objects.get(user=self.alice)
        self.assertEqual(count(), 2)
        self.assertEqual(count(outcome="loss"), 1)
        self.assertEqual(count(**{"from": "2026-10-16"}), 1)
        self.assertEqual(count(**{"to": "2026-10-15"}), 1)
        self.assertEqual(count(symbol="GBPUSD"), 0)
        self.assertEqual(count(tag=tag.id), 1)
        self.assertEqual(count(account=self.alice_account.id), 2)
        self.assertEqual(count(account=self.bob_account.id), 0)
        for params in ({"from": "2026-13-45"}, {"outcome": "huge"}, {"tag": "abc"}):
            self.assertEqual(self.client.get(self.TRADES, params).status_code, 400, params)

    def test_text_cells_cannot_run_as_formulas(self):
        self.loser(notes="=SUM(A1)")
        Playbook.objects.filter(user=self.alice).update(name="@cmd")
        TradingAccount.objects.filter(pk=self.alice_account.pk).update(name="+bad")
        _, rows, _ = self.read(self.client.get(self.TRADES))
        winner, loss = rows
        self.assertEqual((winner["account"], winner["playbook"]), ("'+bad", "'@cmd"))
        self.assertEqual(loss["notes"], "'=SUM(A1)")
        self.assertEqual(loss["net_pnl"], "-250.00")  # numbers keep their minus sign

    # ---- journal export ----

    def test_journal_export(self):
        JournalEntry.objects.create(
            user=self.alice, kind="pre_market", date=date(2026, 10, 16),
            sections=[
                {"prompt": "Plan?", "answer": 'Said "no", twice\nthen stopped'},
                {"prompt": "=Bad prompt", "answer": "=1+1"},
            ],
        )
        JournalEntry.objects.create(user=self.alice, kind="weekly", date=date(2026, 10, 12), sections=[])

        header, rows, _ = self.read(self.client.get(self.JOURNAL))
        self.assertEqual(header, ["date", "kind", "section", "prompt", "answer"])
        self.assertEqual(
            [(r["date"], r["kind"], r["section"]) for r in rows],
            [("2026-10-12", "weekly", ""), ("2026-10-15", "post_market", "1"),
             ("2026-10-16", "pre_market", "1"), ("2026-10-16", "pre_market", "2")],
        )
        self.assertEqual(rows[2]["answer"], 'Said "no", twice\nthen stopped')  # quotes and newlines survive
        self.assertEqual((rows[3]["prompt"], rows[3]["answer"]), ("'=Bad prompt", "'=1+1"))

        def count(**params):
            return len(self.read(self.client.get(self.JOURNAL, params))[1])

        self.assertEqual(count(kind="weekly"), 1)
        self.assertEqual(count(**{"from": "2026-10-16"}), 2)
        self.assertEqual(self.client.get(self.JOURNAL, {"kind": "diary"}).status_code, 400)
        self.assertEqual(self.client.get(self.JOURNAL, {"from": "soon"}).status_code, 400)

    def test_everything_requires_login(self):
        anon = APIClient()
        self.assertEqual(anon.get(self.TRADES).status_code, 401)
        self.assertEqual(anon.get(self.JOURNAL).status_code, 401)
        body = {"password": self.PASSWORD, "scope": "data", "confirm": "DELETE"}
        self.assertEqual(anon.post(self.DELETE, body, format="json").status_code, 401)

    # ---- deleting data ----

    def test_wipe_data_keeps_the_login_and_other_users(self):
        for day in range(1, 26):  # plenty of trades, not just one
            closed = utc(day, 8)
            Trade.objects.create(
                account=self.alice_account, symbol="EURUSD", side="long",
                opened_at=closed - timedelta(hours=1), closed_at=closed,
                entry_price=D("1.10000"), exit_price=D("1.10500"), lots=D("1"),
            )
        self.assertTrue(DailyRollup.objects.filter(account=self.alice_account).exists())

        body = {"password": self.PASSWORD, "scope": "data", "confirm": "DELETE"}
        res = self.client.post(self.DELETE, body, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"scope": "data", "deleted": {
            "accounts": 1, "trades": 26, "tags": 1, "playbooks": 1,
            "journal_entries": 1, "journal_templates": 1, "import_templates": 1,
            "goals": 0, "screenshots": 0,
        }})

        self.assertEqual(TradingAccount.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(Trade.objects.filter(account=self.alice_account).count(), 0)
        self.assertEqual(DailyRollup.objects.filter(account=self.alice_account).count(), 0)
        self.assertEqual(RiskRule.objects.filter(account=self.alice_account).count(), 0)
        self.assertEqual(Tag.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(Playbook.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(PlaybookRule.objects.filter(playbook__user=self.alice).count(), 0)
        self.assertEqual(JournalEntry.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(JournalTemplate.objects.filter(user=self.alice).count(), 0)
        self.assertEqual(ImportTemplate.objects.filter(user=self.alice).count(), 0)

        # Bob is untouched
        self.assertEqual(Trade.objects.filter(account=self.bob_account).count(), 1)
        self.assertEqual(DailyRollup.objects.filter(account=self.bob_account).count(), 1)
        self.assertEqual(Playbook.objects.filter(user=self.bob).count(), 1)
        self.assertEqual(JournalEntry.objects.filter(user=self.bob).count(), 1)

        # Alice can still sign in and start again
        self.assertTrue(get_user_model().objects.filter(pk=self.alice.pk).exists())
        self.assertEqual(self.client.get("/api/accounts/").json()["count"], 0)

    def test_wrong_password_or_confirmation_deletes_nothing(self):
        cases = (
            {"password": "wrong", "scope": "data", "confirm": "DELETE"},
            {"password": self.PASSWORD, "scope": "data", "confirm": "delete"},
            {"password": self.PASSWORD, "scope": "everything", "confirm": "DELETE"},
            {"scope": "data", "confirm": "DELETE"},
            {"password": self.PASSWORD, "scope": "account"},
        )
        for body in cases:
            self.assertEqual(self.client.post(self.DELETE, body, format="json").status_code, 400, body)
        self.assertTrue(Trade.objects.filter(account=self.alice_account).exists())
        self.assertTrue(get_user_model().objects.filter(pk=self.alice.pk).exists())

    def test_deleting_the_account_removes_the_login_and_its_tokens(self):
        password = "a-Str0ng-pass!"
        get_user_model().objects.create_user(
            "gone@example.com", "gone@example.com", password, email_verified=True
        )
        login = APIClient().post(
            "/api/auth/login/", {"username": "gone@example.com", "password": password}, format="json"
        ).json()
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {login['access']}")
        made = client.post(
            "/api/accounts/", {"name": "X", "starting_balance": "100", "currency": "USD"}, format="json"
        )
        self.assertEqual(made.status_code, 201)

        res = client.post(
            self.DELETE, {"password": password, "scope": "account", "confirm": "DELETE"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["deleted"]["accounts"], 1)
        self.assertFalse(get_user_model().objects.filter(email="gone@example.com").exists())
        self.assertEqual(client.get("/api/trades/").status_code, 401)  # the old token is dead
        again = APIClient().post(
            "/api/auth/login/", {"username": "gone@example.com", "password": password}, format="json"
        )
        self.assertEqual(again.status_code, 401)
        self.assertEqual(TradingAccount.objects.filter(user=self.alice).count(), 1)  # others unaffected