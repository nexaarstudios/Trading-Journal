import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.db import transaction

from . import fx, rollups
from .models import ImportBatch, InstrumentSpec, Trade
from .pnl import PnLError, compute_pnl

MAX_ROWS = 20_000
ZERO = Decimal("0")
LIMIT = Decimal("1e11")  # keeps every value inside the database column sizes

REQUIRED = ["symbol", "side", "opened_at", "closed_at", "entry_price", "exit_price", "lots"]
OPTIONAL = [
    "stop_loss", "take_profit", "commission", "swap", "fees",
    "broker_profit", "quote_to_account_rate",
]
FIELDS = REQUIRED + OPTIONAL
NUMERIC_FIELDS = [
    "entry_price", "exit_price", "lots", "stop_loss", "take_profit",
    "commission", "swap", "fees", "broker_profit", "quote_to_account_rate",
]

# Normalised header names (lowercase, letters and digits only) -> our field
ALIASES = {
    "symbol": ["symbol", "instrument", "item", "pair", "ticker"],
    "side": ["side", "type", "direction", "action"],
    "opened_at": ["opentime", "opendate", "entrytime", "entrydate", "time"],
    "closed_at": ["closetime", "closedate", "exittime", "exitdate", "time2"],
    "entry_price": ["openprice", "entryprice", "price"],
    "exit_price": ["closeprice", "exitprice", "price2"],
    "lots": ["lots", "size", "volume", "quantity", "qty"],
    "stop_loss": ["sl", "stoploss", "stop"],
    "take_profit": ["tp", "takeprofit", "target"],
    "commission": ["commission", "comm"],
    "swap": ["swap", "rollover"],
    "fees": ["fees", "fee", "taxes", "tax"],
    "broker_profit": ["profit", "pl", "pnl", "netprofit"],
    "quote_to_account_rate": ["rate", "fxrate", "conversionrate"],
}

MT_FORMATS = ("%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M")
LONG = {"buy", "long", "b", "l"}
SHORT = {"sell", "short", "s"}

THOUSANDS_POINT = re.compile(r"^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$")  # 1,234.56
THOUSANDS_COMMA = re.compile(r"^[+-]?\d{1,3}(\.\d{3})+(,\d+)?$")  # 1.234,56
SPACES = re.compile(r"[\s\u00a0\u202f]")


class ImportFileError(ValueError):
    """The whole file or mapping is unusable."""


class RowError(ValueError):
    """One row is unusable. The rest of the file still imports."""


@dataclass
class ImportPlan:
    account: object
    total_rows: int = 0
    trades: list = field(default_factory=list)   # unsaved Trade objects, ready to insert
    duplicates: int = 0
    errors: list = field(default_factory=list)   # [(line_number, message)]
    decimal_separator: str = "."                 # the one used to read numbers
    detected_decimal_separator: str = "."        # what the data itself suggests


# ---------- reading files and pasted text ----------

def _text(value):
    return "" if value is None else str(value).strip()


def _is_blank(values):
    return not any(_text(v) for v in values)


def _split_text(text, allow_spaces=False):
    """Split CSV-like text into rows, choosing the delimiter without being fooled by decimal commas."""
    lines = [line for line in text.split("\n") if line.strip()]
    # A delimiter that appears the same number of times on every line is almost certainly the one
    for delimiter in ("\t", ";", "|", ","):
        counts = {line.count(delimiter) for line in lines}
        if len(counts) == 1 and counts != {0}:
            return list(csv.reader(io.StringIO(text), delimiter=delimiter))
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = None
    if dialect is not None:
        table = list(csv.reader(io.StringIO(text), dialect))
        if max((len(row) for row in table), default=0) >= 2:
            return table
    if allow_spaces:  # text pasted from a PDF or web page: columns are separated by runs of spaces
        return [re.split(r"\s{2,}", line.strip()) for line in lines]
    return list(csv.reader(io.StringIO(text), csv.excel))


def _read_csv(fileobj):
    data = fileobj.read()
    if isinstance(data, bytes):
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            text = data.decode("utf-16")
        else:
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("latin-1")
    else:
        text = data
    return _split_text(text.replace("\r\n", "\n").replace("\r", "\n"))


def _read_xlsx(fileobj):
    from openpyxl import load_workbook

    try:
        workbook = load_workbook(fileobj, read_only=True, data_only=True)
    except Exception:
        raise ImportFileError("Could not read this Excel file.")
    try:
        return [list(row) for row in workbook.active.iter_rows(values_only=True)]
    finally:
        workbook.close()


def _unique_headers(first_row):
    seen, headers = {}, []
    for i, value in enumerate(first_row):
        name = _text(value) or f"Column {i + 1}"
        seen[name] = seen.get(name, 0) + 1
        headers.append(name if seen[name] == 1 else f"{name} ({seen[name]})")
    return headers


def _table_to_rows(table, what):
    """Turn a list of lists into (headers, [dict per row])."""
    while table and _is_blank(table[-1]):
        table.pop()
    while table and _is_blank(table[0]):
        table.pop(0)
    if len(table) < 2:
        raise ImportFileError(f"The {what} needs a header row and at least one trade.")
    if len(table) - 1 > MAX_ROWS:
        raise ImportFileError(f"Imports are limited to {MAX_ROWS:,} rows. Split this one.")
    headers = _unique_headers(table[0])
    rows = [dict(zip(headers, values)) for values in table[1:]]
    return headers, rows


def read_table(fileobj, filename):
    """Read an uploaded .csv or .xlsx file."""
    name = filename.lower()
    if name.endswith(".xlsx"):
        table = _read_xlsx(fileobj)
    elif name.endswith((".csv", ".tsv", ".txt")):
        table = _read_csv(fileobj)
    else:
        raise ImportFileError("Upload a .csv or .xlsx file.")
    return _table_to_rows(table, "file")


def read_pasted(text, has_header=True):
    """Read text copied from a table. Without a header row, columns are named Column 1, Column 2, ..."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ImportFileError("Paste some rows first.")
    table = _split_text(text, allow_spaces=True)
    if table and not has_header:
        width = max(len(row) for row in table)
        table.insert(0, [f"Column {i}" for i in range(1, width + 1)])
    return _table_to_rows(table, "pasted text")


def sample_rows(headers, rows, limit=3):
    """The first few raw rows as lists of text, for the mapping screen."""
    return [[_text(row.get(h)) for h in headers] for row in rows[:limit]]


def suggest_mapping(headers):
    """Guess a mapping from common export headers (MetaTrader and similar)."""
    normalised = {h: re.sub(r"[^a-z0-9]", "", h.lower()) for h in headers}
    mapping, used = {}, set()
    for target in FIELDS:
        for header, key in normalised.items():
            if header not in used and key in ALIASES[target]:
                mapping[target] = header
                used.add(header)
                break
    return mapping


def missing_fields(mapping):
    return [f for f in REQUIRED if not mapping.get(f)]


def validate_mapping(headers, mapping):
    missing = missing_fields(mapping)
    if missing:
        raise ImportFileError(f"Map these required fields: {', '.join(missing)}.")
    unknown = sorted({c for c in mapping.values() if c and c not in headers})
    if unknown:
        raise ImportFileError(f"Columns not found in the file: {', '.join(unknown)}.")


# ---------- parsing single values ----------

def _separator_vote(text):
    """Which decimal separator does this one value prove? None when it could be either."""
    s = SPACES.sub("", text)
    if "," in s and "." in s:
        return "," if s.rfind(",") > s.rfind(".") else "."  # the last one is the decimal mark
    if "," in s:
        if re.fullmatch(r"[+-]?\d+,\d+", s) and not re.fullmatch(r"[+-]?\d{1,3}(,\d{3})+", s):
            return ","
        return None
    if re.fullmatch(r"[+-]?\d+\.\d+", s) and not re.fullmatch(r"[+-]?\d{1,3}(\.\d{3})+", s):
        return "."
    return None


def suggest_decimal_separator(rows, mapping):
    """Read the number columns: a comma only wins when the data has no point-decimals at all."""
    votes = {",": 0, ".": 0}
    columns = [mapping[f] for f in NUMERIC_FIELDS if mapping.get(f)]
    for row in rows[:200]:
        for column in columns:
            value = row.get(column)
            if isinstance(value, str):
                vote = _separator_vote(value)
                if vote:
                    votes[vote] += 1
    return "," if votes[","] and not votes["."] else "."


def _clean_number(text, label, sep):
    """Turn '1,234.56' or '1.234,56' into '1234.56'. Anything ambiguous is refused, never guessed."""
    s = SPACES.sub("", text)
    if sep == ",":
        if "." in s:
            if not THOUSANDS_COMMA.match(s):
                raise RowError(
                    f"{label}: '{text}' uses a decimal point. Set the decimal separator to point."
                )
            s = s.replace(".", "")
        return s.replace(",", ".")
    if "," in s:
        if not THOUSANDS_POINT.match(s):
            raise RowError(
                f"{label}: '{text}' uses a decimal comma. Set the decimal separator to comma."
            )
        s = s.replace(",", "")
    return s


def _decimal(value, places, label, required=False, sep="."):
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        text = s = str(value)  # real numbers (from Excel) have no separator to interpret
    else:
        text = _text(value)
        if not text:
            if required:
                raise RowError(f"{label} is empty.")
            return None
        s = _clean_number(text, label, sep)
    try:
        number = Decimal(s)
    except InvalidOperation:
        raise RowError(f"{label}: '{text}' is not a number.")
    if not number.is_finite() or abs(number) >= LIMIT:
        raise RowError(f"{label}: '{text}' is out of range.")
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def _cost(value, sign):
    # Adding ZERO turns a negative zero (-0.00) into a plain 0.00
    return (value or ZERO) * sign + ZERO


def _datetime(value, tz, date_format, label):
    if isinstance(value, datetime):
        parsed = value
    else:
        s = _text(value)
        if not s:
            raise RowError(f"{label} is empty.")
        parsed = None
        if date_format:
            try:
                parsed = datetime.strptime(s, date_format)
            except ValueError:
                raise RowError(f"{label}: '{s}' does not match the format {date_format}.")
        else:
            try:
                parsed = datetime.fromisoformat(s)
            except ValueError:
                for fmt in MT_FORMATS:
                    try:
                        parsed = datetime.strptime(s, fmt)
                        break
                    except ValueError:
                        continue
            if parsed is None:
                raise RowError(
                    f"{label}: can't read '{s}'. Set a date format such as %d/%m/%Y %H:%M."
                )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)  # naive times are in the file's timezone
    return parsed.astimezone(dt_timezone.utc)


def _side(value):
    s = _text(value).lower()
    if s in LONG:
        return "long"
    if s in SHORT:
        return "short"
    raise RowError(f"side: '{_text(value)}' is not buy or sell.")


def _symbol(value, specs):
    s = re.sub(r"[^A-Za-z0-9]", "", _text(value)).upper()  # EUR/USD, EURUSD.pro -> clean
    if not s:
        raise RowError("symbol is empty.")
    if s in specs:
        return s
    if len(s) > 6 and s[:6] in specs:  # broker suffixes: EURUSDm, EURUSD.pro
        return s[:6]
    raise RowError(f"No instrument spec for {s}. Add it in Instrument specs.")


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ImportFileError(f"Unknown timezone '{name}'. Use a name like Africa/Lagos.")


def load_specs(account):
    specs = {s.symbol: s for s in InstrumentSpec.objects.filter(account__isnull=True)}
    specs.update({s.symbol: s for s in InstrumentSpec.objects.filter(account=account)})
    return specs


# ---------- building trades ----------

def _build_trade(raw, mapping, specs, account, tz, date_format, sign, sep, book):
    def get(name):
        column = mapping.get(name)
        return raw.get(column) if column else None

    def number(name, places, required=False):
        return _decimal(get(name), places, name, required=required, sep=sep)

    symbol = _symbol(get("symbol"), specs)
    spec = specs[symbol]
    side = _side(get("side"))
    opened = _datetime(get("opened_at"), tz, date_format, "opened_at")
    closed = _datetime(get("closed_at"), tz, date_format, "closed_at")
    if closed < opened:
        raise RowError("Close time is before open time.")

    entry = number("entry_price", 6, required=True)
    exit_price = number("exit_price", 6, required=True)
    lots = number("lots", 4, required=True)
    stop = number("stop_loss", 6)
    target = number("take_profit", 6)
    commission = _cost(number("commission", 2), sign)
    swap = _cost(number("swap", 2), sign)
    fees = _cost(number("fees", 2), sign)
    broker_profit = number("broker_profit", 2)
    rate = number("quote_to_account_rate", 8)

    # A rate in the file wins. Otherwise cross pairs use the FX table (closed is UTC here).
    rate_source = "manual" if rate is not None else ""
    if rate is None and fx.needs_rate(spec, account.currency):
        found = book.lookup(spec.quote_currency, account.currency, closed.date())
        if found:
            rate, rate_source = found.rate, "auto"

    result = compute_pnl(
        side=side, entry=entry, exit_price=exit_price, lots=lots,
        contract_size=spec.contract_size,
        base_currency=spec.base_currency, quote_currency=spec.quote_currency,
        account_currency=account.currency,
        commission=commission, swap=swap, fees=fees, stop=stop,
        quote_to_account_rate=rate, broker_profit=broker_profit,
    )
    trade = Trade(
        account=account, symbol=symbol, side=side, opened_at=opened, closed_at=closed,
        entry_price=entry, exit_price=exit_price, lots=lots,
        stop_loss=stop, take_profit=target,
        commission=commission, swap=swap, fees=fees,
        quote_to_account_rate=rate, rate_source=rate_source, broker_profit=broker_profit,
    )
    trade.apply_result(result)
    return trade


def _parse_rows(rows, mapping, specs, account, tz, date_format, sign, sep, book):
    parsed, errors, total = [], [], 0
    for line, raw in enumerate(rows, start=2):  # line 1 is the header
        if _is_blank(raw.values()):
            continue
        total += 1
        try:
            parsed.append(
                _build_trade(raw, mapping, specs, account, tz, date_format, sign, sep, book)
            )
        except (RowError, PnLError) as exc:
            errors.append((line, str(exc)))
    return parsed, errors, total


def build_plan(
    account, headers, rows, mapping, tz_name="UTC", date_format="",
    costs_as_negative=True, decimal_separator=None,
):
    """Parse every row and work out what would import. Writes nothing to the trade tables."""
    validate_mapping(headers, mapping)
    tz = _zone(tz_name)
    sign = Decimal(-1) if costs_as_negative else Decimal(1)
    specs = load_specs(account)
    detected = suggest_decimal_separator(rows, mapping)
    separator = decimal_separator or detected
    plan = ImportPlan(
        account=account, decimal_separator=separator, detected_decimal_separator=detected
    )

    args = (rows, mapping, specs, account, tz, date_format, sign, separator)
    book = fx.RateBook()
    parsed, errors, total = _parse_rows(*args, book)
    if book.misses and fx.ensure_pairs(book.misses):
        # Rates were missing from the table and have just been downloaded: parse again with them
        parsed, errors, total = _parse_rows(*args, fx.RateBook())
    plan.total_rows, plan.errors = total, errors

    if parsed:
        low = min(t.opened_at for t in parsed)
        high = max(t.opened_at for t in parsed)
        seen = set(
            Trade.objects.filter(account=account, opened_at__range=(low, high))
            .values_list("symbol", "opened_at", "entry_price", "lots")
        )
        for trade in parsed:
            key = (trade.symbol, trade.opened_at, trade.entry_price, trade.lots)
            if key in seen:
                plan.duplicates += 1
            else:
                seen.add(key)
                plan.trades.append(trade)
    return plan


# ---------- saving and undoing ----------

def commit_plan(plan, filename="", template=None):
    """Save the whole plan in one transaction: all of it lands, or none of it."""
    with transaction.atomic():
        batch = ImportBatch.objects.create(
            account=plan.account, template=template, filename=filename[:255],
            total_rows=plan.total_rows, imported_count=len(plan.trades),
            duplicate_count=plan.duplicates, error_count=len(plan.errors),
        )
        for trade in plan.trades:
            trade.import_batch = batch
        Trade.objects.bulk_create(plan.trades, batch_size=1000)
        # bulk_create skips signals, so refresh the touched days explicitly
        rollups.touch(plan.account.pk, {t.closed_at for t in plan.trades})
    return batch


def undo_batch(batch):
    with transaction.atomic():
        batch = ImportBatch.objects.select_for_update().get(pk=batch.pk)
        if batch.status == ImportBatch.Status.UNDONE:
            raise ImportFileError("This import was already undone.")
        trades = Trade.objects.filter(import_batch=batch)
        removed = trades.count()
        with rollups.deferred():  # one refresh for the whole batch, not one per trade
            trades.delete()
        batch.status = ImportBatch.Status.UNDONE
        batch.save(update_fields=["status"])
    return removed