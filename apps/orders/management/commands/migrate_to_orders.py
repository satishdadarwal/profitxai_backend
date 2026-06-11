"""
Migration command — OptionTrade and PaperTrade models have been removed.
All records are now managed through the Order model directly.
This command is kept as a no-op to avoid import errors.
"""

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "No-op: OptionTrade/PaperTrade migration is complete — all records use Order model"

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="(ignored — migration already complete)",
        )

    def handle(self, *args, **options):
        self.stdout.write(
            self.style.SUCCESS(
                "Migration complete: OptionTrade and PaperTrade models have been removed. "
                "All trade data uses apps.orders.models.Order."
            )
        )
