import io
from datetime import datetime
from decimal import Decimal as D

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from openpyxl import Workbook
from rest_framework.test import APIClient

from .importer import (
    RowError, _decimal, build_plan, read_table, suggest_decimal_separator, suggest_mapping,
)
from .models import ImportTemplate, InstrumentSpec, Trade, TradingAccount

COLUMNS = [
    "Symbol", "Type", "Open Time", "Close Time", "Open Price", "Close Price",
    "Size", "S / L", "Commission", "Swap", "Profit",
]
ROW = ["EURUSD", "buy", "2026.10.15 14:00:00", "2026.10.15 16:00:00",
       "1.10000", "1.10500", "1", "1.09500", "-7", "0", "500"]
EU_ROW = ["EURUSD", "buy", "2026.10.15 14:00:00", "2026.10.15 16:00:00",
          "1,10000", "1,10500", "1", "1,09500", "-7", "0", "500"]


def table(delimiter, row=ROW):
    return "\n".join(delimiter.join(line) for line in (COLUMNS, row))


class DecimalRuleTests(SimpleTestCase):
    def test_point_mode_and_guards(self):
        cases = {
            "1234.5": "1234.50", "1,234.50": "1234.50", "1,234,567": "1234567.00",
            "-7": "-7.00", "+7": "7.00", "1 234.5": "1234.50",
        }
        for raw, expected in cases.items():
            self.assertEqual(_decimal(raw, 2, "x"), D(expected), raw)
        for bad in ("1,10500", "1,5", "1,2345,6"):
            with self.assertRaises(RowError):
                _decimal(bad, 2, "x")
        self.assertIsNone(_decimal("  ", 2, "x"))
        with self.assertRaises(RowError):
            _decimal("", 2, "x", required=True)
        with self.assertRaises(RowError):
            _decimal("abc", 2, "x")

    def test_comma_mode_guards_and_typed_numbers(self):
        cases = {"1,5": "1.50", "1.234,50": "1234.50", "1 234,5": "1234.50", "-0,25": "-0.25", "7": "7.00"}
        for raw, expected in cases.items():
            self.assertEqual(_decimal(raw, 2, "x", sep=","), D(expected), raw)
        for bad in ("1.10500", "1.5", "1,234.50"):
            with self.assertRaises(RowError):
                _decimal(bad, 2, "x", sep=",")
        for value in (1.105, 5, D("1.105")):  # real numbers from Excel ignore the setting
            self.assertEqual(
                _decimal(value, 3, "x", sep=","), D(str(value)).quantize(D("0.001")), value
            )

    def test_suggestions(self):
        def suggest(*values):
            return suggest_decimal_separator([{"P": v} for v in values], {"entry_price": "P"})

        self.assertEqual(suggest("1,10500", "1,2"), ",")
        self.assertEqual(suggest("1.10500"), ".")
        self.assertEqual(suggest("1.234,56"), ",")
        self.assertEqual(suggest("1,234.56"), ".")
        self.assertEqual(suggest("1,234", "5,000"), ".")  # could be thousands: stay on the default
        self.assertEqual(suggest("1,5", "1.5"), ".")      # mixed evidence: stay on the default
        self.assertEqual(suggest(1.5, 2), ".")            # real numbers carry no evidence
        self.assertEqual(suggest_decimal_separator([{"X": "1,5"}], {"entry_price": "P"}), ".")


class PasteTests(TestCase):
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

    def post(self, url, **fields):
        return self.client.post(url, {"account": self.account.id, **fields}, format="json")

    def test_tab_paste_preview_commit_and_duplicates(self):
        text = table("\t")
        preview = self.post("/api/imports/preview/", text=text)
        self.assertEqual(preview.status_code, 200)
        body = preview.json()
        self.assertEqual(
            (body["filename"], body["needs_mapping"], body["importable"]), ("Pasted rows", False, 1)
        )
        self.assertEqual(body["preview"][0]["net_pnl"], "493.00")
        self.assertEqual(Trade.objects.count(), 0)

        done = self.post("/api/imports/", text=text)
        self.assertEqual(done.status_code, 201)
        self.assertEqual(done.json()["batch"]["filename"], "Pasted rows")
        self.assertEqual(Trade.objects.count(), 1)

        again = self.post("/api/imports/", text=text)
        self.assertEqual((again.status_code, again.json()["duplicates"]), (400, 1))

    def test_other_separators_and_windows_newlines(self):
        variants = {
            "comma": table(","), "semicolon": table(";"), "pipe": table("|"),
            "spaces": table("  "), "windows": table("\t").replace("\n", "\r\n"),
        }
        for name, text in variants.items():
            body = self.post("/api/imports/preview/", text=text).json()
            self.assertEqual((body["importable"], body["error_count"]), (1, 0), name)

    def test_headerless_paste(self):
        row = "\t".join(ROW)
        mapping = {
            "symbol": "Column 1", "side": "Column 2", "opened_at": "Column 3",
            "closed_at": "Column 4", "entry_price": "Column 5", "exit_price": "Column 6",
            "lots": "Column 7", "stop_loss": "Column 8", "commission": "Column 9",
            "swap": "Column 10", "broker_profit": "Column 11",
        }
        ask = self.post("/api/imports/preview/", text=row, has_header=False)
        self.assertEqual(ask.status_code, 200)
        self.assertTrue(ask.json()["needs_mapping"])
        self.assertEqual(ask.json()["headers"][:2], ["Column 1", "Column 2"])

        mapped = self.post(
            "/api/imports/preview/", text=row, has_header=False, mapping=mapping
        ).json()
        self.assertEqual((mapped["importable"], mapped["preview"][0]["net_pnl"]), (1, "493.00"))
        # A single row taken as the header leaves nothing to import
        self.assertEqual(self.post("/api/imports/preview/", text=row).status_code, 400)

    def test_unmapped_columns_ask_for_a_mapping(self):
        text = (
            "Pair\tDir\tStart\tEnd\tIn\tOut\tQty\n"
            "EURUSD\tbuy\t2026.10.15 14:00:00\t2026.10.15 16:00:00\t1.1\t1.105\t1"
        )
        ask = self.post("/api/imports/preview/", text=text).json()
        self.assertTrue(ask["needs_mapping"])
        self.assertEqual(ask["mapping"], {"symbol": "Pair", "lots": "Qty"})
        self.assertEqual(
            sorted(ask["missing_fields"]),
            ["closed_at", "entry_price", "exit_price", "opened_at", "side"],
        )
        self.assertEqual(ask["headers"], ["Pair", "Dir", "Start", "End", "In", "Out", "Qty"])
        self.assertEqual(ask["sample_rows"][0][:2], ["EURUSD", "buy"])
        self.assertEqual(self.post("/api/imports/", text=text).status_code, 400)

        mapping = {
            "symbol": "Pair", "side": "Dir", "opened_at": "Start", "closed_at": "End",
            "entry_price": "In", "exit_price": "Out", "lots": "Qty",
        }
        ok = self.post("/api/imports/preview/", text=text, mapping=mapping).json()
        self.assertEqual(
            (ok["needs_mapping"], ok["importable"], ok["preview"][0]["net_pnl"]),
            (False, 1, "500.00"),
        )

    def test_decimal_comma_is_detected_and_parsed(self):
        body = self.post("/api/imports/preview/", text=table(";", row=EU_ROW)).json()
        self.assertEqual((body["decimal_separator"], body["decimal_separator_detected"]), (",", ","))
        self.assertEqual((body["importable"], body["error_count"]), (1, 0))
        trade = body["preview"][0]
        self.assertEqual(
            (trade["entry_price"], trade["exit_price"], trade["net_pnl"]),
            ("1.100000", "1.105000", "493.00"),
        )

    def test_wrong_separator_is_refused_row_by_row(self):
        eu = self.post("/api/imports/preview/", text=table(";", row=EU_ROW), decimal_separator=".").json()
        self.assertEqual((eu["importable"], eu["error_count"]), (0, 1))
        self.assertIn("decimal comma", eu["errors"][0]["message"])

        dots = self.post("/api/imports/preview/", text=table("\t"), decimal_separator=",").json()
        self.assertEqual((dots["importable"], dots["error_count"]), (0, 1))
        self.assertIn("decimal point", dots["errors"][0]["message"])

        bad = self.post("/api/imports/preview/", text=table("\t"), decimal_separator=";")
        self.assertEqual(bad.status_code, 400)

    def test_template_keeps_the_separator(self):
        first = self.post("/api/imports/", text=table(";", row=EU_ROW), save_template_name="EU broker")
        self.assertEqual(first.status_code, 201)
        template = ImportTemplate.objects.get(name="EU broker")
        self.assertEqual(template.decimal_separator, ",")
        self.assertEqual(self.client.get("/api/import-templates/").json()[0]["decimal_separator"], ",")

        next_day = list(EU_ROW)
        next_day[2], next_day[3] = "2026.10.16 14:00:00", "2026.10.16 16:00:00"
        second = self.post(
            "/api/imports/preview/", text=table(";", row=next_day), template=template.id
        ).json()
        self.assertEqual((second["decimal_separator"], second["importable"]), (",", 1))

    def test_semicolon_file_upload(self):
        upload = SimpleUploadedFile(
            "eu.csv", table(";", row=EU_ROW).encode(), content_type="text/csv"
        )
        res = self.client.post(
            "/api/imports/preview/", {"file": upload, "account": self.account.id}, format="multipart"
        )
        body = res.json()
        self.assertEqual((res.status_code, body["decimal_separator"], body["importable"]), (200, ",", 1))

    def test_xlsx_numbers_ignore_the_separator(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(COLUMNS)
        sheet.append([
            "EURUSD", "buy", datetime(2026, 10, 15, 14, 0), datetime(2026, 10, 15, 16, 0),
            1.1, 1.105, 1, 1.095, -7, 0, 500,
        ])
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        headers, rows = read_table(buffer, "t.xlsx")
        for separator in (".", ","):
            plan = build_plan(
                self.account, headers, rows, suggest_mapping(headers), decimal_separator=separator
            )
            self.assertEqual((plan.errors, plan.trades[0].net_pnl), ([], D("493.00")), separator)

    def test_input_validation(self):
        url = "/api/imports/preview/"
        self.assertEqual(self.client.post(url, {"account": self.account.id}, format="json").status_code, 400)
        self.assertEqual(self.post(url, text="   \n  ").status_code, 400)
        self.assertEqual(self.post(url, text="x" * 1_000_001).status_code, 400)
        self.assertEqual(self.post(url, text="Symbol\tType").status_code, 400)  # no trades

        both = self.client.post(
            url,
            {"account": self.account.id, "text": table("\t"),
             "file": SimpleUploadedFile("t.csv", table(",").encode())},
            format="multipart",
        )
        self.assertEqual(both.status_code, 400)
        theirs = self.client.post(
            url, {"account": self.bob_account.id, "text": table("\t")}, format="json"
        )
        self.assertEqual(theirs.status_code, 400)