from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # Import lifecycle hooks without touching the shared filesystem or
        # requiring Samba to be available during application startup.
        from . import signals  # noqa: F401
