# apps/brokers/migrations/0013_dhan_broker_support.py
# Generated manually — Dhan broker add kiya

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brokers", "0012_multi_user_sebi_compliance"),
    ]

    operations = [
        # 1. BrokerType choices mein "dhan" add karo
        migrations.AlterField(
            model_name="brokeraccount",
            name="broker",
            field=models.CharField(
                choices=[
                    ("zerodha", "Zerodha"),
                    ("binance", "Binance"),
                    ("fyers",   "Fyers"),
                    ("delta",   "Delta"),
                    ("dhan",    "Dhan"),
                ],
                max_length=20,
            ),
        ),
        # 2. dhan_client_id field
        migrations.AddField(
            model_name="brokeraccount",
            name="dhan_client_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Dhan Client ID (e.g. '1000000001')",
                max_length=50,
            ),
        ),
        # 3. dhan_access_token field
        migrations.AddField(
            model_name="brokeraccount",
            name="dhan_access_token",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Dhan access token (24hr validity — SEBI mandate)",
            ),
        ),
    ]
