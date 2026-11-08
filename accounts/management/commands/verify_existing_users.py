from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Mark every existing user's email as verified. Run once when turning verification on for an existing database."

    def handle(self, *args, **options):
        count = get_user_model().objects.filter(email_verified=False).update(email_verified=True)
        self.stdout.write(self.style.SUCCESS(f"Marked {count} existing users as verified"))