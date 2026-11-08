import json
import re
import time
import urllib.request
from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings

from .models import FxRate

API = "https://api.frankfurter.dev/v1"
WINDOW_DAYS = 7        # how far back a lookup may reach (weekends, bank holidays)
CHUNK_DAYS = 60        # time-series requests stay short on purpose
COOLDOWN_SECONDS = 60  # after a failed download, leave that pair alone for a minute
EIGHT = Decimal("0.00000001")
ONE = Decimal("1.00000000")
CODE = re.compile(r"^[A-Z]{3}$")

_cooldowns = {}  # (base, symbol) -> monotonic time before which we won't retry


class FxFetchError(Exception):
    """The rate provider could not be reached or rejected the request."""


@dataclass(frozen=True)
class FoundRate:
    rate: Decimal  # units of the target currency per 1 unit of the source currency
    day: date      # the day this rate was published for


def utc_today():
    return datetime.now(dt_timezone.utc).date()


def needs_rate(spec, account_currency):
    """Only cross pairs need one: neither side of the pair is the account currency."""
    return spec.quote_currency != account_currency and spec.base_currency != account_currency


class RateBook:
    """Daily rates, read from the database once per pair."""

    def __init__(self):
        self._series = {}
        self.misses = set()  # (from, to, day) lookups that found nothing

    def _load(self, base, quote):
        key = (base, quote)
        if key not in self._series:
            rows = list(
                FxRate.objects.filter(base_currency=base, quote_currency=quote)
                .order_by("date")
                .values_list("date", "rate")
            )
            self._series[key] = ([r[0] for r in rows], [r[1] for r in rows])
        return self._series[key]

    def lookup(self, from_ccy, to_ccy, day):
        """Units of to_ccy per 1 from_ccy: the latest rate on or before `day`, within the window."""
        if from_ccy == to_ccy:
            return FoundRate(ONE, day)
        candidates = []
        for base, quote, invert in ((from_ccy, to_ccy, False), (to_ccy, from_ccy, True)):
            dates, rates = self._load(base, quote)
            i = bisect_right(dates, day) - 1
            if i >= 0 and (day - dates[i]).days <= WINDOW_DAYS:
                rate = rates[i]
                if invert:
                    rate = (ONE / rate).quantize(EIGHT, rounding=ROUND_HALF_UP)
                candidates.append(FoundRate(rate, dates[i]))
        if not candidates:
            self.misses.add((from_ccy, to_ccy, day))
            return None
        return max(candidates, key=lambda c: c.day)  # the fresher rate wins; the direct one wins ties


def find_rate(from_ccy, to_ccy, day):
    """Look a rate up, downloading it first if the table has nothing close enough."""
    book = RateBook()
    found = book.lookup(from_ccy, to_ccy, day)
    if found is None and ensure_pairs(book.misses):
        book = RateBook()
        found = book.lookup(from_ccy, to_ccy, day)
    return found


# ---------- downloading ----------

def _http_get_json(url):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "trading-journal/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.FX_FETCH_TIMEOUT) as response:
            data = json.load(response)
    except (OSError, ValueError) as exc:  # network errors, HTTP errors, timeouts, bad JSON
        raise FxFetchError(str(exc)) from exc
    if not isinstance(data, dict):
        raise FxFetchError("Unexpected response from the rate provider.")
    return data


def _store(base, symbol, by_day):
    if not isinstance(by_day, dict):
        return 0
    rows = []
    for day_text, rates in by_day.items():
        try:
            day = date.fromisoformat(day_text)
            value = Decimal(str(rates[symbol]))
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
        if value.is_finite() and value > 0:
            rows.append(FxRate(
                base_currency=base, quote_currency=symbol, date=day, rate=value.quantize(EIGHT)
            ))
    FxRate.objects.bulk_create(
        rows, batch_size=500, update_conflicts=True,
        unique_fields=["base_currency", "quote_currency", "date"],
        update_fields=["rate", "source"],
    )
    return len(rows)


def fetch_range(base, symbol, start, end):
    """Download daily rates (1 `base` in `symbol`) from start to end and store them."""
    if not (CODE.match(base) and CODE.match(symbol)) or base == symbol:
        raise FxFetchError(f"Not a currency pair: {base}/{symbol}")
    stored = 0
    day = start
    while day <= end:
        last = min(day + timedelta(days=CHUNK_DAYS - 1), end)
        query = urlencode({"base": base, "symbols": symbol})
        if day == last:
            payload = _http_get_json(f"{API}/{day.isoformat()}?{query}")
            by_day = {payload.get("date"): payload.get("rates") or {}}
        else:
            payload = _http_get_json(f"{API}/{day.isoformat()}..{last.isoformat()}?{query}")
            by_day = payload.get("rates") or {}
        stored += _store(base, symbol, by_day)
        day = last + timedelta(days=1)
    return stored


def ensure_pairs(misses):
    """Download whatever a failed lookup was missing. Never raises. True if anything arrived."""
    if not settings.FX_AUTO_FETCH or not misses:
        return False
    wanted = {}
    for from_ccy, to_ccy, day in misses:
        # Ask for the account currency as the base: that direction keeps the most precision
        key = (to_ccy, from_ccy)
        low, high = wanted.get(key, (day, day))
        wanted[key] = (min(low, day), max(high, day))

    today = utc_today()
    stored = 0
    for (base, symbol), (low, high) in wanted.items():
        if time.monotonic() < _cooldowns.get((base, symbol), 0):
            continue
        start, end = low - timedelta(days=WINDOW_DAYS), min(high, today)
        if start > end:
            continue
        try:
            stored += fetch_range(base, symbol, start, end)
        except FxFetchError:
            _cooldowns[(base, symbol)] = time.monotonic() + COOLDOWN_SECONDS
    return stored > 0