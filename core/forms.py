import os

from django import forms
from django.core.exceptions import ValidationError

from .models import Folder

# Whitelist of allowed file extensions. SVG and HTML are intentionally excluded
# because they can carry script payloads when served from MEDIA_URL.
ALLOWED_EXTENSIONS = {
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'ico',
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx',
    'txt', 'md', 'csv', 'rtf', 'log',
    'zip', 'rar', '7z', 'tar', 'gz', 'bz2',
    'mp3', 'wav', 'ogg', 'flac', 'm4a',
    'mp4', 'avi', 'mov', 'mkv', 'webm',
}

MAX_FILE_SIZE = 50 * 1024 * 1024
MAX_BATCH_SIZE = 200 * 1024 * 1024
MAX_FILES_PER_REQUEST = 20
MAX_FILENAME_LENGTH = 200


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput())
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_file_clean = super().clean
        if isinstance(data, (list, tuple)):
            result = [single_file_clean(d, initial) for d in data]
        else:
            result = single_file_clean(data, initial)
        return result


class FileUploadForm(forms.Form):
    files = MultipleFileField(label='Select files', required=False)

    def clean(self):
        cleaned = super().clean()
        files = self.files.getlist('files') if self.files else []

        if not files:
            raise ValidationError('请选择至少一个文件')

        if len(files) > MAX_FILES_PER_REQUEST:
            raise ValidationError(
                f'一次最多上传 {MAX_FILES_PER_REQUEST} 个文件,当前 {len(files)} 个'
            )

        total_size = 0
        for f in files:
            name = f.name or ''
            if len(name) > MAX_FILENAME_LENGTH:
                raise ValidationError(f'文件名过长: {name[:40]}...')
            if any(c in name for c in ('\x00', '/', '\\')) or name in ('.', '..'):
                raise ValidationError(f'文件名包含非法字符: {name}')

            ext = os.path.splitext(name)[1].lower().lstrip('.')
            if not ext or ext not in ALLOWED_EXTENSIONS:
                raise ValidationError(f'不允许的文件类型: {name}')

            if f.size > MAX_FILE_SIZE:
                raise ValidationError(
                    f'{name} 超过单文件大小上限 {MAX_FILE_SIZE // 1024 // 1024}MB'
                )
            total_size += f.size

        if total_size > MAX_BATCH_SIZE:
            raise ValidationError(
                f'本批次总大小超过 {MAX_BATCH_SIZE // 1024 // 1024}MB'
            )

        cleaned['files'] = files
        return cleaned


class FolderForm(forms.ModelForm):
    class Meta:
        model = Folder
        fields = ['name']

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError('文件夹名不能为空')
        if any(c in name for c in ('/', '\\', '\x00')) or name in ('.', '..'):
            raise ValidationError('文件夹名包含非法字符')
        return name
