from decimal import Decimal

from django.core.management.base import BaseCommand

from trades.models import InstrumentSpec

FOREX_PAIRS = """
EURUSD GBPUSD AUDUSD NZDUSD USDJPY USDCHF USDCAD
EURGBP EURJPY EURCHF EURAUD EURNZD EURCAD
GBPJPY GBPCHF GBPAUD GBPNZD GBPCAD
AUDJPY AUDNZD AUDCAD AUDCHF
NZDJPY NZDCAD NZDCHF
CADJPY CADCHF CHFJPY
""".split()

# Anything that isn't a standard forex pair goes here:
# symbol, asset_class, base, quote, contract_size, pip_size
OTHER_SPECS = [
    ("XAUUSD", "metal", "XAU", "USD", "100", "0.01"),
]


def build_specs():
    for symbol in FOREX_PAIRS:
        base, quote = symbol[:3], symbol[3:]
        pip = "0.01" if quote == "JPY" else "0.0001"
        yield (symbol, "forex", base, quote, "100000", pip)
    yield from OTHER_SPECS


class Command(BaseCommand):
    help = "Create or update the default (global) instrument specs"

    def handle(self, *args, **options):
        count = 0
        for symbol, cls, base, quote, size, pip in build_specs():
            InstrumentSpec.objects.update_or_create(
                account=None,
                symbol=symbol,
                defaults={
                    "asset_class": cls,
                    "base_currency": base,
                    "quote_currency": quote,
                    "contract_size": Decimal(size),
                    "pip_size": Decimal(pip),
                },
            )
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Seeded {count} instrument specs"))