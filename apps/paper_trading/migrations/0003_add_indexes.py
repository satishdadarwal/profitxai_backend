from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ('paper_trading', '0002_alter_papertrade_asset_type'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(
                fields=['symbol', 'status'],
                name='idx_symbol_status'
            ),
        ),
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(
                fields=['status', 'account'],
                name='idx_status_account'
            ),
        ),
        migrations.AddIndex(
            model_name='papertrade',
            index=models.Index(
                fields=['strategy_id', 'status'],
                name='idx_strategy_status'
            ),
        ),
    ]
