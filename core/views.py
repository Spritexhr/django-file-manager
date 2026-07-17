import json
import logging
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
from django.db import IntegrityError, transaction
from django.db.models import Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST, require_http_methods

from .downloads import build_download_response
from .file_operations import (
    delete_file_record as _delete_stored_file_record,
    delete_folder_tree,
    delete_user_files,
    save_uploaded_files,
)
from .filesystem_sync import ensure_folder_directory, sync_directory
from .forms import FileUploadForm, FolderForm, NewUserForm
from .models import File, Folder


logger = logging.getLogger(__name__)


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
    name = file_obj.original_name or (
        os.path.basename(file_obj.file.name) if file_obj.file else ''
    )
    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    icon_type = 'file'
    icon_name = 'file'
    for kind, (exts, name_) in _ICON_MAP.items():
        if ext in exts:
            icon_type = kind
            icon_name = name_
            break
    try:
        if file_obj.size is None and file_obj.file:
            file_obj.size = file_obj.file.size
            type(file_obj).objects.filter(pk=file_obj.pk, size__isnull=True).update(
                size=file_obj.size
            )
        size = file_obj.size or 0
    except (OSError, ValueError):
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
        folder_form = FolderForm(
            request.POST,
            user=request.user,
            parent=current_folder,
        )

        if 'upload_file' in request.POST:
            if upload_form.is_valid():
                pos = _next_position(
                    File.objects.filter(folder=current_folder, uploaded_by=request.user)
                )
                try:
                    save_uploaded_files(
                        upload_form.cleaned_data['files'],
                        user=request.user,
                        folder=current_folder,
                        start_position=pos,
                    )
                except IntegrityError:
                    logger.warning(
                        'Upload path was indexed concurrently for user %s',
                        request.user.pk,
                    )
                    error = '文件已由共享存储同步，请刷新后重试'
                    messages.error(request, error)
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return JsonResponse(
                            {'success': False, 'errors': [error]},
                            status=409,
                        )
                    return redirect(request.path)
                except OSError:
                    logger.exception('Storage failure while user %s uploaded files', request.user.pk)
                    error = '文件存储暂不可用，请稍后重试'
                    messages.error(request, error)
                    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
                        return JsonResponse(
                            {'success': False, 'errors': [error]},
                            status=503,
                        )
                    return redirect(request.path)
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
                try:
                    new_folder.save()
                except IntegrityError:
                    messages.error(request, '同一目录下已存在同名文件夹')
                    return redirect(request.path)
                try:
                    ensure_folder_directory(new_folder)
                except OSError:
                    logger.exception(
                        'Unable to create storage directory for folder %s',
                        new_folder.pk,
                    )
                    new_folder.delete()
                    messages.error(request, '无法在共享存储中创建文件夹，请稍后重试')
                    return redirect(request.path)
                return redirect(request.path)
            for _, errs in folder_form.errors.items():
                for err in errs:
                    messages.error(request, err)
    else:
        upload_form = FileUploadForm()
        folder_form = FolderForm(user=request.user, parent=current_folder)

    if request.method == 'GET' and getattr(
        settings, 'FILESYSTEM_SYNC_ON_BROWSE', False
    ):
        try:
            # Browsing only imports/refreshes. Destructive pruning remains an
            # explicit `sync_samba --prune` maintenance action.
            sync_stats = sync_directory(request.user, current_folder, prune=False)
            if sync_stats['errors']:
                logger.warning(
                    'Shared storage sync for user %s completed with %s errors',
                    request.user.pk,
                    len(sync_stats['errors']),
                )
                messages.warning(
                    request,
                    '共享存储同步不完整，部分项目暂未更新',
                )
        except OSError:
            logger.exception(
                'Unable to synchronize shared storage for user %s', request.user.pk
            )
            messages.warning(request, '共享存储暂时无法同步，当前显示数据库中的内容')

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
            'url': reverse('download_file', args=[f.id]) if f.file else '',
            'size_human': f.size_human,
            'icon_type': f.icon_type,
            'icon_name': f.icon_name,
            'uploaded_at': f.uploaded_at,
        }
        for f in files
    ]

    try:
        total, used, free = shutil.disk_usage(settings.MEDIA_ROOT)
        disk_capacity = {
            'available': True,
            'label': getattr(settings, 'FILE_STORAGE_LABEL', '共享存储'),
            'total': _humanize_bytes(total),
            'used': _humanize_bytes(used),
            'free': _humanize_bytes(free),
            'percent_used': f'{(used / total) * 100:.1f}' if total else '0.0',
        }
    except OSError:
        logger.exception('Unable to read shared storage capacity')
        disk_capacity = {
            'available': False,
            'label': getattr(settings, 'FILE_STORAGE_LABEL', '共享存储'),
            'total': '',
            'used': '',
            'free': '',
            'percent_used': '0.0',
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
    _delete_stored_file_record(file_obj)


def _delete_folder_recursive(folder):
    delete_folder_tree(folder)


# Backwards-compat alias still imported elsewhere
delete_folder_recursive = _delete_folder_recursive


@login_required
@require_http_methods(['GET', 'HEAD'])
def download_file(request, file_id):
    """Stream an owned file without exposing MEDIA_ROOT or Samba credentials."""
    file_obj = get_object_or_404(File, id=file_id, uploaded_by=request.user)
    try:
        return build_download_response(request, file_obj)
    except FileNotFoundError as exc:
        raise Http404('文件不存在') from exc
    except OSError:
        logger.exception('Unable to open stored file %s for download', file_obj.pk)
        return HttpResponse('共享存储暂不可用，请稍后重试', status=503)


@login_required
@require_POST
def delete_file(request, file_id):
    file_obj = get_object_or_404(File, id=file_id, uploaded_by=request.user)
    folder_id = file_obj.folder_id
    name = file_obj.original_name or (
        os.path.basename(file_obj.file.name) if file_obj.file else 'file'
    )
    try:
        _delete_file_record(file_obj)
    except OSError:
        logger.exception('Unable to delete stored file %s', file_obj.pk)
        error = '共享存储暂不可用，文件尚未删除'
        messages.error(request, error)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error}, status=503)
        if folder_id:
            return redirect('file_manager_folder', folder_id=folder_id)
        return redirect('file_manager_root')
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
    try:
        _delete_folder_recursive(folder_obj)
    except OSError:
        logger.exception('Unable to delete storage folder %s', folder_obj.pk)
        error = '共享存储暂不可用，文件夹未能完整删除'
        messages.error(request, error)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse({'success': False, 'error': error}, status=503)
        return redirect(request.META.get('HTTP_REFERER') or 'file_manager_root')
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
    try:
        for fid in folder_ids:
            folder = get_object_or_404(Folder, id=fid, created_by=request.user)
            _delete_folder_recursive(folder)
            deleted += 1
        for fid in file_ids:
            obj = get_object_or_404(File, id=fid, uploaded_by=request.user)
            _delete_file_record(obj)
            deleted += 1
    except OSError:
        logger.exception('Bulk delete stopped after %s items', deleted)
        error = f'共享存储暂不可用，已删除 {deleted} 项，其余项目保留'
        messages.error(request, error)
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return JsonResponse(
                {'success': False, 'deleted': deleted, 'error': error},
                status=503,
            )
        return redirect(request.META.get('HTTP_REFERER') or 'file_manager_root')

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
    try:
        delete_user_files(target)
    except OSError:
        logger.exception('Unable to remove storage for user %s', target.pk)
        messages.error(request, '共享存储暂不可用，用户及其文件均未删除')
        return redirect('user_management')
    target.delete()
    messages.success(request, f'用户 "{username}" 已删除')
    return redirect('user_management')
