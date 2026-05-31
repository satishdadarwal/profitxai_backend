# apps/brokers/migrations/0014_zerodha_broker_support.py
# Zerodha broker support add kiya

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brokers", "0013_dhan_broker_support"),
    ]

    operations = [
        # 1. BrokerType choices mein "zerodha" add karo
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
        # 2. zerodha_user_id — "AB1234" format (Zerodha user ID)
        migrations.AddField(
            model_name="brokeraccount",
            name="zerodha_user_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Zerodha User ID (e.g. 'AB1234') — from OAuth response",
                max_length=20,
                db_index=True,
            ),
        ),
        # 3. zerodha_request_token — temporary (callback mein use hota hai)
        migrations.AddField(
            model_name="brokeraccount",
            name="zerodha_request_token",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Temporary request_token (callback se aata hai, exchange ke baad clear ho jaata hai)",
                max_length=500,
            ),
        ),
    ]