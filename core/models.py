from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q


def _validate_storage_component(value, *, label):
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
        ordering = ['position', 'id']
        constraints = [
            models.CheckConstraint(
                condition=~Q(id=models.F('parent_id')),
                name='folder_cannot_parent_itself',
            ),
            models.UniqueConstraint(
                fields=['created_by', 'position'],
                condition=Q(parent__isnull=True),
                name='uniq_root_folder_position',
            ),
            models.UniqueConstraint(
                fields=['created_by', 'parent', 'position'],
                condition=Q(parent__isnull=False),
                name='uniq_child_folder_position',
            ),
        ]

    def clean(self):
        super().clean()
        if not self.parent_id:
            return
        if self.created_by_id and self.parent.created_by_id != self.created_by_id:
            raise ValidationError({'parent': '父文件夹必须属于同一用户'})

        seen = set()
        ancestor = self.parent
        while ancestor is not None:
            if ancestor.pk == self.pk or ancestor.pk in seen:
                raise ValidationError({'parent': '文件夹层级不能形成循环'})
            seen.add(ancestor.pk)
            ancestor = ancestor.parent

    def __str__(self):
        return self.name

class File(models.Model):
    # Use a function to define the upload path dynamically
    def get_upload_path(instance, filename):
        user_id = instance.uploaded_by_id
        if not user_id:
            raise ValueError('uploaded_by must be set before saving a file')
        if instance.folder_id and instance.folder.created_by_id != user_id:
            raise ValueError('Folder and file must belong to the same user')
        safe_name = _validate_storage_component(filename, label='file name')
        return f'user_{user_id}/{safe_name}'

    file = models.FileField(upload_to=get_upload_path)
    folder = models.ForeignKey(Folder, on_delete=models.CASCADE, related_name='files',null=True, blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.CASCADE)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    # User-defined manual ordering (drag-and-drop). Lower = earlier.
    position = models.PositiveIntegerField(default=0, db_index=True)

    class Meta:
        ordering = ['position', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['uploaded_by', 'position'],
                condition=Q(folder__isnull=True),
                name='uniq_root_file_position',
            ),
            models.UniqueConstraint(
                fields=['uploaded_by', 'folder', 'position'],
                condition=Q(folder__isnull=False),
                name='uniq_child_file_position',
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.folder_id
            and self.uploaded_by_id
            and self.folder.created_by_id != self.uploaded_by_id
        ):
            raise ValidationError({'folder': '文件夹与文件必须属于同一用户'})

    def __str__(self):
        return self.file.name.replace('\\', '/').rsplit('/', 1)[-1]
