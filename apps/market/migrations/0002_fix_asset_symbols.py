from django.db import migrations

def fix_asset_symbols(apps, schema_editor):
    Asset = apps.get_model('market', 'Asset')
    
    # ETHUSD → ETH-USDT
    Asset.objects.filter(symbol='ETHUSD').update(symbol='ETH-USDT', name='ETH-USDT', exchange='DELTA')
    
    # BTCUSDT → BTC-USDT
    Asset.objects.filter(symbol='BTCUSDT').update(symbol='BTC-USDT', name='BTC-USDT', exchange='DELTA')
    
    # BSE:SENSEX-INDEX duplicate delete
    if Asset.objects.filter(symbol='SENSEX').exists():
        Asset.objects.filter(symbol='BSE:SENSEX-INDEX').delete()

def reverse_fix(apps, schema_editor):
    Asset = apps.get_model('market', 'Asset')
    Asset.objects.filter(symbol='ETH-USDT', exchange='DELTA').update(symbol='ETHUSD', name='ETHUSD', exchange='')
    Asset.objects.filter(symbol='BTC-USDT', exchange='DELTA').update(symbol='BTCUSDT', name='BTCUSDT', exchange='')

class Migration(migrations.Migration):
    dependencies = [
        ('market', '0001_initial'),
    ]
    operations = [
        migrations.RunPython(fix_asset_symbols, reverse_fix),
    ]
