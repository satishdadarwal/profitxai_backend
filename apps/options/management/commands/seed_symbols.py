from django.core.management.base import BaseCommand
from apps.options.models import OptionSymbol

symbols = [
    {'name': 'NIFTY',      'fyers_symbol': 'NSE:NIFTY50-INDEX',    'lot_size': 65,  'strike_step': 50},
    {'name': 'BANKNIFTY',  'fyers_symbol': 'NSE:NIFTYBANK-INDEX',  'lot_size': 30,  'strike_step': 100},
    {'name': 'FINNIFTY',   'fyers_symbol': 'NSE:FINNIFTY-INDEX',   'lot_size': 60,  'strike_step': 50},
    {'name': 'MIDCPNIFTY', 'fyers_symbol': 'NSE:MIDCPNIFTY-INDEX', 'lot_size': 120, 'strike_step': 25},
    {'name': 'NIFTYNXT50', 'fyers_symbol': 'NSE:NIFTYNXT50-INDEX', 'lot_size': 25,  'strike_step': 50},
    {'name': 'SENSEX',     'fyers_symbol': 'BSE:SENSEX-INDEX',     'lot_size': 20,  'strike_step': 100},
    {'name': 'BANKEX',     'fyers_symbol': 'BSE:BANKEX-INDEX',     'lot_size': 15,  'strike_step': 100},
]

class Command(BaseCommand):
    help = "Seed default index symbols"

    def handle(self, *args, **kwargs):
        for sym in symbols:
            obj, created = OptionSymbol.objects.update_or_create(
                name=sym['name'],
                defaults=sym
            )
            if created:
                self.stdout.write(self.style.SUCCESS(f"✅ Created {sym['name']}"))
            else:
                self.stdout.write(f"🔄 Updated {sym['name']}")