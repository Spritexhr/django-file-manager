import re
import unicodedata

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils.text import slugify


FOLDER_NAME_MAX_LENGTH = 100
_INVALID_FOLDER_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')
_RESERVED_FOLDER_NAMES = {
    'CON', 'PRN', 'AUX', 'NUL',
    *(f'COM{index}' for index in range(1, 10)),
    *(f'LPT{index}' for index in range(1, 10)),
}


def normalize_folder_name(value):
    """Return a stable, human-readable virtual folder name."""
    normalized = unicodedata.normalize('NFKC', str(value or ''))
    return re.sub(r'\s+', ' ', normalized).strip()


def validate_folder_name(value):
    name = normalize_folder_name(value)
    if not name:
        raise ValidationError('文件夹名称不能为空')
    if len(name) > FOLDER_NAME_MAX_LENGTH:
        raise ValidationError(f'文件夹名称不能超过 {FOLDER_NAME_MAX_LENGTH} 个字符')
    if name in ('.', '..') or name.startswith('.') or name.endswith('.'):
        raise ValidationError('文件夹名称不能以句点开头或结尾')
    if _INVALID_FOLDER_NAME_CHARS.search(name):
        raise ValidationError('文件夹名称不能包含 \\ / : * ? " < > | 字符')
    if any(unicodedata.category(char) in ('Cc', 'Cf') for char in name):
        raise ValidationError('文件夹名称不能包含控制字符或隐藏格式字符')
    if name.split('.', 1)[0].upper() in _RESERVED_FOLDER_NAMES:
        raise ValidationError('该名称是系统保留名称，请使用其他名称')
    return name


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
        # ModelForm excludes a field from model validation after its field
        # cleaner has already failed. Skip an empty placeholder here so users
        # receive the original naming error only once.
        if self.name:
            try:
                self.name = validate_folder_name(self.name)
            except ValidationError as exc:
                raise ValidationError({'name': exc.messages}) from exc

        if self.created_by_id:
            duplicate = Folder.objects.filter(
                created_by_id=self.created_by_id,
                parent_id=self.parent_id,
                name__iexact=self.name,
            )
            if self.pk:
                duplicate = duplicate.exclude(pk=self.pk)
            if duplicate.exists():
                raise ValidationError({'name': '同一目录下已存在同名文件夹'})

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

    def get_hierarchical_path(self):
        segments = []
        current = self
        seen = set()
        while current is not None and current.pk not in seen:
            seen.add(current.pk)
            readable = slugify(current.name, allow_unicode=True)[:60] or 'folder'
            segments.append(f'{current.pk}-{readable}')
            current = current.parent
        return '/'.join(reversed(segments))

    def get_absolute_url(self):
        return reverse(
            'file_manager_folder',
            kwargs={'folder_path': self.get_hierarchical_path()},
        )

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
