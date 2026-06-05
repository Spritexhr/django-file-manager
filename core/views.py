import json
import os
import shutil
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Max
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import FileUploadForm, FolderForm, NewUserForm
from .models import File, Folder


def staff_required(view):
    """Gate a view to authenticated staff ('管理人员'). Anonymous users are sent to
    login; logged-in non-staff users get a 403 rather than a redirect loop."""
    @wraps(view)
    @login_required
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_staff:
            raise PermissionDenied('需要管理员权限')
        return view(request, *args, **kwargs)
    return _wrapped


def _next_position(queryset):
    """Return max(position)+1 within a scope so new items append at the end."""
    return (queryset.aggregate(m=Max('position'))['m'] or 0) + 1


_ICON_MAP = {
    'image': ({'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'ico', 'tiff', 'tif'}, 'file-image'),
    'video': ({'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv', 'wmv'}, 'file-video'),
    'audio': ({'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'opus'}, 'file-audio'),
    'archive': ({'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'zst'}, 'file-archive'),
    'text': ({'txt', 'md', 'rst', 'csv', 'rtf', 'log',
              'pdf', 'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx', 'odt', 'ods', 'odp',
              'py', 'pyw', 'sh', 'bash', 'zsh', 'fish',
              'js', 'ts', 'tsx', 'jsx', 'vue',
              'css', 'scss', 'sass', 'less',
              'go', 'rs', 'java', 'kt', 'swift', 'c', 'cpp', 'h', 'hpp', 'rb', 'php', 'pl', 'lua', 'r',
              'sql', 'ipynb',
              'json', 'jsonc', 'yaml', 'yml', 'toml', 'xml',
              'ini', 'cfg', 'conf', 'env', 'properties',
              'dockerfile', 'makefile', 'gitignore', 'editorconfig'}, 'file-text'),
}


def _humanize_bytes(n):
    if n is None:
        return ''
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    n = float(n)
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f'{n:.1f} {units[i]}' if i > 0 and n < 10 else f'{n:.0f} {units[i]}'


def _decorate_file(file_obj):
    name = os.path.basename(file_obj.file.name) if file_obj.file else ''
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    icon_type = 'file'
    icon_name = 'file'
    for kind, (exts, name_) in _ICON_MAP.items():
        if ext in exts:
            icon_type = kind
            icon_name = name_
            break
    try:
        size = file_obj.file.size if file_obj.file else 0
    except (FileNotFoundError, ValueError):
        size = 0
    file_obj.display_name = name
    file_obj.size_human = _humanize_bytes(size)
    file_obj.icon_type = icon_type
    file_obj.icon_name = icon_name
    return file_obj


@login_required
def file_manager(request, folder_id=None):
    if folder_id:
        current_folder = get_object_or_404(Folder, id=folder_id, created_by=request.user)
        breadcrumbs = []
        parent = current_folder
        seen = set()
        while parent and parent.id not in seen:
            seen.add(parent.id)
            breadcrumbs.append(parent)
            parent = parent.parent
        breadcrumbs.reverse()
    else:
        current_folder = None
        breadcrumbs = []

    if request.method == 'POST':
        upload_form = FileUploadForm(request.POST, request.FILES)
        folder_form = FolderForm(request.POST)

        if 'upload_file' in request.POST:
            if upload_form.is_valid():
                pos = _next_position(
                    File.objects.filter(folder=current_folder, uploaded_by=request.user)
                )
                for f in upload_form.cleaned_data['files']:
                    File.objects.create(
                        file=f,
                        folder=current_folder,
                        uploaded_by=request.user,
                        position=pos,
                    )
                    pos += 1
                if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                    return JsonResponse({'success': True})
                return redirect(request.path)
            for error in upload_form.non_field_errors():
                messages.error(request, error)
            if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                return JsonResponse(
                    {'success': False, 'errors': list(upload_form.non_field_errors())},
                    status=400,
                )

        if 'create_folder' in request.POST:
            if folder_form.is_valid():
                new_folder = folder_form.save(commit=False)
                new_folder.parent = current_folder
                new_folder.created_by = request.user
                new_folder.position = _next_position(
                    Folder.objects.filter(parent=current_folder, created_by=request.user)
                )
                new_folder.save()
                return redirect(request.path)
            for _, errs in folder_form.errors.items():
                for err in errs:
                    messages.error(request, err)
    else:
        upload_form = FileUploadForm()
        folder_form = FolderForm()

    # Manual (position) order is the source of truth; name/date sorting is done
    # client-side as a non-destructive view.
    if current_folder:
        subfolders = current_folder.subfolders.filter(created_by=request.user).order_by('position', 'name')
        files = current_folder.files.filter(uploaded_by=request.user).order_by('position', '-uploaded_at')
    else:
        subfolders = Folder.objects.filter(parent__isnull=True, created_by=request.user).order_by('position', 'name')
        files = File.objects.filter(folder__isnull=True, uploaded_by=request.user).order_by('position', '-uploaded_at')

    files = [_decorate_file(f) for f in files]

    folders_json = [
        {
            'id': f.id,
            'name': f.name,
            'url': reverse('file_manager_folder', args=[f.id]),
            'created_at': f.created_at,
        }
        for f in subfolders
    ]
    files_json = [
        {
            'id': f.id,
            'name': f.display_name,
            'url': f.file.url if f.file else '',
            'size_human': f.size_human,
            'icon_type': f.icon_type,
            'icon_name': f.icon_name,
            'uploaded_at': f.uploaded_at,
        }
        for f in files
    ]

    total, used, free = shutil.disk_usage(settings.MEDIA_ROOT)
    disk_capacity = {
        'total': _humanize_bytes(total),
        'used': _humanize_bytes(used),
        'free': _humanize_bytes(free),
        'percent_used': f'{(used / total) * 100:.1f}',
    }

    return render(request, 'core/file_manager.html', {
        'current_folder': current_folder,
        'subfolders': subfolders,
        'files': files,
        'folders_json': json.dumps(folders_json, cls=DjangoJSONEncoder),
        'files_json': json.dumps(files_json, cls=DjangoJSONEncoder),
        'upload_form': upload_form,
        'folder_form': folder_form,
        'breadcrumbs': breadcrumbs,
        'disk_capacity': disk_capacity,
    })


def _delete_file_record(file_obj):
    if file_obj.file:
        try:
            path = file_obj.file.path
            if os.path.exists(path):
                os.remove(path)
        except (FileNotFoundError, ValueError):
            pass
    file_obj.delete()


def _delete_folder_recursive(folder):
    for f in folder.files.all():
        _delete_file_record(f)
    for sub in folder.subfolders.all():
        _delete_folder_recursive(sub)
    folder.delete()


# Backwards-compat alias still imported elsewhere
delete_folder_recursive = _delete_folder_recursive


@login_required
@require_POST
def delete_file(request, file_id):
    file_obj = get_object_or_404(File, id=file_id, uploaded_by=request.user)
    folder_id = file_obj.folder_id
    name = os.path.basename(file_obj.file.name) if file_obj.file else 'file'
    with transaction.atomic():
        _delete_file_record(file_obj)
    messages.success(request, f'文件 "{name}" 已删除')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    if folder_id:
        return redirect('file_manager_folder', folder_id=folder_id)
    return redirect('file_manager_root')


@login_required
@require_POST
def delete_folder(request, folder_id):
    folder_obj = get_object_or_404(Folder, id=folder_id, created_by=request.user)
    name = folder_obj.name
    parent_id = folder_obj.parent_id
    with transaction.atomic():
        _delete_folder_recursive(folder_obj)
    messages.success(request, f'文件夹 "{name}" 及其内容已删除')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True})
    if parent_id:
        return redirect('file_manager_folder', folder_id=parent_id)
    return redirect('file_manager_root')


@login_required
@require_POST
def bulk_delete(request):
    folder_ids = request.POST.getlist('folder_ids[]')
    file_ids = request.POST.getlist('file_ids[]')

    deleted = 0
    with transaction.atomic():
        for fid in folder_ids:
            folder = get_object_or_404(Folder, id=fid, created_by=request.user)
            _delete_folder_recursive(folder)
            deleted += 1
        for fid in file_ids:
            obj = get_object_or_404(File, id=fid, uploaded_by=request.user)
            _delete_file_record(obj)
            deleted += 1

    messages.success(request, f'已删除 {deleted} 项')
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return JsonResponse({'success': True, 'deleted': deleted})
    return redirect(request.META.get('HTTP_REFERER') or 'file_manager_root')


@login_required
@require_POST
def reorder(request):
    """Persist a drag-and-drop ordering. Accepts kind=folder|file and ids[] in
    the desired order; sets each item's position to its index. Scoped to the
    current user so foreign ids are silently ignored."""
    kind = request.POST.get('kind')
    ids = request.POST.getlist('ids[]')

    if kind == 'folder':
        model, owner = Folder, {'created_by': request.user}
    elif kind == 'file':
        model, owner = File, {'uploaded_by': request.user}
    else:
        return JsonResponse({'success': False, 'error': 'invalid kind'}, status=400)

    # Only ids that actually belong to the user get updated.
    owned = set(model.objects.filter(id__in=ids, **owner).values_list('id', flat=True))
    with transaction.atomic():
        for index, raw_id in enumerate(ids):
            try:
                obj_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if obj_id in owned:
                model.objects.filter(id=obj_id).update(position=index)

    return JsonResponse({'success': True})


# ── User management (staff only) ─────────────────────────────────────────────

def _can_modify(actor, target):
    """A staff member may not touch a superuser unless they are one themselves."""
    return actor.is_superuser or not target.is_superuser


@staff_required
def user_management(request):
    users = User.objects.order_by('-is_active', 'username')
    users_json = [
        {
            'id': u.id,
            'username': u.username,
            'email': u.email,
            'is_active': u.is_active,
            'is_staff': u.is_staff,
            'is_superuser': u.is_superuser,
            'is_self': u.id == request.user.id,
            'date_joined': u.date_joined,
            'last_login': u.last_login,
        }
        for u in users
    ]
    return render(request, 'core/user_management.html', {
        'users_json': json.dumps(users_json, cls=DjangoJSONEncoder),
        'is_superuser': request.user.is_superuser,
    })


@staff_required
@require_POST
def user_create(request):
    form = NewUserForm(request.POST)
    if not form.is_valid():
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)
        return redirect('user_management')

    user = User.objects.create_user(
        username=form.cleaned_data['username'],
        email=form.cleaned_data.get('email', ''),
        password=form.cleaned_data['password'],
    )
    # Only a superuser may mint another admin, to prevent privilege escalation.
    if request.user.is_superuser and form.cleaned_data.get('is_staff'):
        user.is_staff = True
        user.save(update_fields=['is_staff'])
    messages.success(request, f'用户 "{user.username}" 已创建')
    return redirect('user_management')


@staff_required
@require_POST
def user_set_password(request, user_id):
    target = get_object_or_404(User, id=user_id)
    if not _can_modify(request.user, target):
        messages.error(request, '无权修改该用户的密码')
        return redirect('user_management')

    password = request.POST.get('password', '')
    try:
        validate_password(password, target)
    except ValidationError as exc:
        for msg in exc.messages:
            messages.error(request, msg)
        return redirect('user_management')

    target.set_password(password)
    target.save(update_fields=['password'])
    # Don't log the admin out if they just changed their own password.
    if target.id == request.user.id:
        update_session_auth_hash(request, target)
    messages.success(request, f'用户 "{target.username}" 的密码已更新')
    return redirect('user_management')


@staff_required
@require_POST
def user_toggle_active(request, user_id):
    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        messages.error(request, '不能停用自己的账户')
        return redirect('user_management')
    if not _can_modify(request.user, target):
        messages.error(request, '无权操作该用户')
        return redirect('user_management')

    target.is_active = not target.is_active
    target.save(update_fields=['is_active'])
    messages.success(
        request,
        f'用户 "{target.username}" 已{"启用" if target.is_active else "停用"}',
    )
    return redirect('user_management')


@staff_required
@require_POST
def user_delete(request, user_id):
    target = get_object_or_404(User, id=user_id)
    if target.id == request.user.id:
        messages.error(request, '不能删除自己的账户')
        return redirect('user_management')
    if target.is_superuser:
        messages.error(request, '不能删除超级管理员账户')
        return redirect('user_management')

    username = target.username
    # Folder/File rows cascade with the user; clear their media directory too so
    # uploaded files aren't left orphaned on disk.
    user_media = os.path.join(settings.MEDIA_ROOT, f'user_{target.id}')
    target.delete()
    if os.path.isdir(user_media):
        shutil.rmtree(user_media, ignore_errors=True)
    messages.success(request, f'用户 "{username}" 已删除')
    return redirect('user_management')
