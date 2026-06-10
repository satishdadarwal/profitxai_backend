# Generated migration for Order trade tracking fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0010_order_journal_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='entry_price',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='exit_price',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='realized_pnl',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='unrealized_pnl',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='current_price',
            field=models.DecimalField(blank=True, decimal_places=6, max_digits=16, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='entry_time',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='exit_time',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='order',
            name='exit_reason',
            field=models.CharField(blank=True, default='', max_length=50),
        ),
        migrations.AddField(
            model_name='order',
            name='position_size',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
