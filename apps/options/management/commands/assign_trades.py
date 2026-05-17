from django.core.management.base import BaseCommand
from apps.options.models import OptionTrade
from apps.strategies.models import Strategy


class Command(BaseCommand):
    help = 'Assign unassigned option trades to a strategy'

    def handle(self, *args, **options):
        unassigned = OptionTrade.objects.filter(strategy__isnull=True, mode='paper')
        count = unassigned.count()
        
        self.stdout.write(f"Found {count} unassigned trades")
        
        if count == 0:
            self.stdout.write("No trades to assign")
            return
        
        # Get EMA strategy
        ema = Strategy.objects.filter(name__icontains="EMA").first()
        
        if ema:
            self.stdout.write(f"Assigning to: {ema.name}")
            updated = unassigned.update(strategy=ema)
            self.stdout.write(self.style.SUCCESS(f"✓ Assigned {updated} trades"))
        else:
            self.stdout.write(self.style.WARNING("No EMA strategy found"))