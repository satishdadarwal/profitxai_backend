from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('orders', '0011_order_trade_fields')]
    operations = [
        migrations.AddIndex(model_name='order', index=models.Index(fields=['user', 'status', 'created_at'], name='order_user_status_idx')),
        migrations.AddIndex(model_name='order', index=models.Index(fields=['user', 'mode', 'status'], name='order_user_mode_idx')),
        migrations.AddIndex(model_name='order', index=models.Index(fields=['symbol_display', 'status'], name='order_symbol_status_idx')),
    ]
