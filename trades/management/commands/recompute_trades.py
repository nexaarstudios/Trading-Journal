from django.core.management.base import BaseCommand

from trades import rollups
from trades.models import Trade, TradingAccount
from trades.pnl import PnLError


class Command(BaseCommand):
    help = "Recompute P&L, R and risk on stored trades, then rebuild the daily rollups"

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Only this user's accounts")

    def handle(self, *args, **options):
        accounts = TradingAccount.objects.select_related("user")
        if options["email"]:
            accounts = accounts.filter(user__email__iexact=options["email"])

        done = skipped = 0
        fields = [
            "gross_pnl", "net_pnl", "r_multiple", "risk_amount", "duration_seconds",
            "quote_to_account_rate", "rate_source",
        ]
        for account in accounts:
            fixed = []
            for trade in Trade.objects.filter(account=account).select_related("account"):
                try:
                    trade.apply_result(trade._compute())
                except PnLError:
                    skipped += 1  # e.g. its instrument spec was deleted
                    continue
                fixed.append(trade)
            Trade.objects.bulk_update(fixed, fields, batch_size=1000)
            rollups.rebuild_account(account)
            done += len(fixed)
        self.stdout.write(self.style.SUCCESS(f"Recomputed {done} trades, skipped {skipped}"))