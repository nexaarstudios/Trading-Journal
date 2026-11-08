import json

from django.core.management.base import BaseCommand

from trades.endpoints import collect


class Command(BaseCommand):
    help = "List every /api/ endpoint, read from the real URL configuration, as JSON"

    def add_arguments(self, parser):
        parser.add_argument("--output", help="Write to this file instead of printing")

    def handle(self, *args, **options):
        endpoints = collect()
        body = json.dumps({"count": len(endpoints), "endpoints": endpoints}, indent=2)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as handle:
                handle.write(body + "\n")
            self.stdout.write(self.style.SUCCESS(f"Wrote {len(endpoints)} endpoints to {options['output']}"))
        else:
            self.stdout.write(body)