from django.core.management.base import BaseCommand
from apps.orders.models import Order
from apps.strategies.models import Strategy


class Command(BaseCommand):
    help = 'Assign unassigned option orders to a strategy'

    def handle(self, *args, **options):
        unassigned = Order.objects.filter(
            strategy__isnull=True,
            mode=Order.Mode.PAPER,
            instrument_type=Order.InstrumentType.OPTIONS,
        )
        count = unassigned.count()

        self.stdout.write(f"Found {count} unassigned orders")

        if count == 0:
            self.stdout.write("No orders to assign")
            return

        # Get EMA strategy
        ema = Strategy.objects.filter(name__icontains="EMA").first()

        if ema:
            self.stdout.write(f"Assigning to: {ema.name}")
            updated = unassigned.update(strategy=ema)
            self.stdout.write(self.style.SUCCESS(f"Assigned {updated} orders"))
        else:
            self.stdout.write(self.style.WARNING("No EMA strategy found"))
