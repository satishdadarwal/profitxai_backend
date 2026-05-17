# backfill_order_strategy.py
from django.core.management.base import BaseCommand
import re
from apps.orders.models import Order
from apps.strategies.models import Strategy

class Command(BaseCommand):
    help = 'Backfill Order.strategy FK from order.notes field'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true',
                            help='Simulate without saving')

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        orders = Order.objects.filter(strategy__isnull=True)
        self.stdout.write(f"Orders without strategy: {orders.count()}")

        linked, skipped, not_found = 0, 0, 0

        for order in orders:
            if not order.notes:
                skipped += 1
                continue

            match = re.search(r'strategy=([a-f0-9-]+)', order.notes)
            if not match:
                skipped += 1
                continue

            strategy_id = match.group(1)
            try:
                strategy = Strategy.objects.get(id=strategy_id)
                if not dry_run:
                    order.strategy = strategy
                    order.save(update_fields=['strategy'])
                self.stdout.write(f"✅ Order {order.id} → {strategy.name}")
                linked += 1
            except Strategy.DoesNotExist:
                self.stdout.write(f"❌ Strategy {strategy_id} not found")
                not_found += 1

        self.stdout.write(
            f"\nDone — Linked: {linked}, Skipped: {skipped}, Not found: {not_found}"
        )