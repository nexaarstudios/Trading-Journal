import json
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from .models import ImportBatch, InstrumentSpec, Trade, TradingAccount
from .test_import import EUR_ROW, HEADER

SECOND_ROW = "EURUSD,sell,2026.10.16 09:00:00,2026.10.16 10:00:00,1.10500,1.10000,1,,-7,0,500"


def csv_file(*rows, name="trades.csv"):
    text = "\n".join((HEADER, *rows))
    return SimpleUploadedFile(name, text.encode(), content_type="text/csv")


class ImportAPITests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.alice = User.objects.create_user("alice", "alice@example.com", "x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "x")
        self.account = TradingAccount.objects.create(user=self.alice, name="A", currency="USD")
        self.bob_account = TradingAccount.objects.create(user=self.bob, name="B", currency="USD")
        InstrumentSpec.objects.create(
            symbol="EURUSD", asset_class="forex", base_currency="EUR",
            quote_currency="USD", contract_size=D("100000"), pip_size=D("0.0001"),
        )
        self.client = APIClient()
        self.client.force_authenticate(self.alice)

    def post(self, url, file, **fields):
        data = {"file": file, "account": self.account.id, **fields}
        return self.client.post(url, data, format="multipart")

    def test_preview_reports_but_saves_nothing(self):
        res = self.post("/api/imports/preview/", csv_file(EUR_ROW, EUR_ROW))
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual((body["importable"], body["duplicates"]), (1, 1))
        self.assertEqual(body["preview"][0]["net_pnl"], "493.00")
        self.assertEqual(body["mapping"]["symbol"], "Symbol")
        self.assertEqual(Trade.objects.count(), 0)
        self.assertEqual(ImportBatch.objects.count(), 0)

    def test_commit_then_same_file_again_is_all_duplicates(self):
        res = self.post("/api/imports/", csv_file(EUR_ROW))
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["batch"]["imported_count"], 1)
        self.assertEqual(Trade.objects.count(), 1)

        again = self.post("/api/imports/", csv_file(EUR_ROW))
        self.assertEqual(again.status_code, 400)
        self.assertEqual(again.json()["duplicates"], 1)
        self.assertEqual(Trade.objects.count(), 1)

    def test_saved_template_is_reused(self):
        first = self.post(
            "/api/imports/", csv_file(EUR_ROW),
            timezone="Africa/Lagos", save_template_name="MT4 Lagos",
        )
        self.assertEqual(first.status_code, 201)
        template_id = first.json()["template_id"]

        second = self.post("/api/imports/preview/", csv_file(SECOND_ROW), template=template_id)
        self.assertEqual(second.status_code, 200)
        body = second.json()
        self.assertEqual(body["timezone"], "Africa/Lagos")
        self.assertEqual(body["importable"], 1)
        self.assertEqual(body["preview"][0]["opened_at"], "2026-10-16T08:00:00Z")  # 09:00 Lagos

        listed = self.client.get("/api/import-templates/").json()
        self.assertEqual(listed[0]["name"], "MT4 Lagos")

    def test_cannot_import_into_another_users_account(self):
        data = {"file": csv_file(EUR_ROW), "account": self.bob_account.id}
        res = self.client.post("/api/imports/", data, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Trade.objects.count(), 0)

    def test_undo_and_ownership(self):
        batch_id = self.post("/api/imports/", csv_file(EUR_ROW)).json()["batch"]["id"]

        bob = APIClient()
        bob.force_authenticate(self.bob)
        self.assertEqual(bob.post(f"/api/imports/{batch_id}/undo/").status_code, 404)
        self.assertEqual(Trade.objects.count(), 1)

        res = self.client.post(f"/api/imports/{batch_id}/undo/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["removed"], 1)
        self.assertEqual(Trade.objects.count(), 0)
        self.assertEqual(self.client.post(f"/api/imports/{batch_id}/undo/").status_code, 400)

    def test_wrong_file_type_rejected(self):
        bad = SimpleUploadedFile("notes.pdf", b"%PDF-1.4", content_type="application/pdf")
        res = self.post("/api/imports/preview/", bad)
        self.assertEqual(res.status_code, 400)

    def test_bad_mapping_rejected(self):
        complete = {
            "symbol": "Symbol", "side": "Type", "opened_at": "Open Time", "closed_at": "Close Time",
            "entry_price": "Open Price", "exit_price": "Close Price", "lots": "Size",
        }
        res = self.post(
            "/api/imports/preview/", csv_file(EUR_ROW),
            mapping=json.dumps({**complete, "symbol": "Nope"}),
        )
        self.assertEqual(res.status_code, 400)