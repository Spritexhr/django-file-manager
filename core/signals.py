import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import File


logger = logging.getLogger(__name__)


@receiver(post_delete, sender=File)
def delete_file_object_after_commit(sender, instance, **kwargs):
    """Delete storage only after the database deletion commits successfully."""
    if not instance.file or not instance.file.name:
        return

    storage = instance.file.storage
    name = instance.file.name

    def cleanup():
        try:
            storage.delete(name)
        except Exception:
            logger.exception('Unable to delete stored object after commit: %s', name)

    transaction.on_commit(cleanup)
