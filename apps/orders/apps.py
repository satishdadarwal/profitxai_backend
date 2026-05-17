from django.apps import AppConfig


class OrdersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.orders'  # ✅ Should be 'apps.orders'
    verbose_name = 'Orders'
    
    def ready(self):
        import apps.orders.signals  # noqa