from django.core.management.base import BaseCommand

from trades import rollups
from trades.models import TradingAccount


class Command(BaseCommand):
    help = "Rebuild the daily rollup rows from the trades table"

    def add_arguments(self, parser):
        parser.add_argument("--email", help="Only rebuild this user's accounts")

    def handle(self, *args, **options):
        accounts = TradingAccount.objects.select_related("user")
        if options["email"]:
            accounts = accounts.filter(user__email__iexact=options["email"])
        count = 0
        for account in accounts:
            rollups.rebuild_account(account)
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Rebuilt rollups for {count} accounts"))