import logging
import mimetypes
import os

from django.db import transaction

from .filesystem_sync import remove_folder_directory, user_root_path
from .models import File


logger = logging.getLogger(__name__)


def _safe_content_type(filename):
    value, _ = mimetypes.guess_type(filename)
    if not value or '\r' in value or '\n' in value:
        return 'application/octet-stream'
    return value


def save_uploaded_files(files, *, user, folder, start_position):
    """Persist one validated upload batch with storage compensation on failure."""
    stored_objects = []
    created = []
    try:
        with transaction.atomic():
            position = start_position
            for uploaded in files:
                record = File(
                    folder=folder,
                    uploaded_by=user,
                    position=position,
                    original_name=uploaded.name,
                    size=uploaded.size,
                    content_type=_safe_content_type(uploaded.name),
                )
                # Calling FieldFile.save explicitly lets us remember the final
                # collision-safe object name before the database insert.
                record.file.save(uploaded.name, uploaded, save=False)
                stored_objects.append((record.file.storage, record.file.name))
                record.save(force_insert=True)
                created.append(record)
                position += 1
    except Exception:
        # A database transaction cannot roll back filesystem/SMB writes. Remove
        # every object written by this batch before surfacing the original error.
        for storage, name in reversed(stored_objects):
            try:
                # A concurrent Samba reconciliation may already have indexed
                # the object. In that case it now owns the path and deleting it
                # would leave the winning database row orphaned.
                if not File.objects.filter(file=name).exists():
                    storage.delete(name)
            except Exception:
                logger.exception('Unable to compensate failed upload for %s', name)
        raise
    return created


def delete_file_record(file_obj):
    """Delete the stored object first, then its database row."""
    if file_obj.file and file_obj.file.name:
        file_obj.file.storage.delete(file_obj.file.name)
    file_obj.delete()


def delete_folder_tree(folder):
    """Delete a logical folder and all legacy or materialized files below it."""
    for file_obj in list(folder.files.all()):
        delete_file_record(file_obj)
    for child in list(folder.subfolders.all()):
        delete_folder_tree(child)
    remove_folder_directory(folder)
    folder.delete()


def delete_user_files(user):
    """Remove all objects owned by a user, including legacy flat files."""
    root = user_root_path(user.id)
    if os.path.lexists(root):
        if root.is_symlink() or not root.is_dir():
            raise OSError(f'用户存储根目录不安全: {root}')
        # Remove the complete user boundary first. This includes unindexed
        # Samba-side content and avoids leaving a half-deleted DB tree when the
        # shared filesystem rejects the operation.
        import shutil
        shutil.rmtree(root)

    # Cascades and the post-delete hook are now harmless/idempotent because all
    # valid object names live below the removed user root.
    File.objects.filter(uploaded_by=user).delete()
