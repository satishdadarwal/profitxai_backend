# apps/brokers/migrations/0012_multi_user_sebi_compliance.py
#
# Migration: BrokerAccount pe compound indexes add karo
#
# Problem ye tha ki 0011 ne sirf fyers_client_id field add kiya,
# lekin multi-user lookup ke liye compound indexes nahi the.
#
# Ye indexes add hone ke baad:
#   - auto_refresh_fyers_tokens: (user, broker, fyers_client_id) se fast lookup
#   - factory._get_adapter_for_user: (user, broker, is_active, is_verified) fast
#   - FyersAutoLoginView: fyers_client_id match fast hoga
#
# NOTE: model ke Meta.indexes mein manually add karo bhi (niche dekho)
# Run: python manage.py migrate brokers 0012

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("brokers", "0011_brokeraccount_fyers_client_id"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        # Index 1: (user, broker, fyers_client_id)
        # FyersAutoLoginView + auto_refresh ke fyers_client_id lookups ke liye
        migrations.AddIndex(
            model_name="brokeraccount",
            index=models.Index(
                fields=["user", "broker", "fyers_client_id"],
                name="ba_user_broker_client_idx",
            ),
        ),
        # Index 2: (user, broker, is_active, is_verified)
        # Factory._get_adapter_for_user() ke liye — ye query har order pe chalti hai
        migrations.AddIndex(
            model_name="brokeraccount",
            index=models.Index(
                fields=["user", "broker", "is_active", "is_verified"],
                name="ba_user_broker_active_idx",
            ),
        ),
    ]