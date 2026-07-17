from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q
from django.db.models.functions import Lower


def _validate_storage_component(value, *, label):
    """Return a path component that cannot escape its Samba user directory."""
    value = str(value or '')
    if (
        not value
        or value in ('.', '..')
        or len(value) > 255
        or any(char in value for char in ('/', '\\', '\x00', '\r', '\n'))
    ):
        raise ValueError(f'Invalid {label}: {value!r}')
    return value

class Folder(models.Model):
    name = models.CharField(max_length=255)
    # The 'self' reference creates the nested structure
    parent = models.ForeignKey('self', on_delete=models.CASCADE, null=True, blank=True, related_name='subfolders')
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.CASCADE)
    # User-defined manual ordering (drag-and-drop). Lower = earlier.
    position = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ['position']
        constraints = [
            models.UniqueConstraint(
                Lower('name'),
                'created_by',
                condition=Q(parent__isnull=True),
                name='uniq_root_folder_name_ci',
            ),
            models.UniqueConstraint(
                Lower('name'),
                'created_by',
                'parent',
                condition=Q(parent__isnull=False),
                name='uniq_child_folder_name_ci',
            ),
        ]

    def __str__(self):
        return self.name

class File(models.Model):
    def get_upload_path(instance, filename):
        """Mirror the database folder tree below ``MEDIA_ROOT/user_<id>``.

        FileField storage names always use forward slashes, including when the
        Django process runs on Windows.  The ownership and cycle checks keep a
        malformed database row from directing an upload into another user's
        Samba-visible tree.
        """
        user_id = instance.uploaded_by_id
        if not user_id:
            raise ValueError('uploaded_by must be set before saving a file')

        parts = []
        folder = instance.folder
        seen = set()
        while folder is not None:
            if folder.pk and folder.pk in seen:
                raise ValueError('Folder hierarchy contains a cycle')
            if folder.pk:
                seen.add(folder.pk)
            if folder.created_by_id != user_id:
                raise ValueError('Folder and file must belong to the same user')
            parts.append(_validate_storage_component(folder.name, label='folder name'))
            folder = folder.parent

        safe_name = _validate_storage_component(filename, label='file name')
        parts.reverse()
        return '/'.join([f'user_{user_id}', *parts, safe_name])

    file = models.FileField(upload_to=get_upload_path, max_length=1024, unique=True)
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='files',null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    original_name = models.CharField(max_length=255, blank=True, default='')
    size = models.PositiveBigIntegerField(null=True, blank=True)
    content_type = models.CharField(
        max_length=255,
        blank=True,
        default='application/octet-stream',
    )
    # User-defined manual ordering (drag-and-drop). Lower = earlier.
    position = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ['position']

    def __str__(self):
        if self.original_name:
            return self.original_name
        return self.file.name.replace('\\', '/').rsplit('/', 1)[-1]
