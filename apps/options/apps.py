from django.apps import AppConfig


class OptionsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.options"

    def ready(self):
        import apps.options.signals  # noqa: F401
