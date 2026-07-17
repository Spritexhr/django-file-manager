import os

from django import forms
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError

from .models import Folder

# Whitelist of allowed file extensions.
# SVG and HTML are intentionally excluded as defense in depth: even though
# downloads are authenticated, these formats can carry active script payloads.
ALLOWED_EXTENSIONS = {
    # Images
    'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'ico', 'tiff', 'tif',
    # Documents
    'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp',
    # Plain text / markup
    'txt', 'md', 'rst', 'csv', 'rtf', 'log',
    # Code
    'py', 'pyw', 'sh', 'bash', 'zsh', 'fish',
    'js', 'ts', 'tsx', 'jsx', 'vue',
    'css', 'scss', 'sass', 'less',
    'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'h', 'hpp',
    'rb', 'php', 'pl', 'lua', 'r',
    'sql', 'ipynb',
    # Config / data
    'json', 'jsonc', 'yaml', 'yml', 'toml', 'xml',
    'ini', 'cfg', 'conf', 'env', 'properties',
    'dockerfile', 'makefile', 'gitignore', 'editorconfig',
    # Archives
    'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'zst',
    # Audio
    'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'opus',
    # Video
    'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv', 'wmv',
}

# Sourced from settings (env-configurable) so large uploads can be enabled without
# touching code. Falls back to generous defaults if settings are absent.
MAX_FILE_SIZE = getattr(settings, 'MAX_UPLOAD_FILE_SIZE', 5 * 1024 * 1024 * 1024)
MAX_BATCH_SIZE = getattr(settings, 'MAX_UPLOAD_BATCH_SIZE', 20 * 1024 * 1024 * 1024)
MAX_FILES_PER_REQUEST = getattr(settings, 'MAX_UPLOAD_FILES', 20)
MAX_FILENAME_LENGTH = 200

WINDOWS_FORBIDDEN_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED_NAMES = {
    'con', 'prn', 'aux', 'nul',
    *(f'com{i}' for i in range(1, 10)),
    *(f'lpt{i}' for i in range(1, 10)),
}


def _validate_portable_name(name, *, label='文件名'):
    """Reject names that cannot be represented safely on an SMB share."""
    if (
        not name
        or name in ('.', '..')
        or name[-1] in (' ', '.')
        or any(ord(char) < 32 or char in WINDOWS_FORBIDDEN_CHARS for char in name)
    ):
        raise ValidationError(f'{label}包含 SMB/Windows 不支持的字符: {name}')

    # Windows reserves these basenames even when an extension is present.
    stem = name.split('.', 1)[0].rstrip(' .').lower()
    if stem in WINDOWS_RESERVED_NAMES:
        raise ValidationError(f'{label}是 SMB/Windows 保留名称: {name}')


def _fmt_limit(num_bytes):
    """Human-friendly size for error messages (GB once we cross 1GB, else MB)."""
    gb = num_bytes / (1024 ** 3)
    if gb >= 1:
        return f'{gb:.0f}GB' if gb == int(gb) else f'{gb:.1f}GB'
    return f'{num_bytes // (1024 * 1024)}MB'


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
            _validate_portable_name(name)

            ext = os.path.splitext(name)[1].lower().lstrip('.')
            # Allow well-known extensionless files (Dockerfile, Makefile, etc.)
            bare = name.lower()
            if ext:
                allowed = ext in ALLOWED_EXTENSIONS
            else:
                allowed = bare in {'dockerfile', 'makefile', 'vagrantfile',
                                   '.gitignore', '.dockerignore', '.editorconfig',
                                   '.env', '.envrc', 'procfile', 'gemfile', 'rakefile'}
            if not allowed:
                raise ValidationError(f'不允许的文件类型: {name}')

            if f.size > MAX_FILE_SIZE:
                raise ValidationError(
                    f'{name} 超过单文件大小上限 {_fmt_limit(MAX_FILE_SIZE)}'
                )
            total_size += f.size

        if total_size > MAX_BATCH_SIZE:
            raise ValidationError(
                f'本批次总大小超过 {_fmt_limit(MAX_BATCH_SIZE)}'
            )

        cleaned['files'] = files
        return cleaned


class FolderForm(forms.ModelForm):
    def __init__(self, *args, user=None, parent=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.parent = parent

    class Meta:
        model = Folder
        fields = ['name']

    def clean_name(self):
        name = (self.cleaned_data.get('name') or '').strip()
        if not name:
            raise ValidationError('文件夹名不能为空')
        _validate_portable_name(name, label='文件夹名')
        if self.user and Folder.objects.filter(
            created_by=self.user,
            parent=self.parent,
            name__iexact=name,
        ).exclude(pk=self.instance.pk).exists():
            raise ValidationError('同一目录下已存在同名文件夹')
        return name


class NewUserForm(forms.Form):
    """Create a login account from the staff-only user management page."""
    username = forms.CharField(max_length=150)
    email = forms.EmailField(required=False)
    password = forms.CharField()
    is_staff = forms.BooleanField(required=False)

    def clean_username(self):
        username = (self.cleaned_data.get('username') or '').strip()
        if not username:
            raise ValidationError('用户名不能为空')
        UnicodeUsernameValidator(
            message='用户名只能包含字母、数字以及 @/./+/-/_ 字符'
        )(username)
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError('该用户名已被占用')
        return username

    def clean_password(self):
        password = self.cleaned_data.get('password') or ''
        # Run Django's configured password validators; give the similarity
        # validator a stand-in user so it can compare against username/email.
        probe = User(
            username=self.cleaned_data.get('username', ''),
            email=self.cleaned_data.get('email', ''),
        )
        validate_password(password, probe)
        return password
