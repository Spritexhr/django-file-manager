from pathlib import Path

from django.apps import AppConfig
from django.conf import settings


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        Path(settings.MEDIA_ROOT).mkdir(parents=True, exist_ok=True)
        from . import signals  # noqa: F401
