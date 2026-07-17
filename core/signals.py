import logging

from django.db import transaction
from django.db.models.signals import post_delete
from django.dispatch import receiver

from .models import File


logger = logging.getLogger(__name__)


@receiver(post_delete, sender=File)
def delete_file_object_after_commit(sender, instance, **kwargs):
    """Cover cascades/admin deletes that do not use the view service.

    Normal view deletion removes the object before the row, so this callback is
    idempotent. For cascades it runs only after the database transaction commits.
    """
    if not instance.file or not instance.file.name:
        return

    storage = instance.file.storage
    name = instance.file.name

    def cleanup():
        try:
            storage.delete(name)
        except Exception:
            logger.exception('Unable to delete stored object after row deletion: %s', name)

    transaction.on_commit(cleanup)
