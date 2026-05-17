from django.apps import AppConfig


class WalletConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.wallet"
    verbose_name = "Wallet"

    def ready(self):
        """App ready — signals import hote hain yahan."""
        pass
