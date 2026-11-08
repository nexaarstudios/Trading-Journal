from datetime import timedelta, timezone as dt_tz

from django.core.management.base import BaseCommand
from django.db.models import Max, Min

from trades import fx
from trades.models import FxRate, InstrumentSpec, Trade, TradingAccount


class Command(BaseCommand):
    help = "Download the daily FX rates that cross-pair trades need"

    def add_arguments(self, parser):
        parser.add_argument("--currency", help="Only accounts in this currency, e.g. USD")

    def handle(self, *args, **options):
        today = fx.utc_today()
        accounts = TradingAccount.objects.all()
        if options["currency"]:
            accounts = accounts.filter(currency=options["currency"].upper())
        specs = list(InstrumentSpec.objects.filter(account__isnull=True))

        total = 0
        for currency in sorted(set(accounts.values_list("currency", flat=True))):
            first = Trade.objects.filter(account__currency=currency).aggregate(
                first=Min("closed_at")
            )["first"]
            start = (first.astimezone(dt_tz.utc).date() if first else today) - timedelta(
                days=fx.WINDOW_DAYS
            )
            quotes = sorted({
                s.quote_currency for s in specs
                if currency not in (s.base_currency, s.quote_currency)
            })
            for quote in quotes:
                last = FxRate.objects.filter(
                    base_currency=currency, quote_currency=quote
                ).aggregate(last=Max("date"))["last"]
                since = max(start, last - timedelta(days=3)) if last else start
                try:
                    count = fx.fetch_range(currency, quote, since, today)
                except fx.FxFetchError as exc:
                    self.stderr.write(f"{currency}/{quote}: {exc}")
                    continue
                total += count
                self.stdout.write(f"{currency}/{quote}: {count} days")
        self.stdout.write(self.style.SUCCESS(f"Stored {total} rates"))