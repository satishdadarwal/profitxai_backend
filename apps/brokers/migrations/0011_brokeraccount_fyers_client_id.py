# apps/brokers/migrations/0011_brokeraccount_fyers_client_id.py
# ✅ FIXED: blank=True, default="" — NOT NULL error nahi aayega
# Existing rows ke liye bhi "" default set hoga

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brokers", "0010_brokerorder_realized_pnl"),
    ]

    operations = [
        migrations.AddField(
            model_name="brokeraccount",
            name="fyers_client_id",
            field=models.CharField(
                max_length=20,
                blank=True,
                null=True,        # ✅ FIX: null=True temporarily during migration
                default="",
                db_index=True,
                help_text=(
                    "User ka Fyers client ID (e.g. 'YC00329'). "
                    "Auto-login aur multi-user account identification ke liye."
                ),
            ),
            preserve_default=True,
        ),
        # Step 2: Existing NULL rows ko empty string se fill karo
        migrations.RunSQL(
            sql="UPDATE brokers_brokeraccount SET fyers_client_id = '' WHERE fyers_client_id IS NULL;",
            reverse_sql=migrations.RunSQL.noop,
        ),
        # Step 3: NOT NULL enforce karo (null=True hata do) - via AlterField
        migrations.AlterField(
            model_name="brokeraccount",
            name="fyers_client_id",
            field=models.CharField(
                max_length=20,
                blank=True,
                null=False,       # ← ab NOT NULL, safe hai kyunki sab rows fill ho gayi
                default="",
                db_index=True,
                help_text=(
                    "User ka Fyers client ID (e.g. 'YC00329'). "
                    "Auto-login aur multi-user account identification ke liye."
                ),
            ),
        ),
    ]
