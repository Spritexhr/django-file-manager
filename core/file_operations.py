import logging

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Max, Q

from .models import File, Folder


logger = logging.getLogger(__name__)


def _next_position(queryset):
    return (queryset.aggregate(value=Max('position'))['value'] or 0) + 1


def save_uploaded_files(files, *, user, folder):
    """Save one upload batch atomically and compensate storage on failure."""
    stored_objects = []
    created = []
    try:
        with transaction.atomic():
            # Serializes position allocation on databases that support row locks.
            User.objects.select_for_update().get(pk=user.pk)
            position = _next_position(
                File.objects.filter(folder=folder, uploaded_by=user)
            )
            for uploaded in files:
                record = File(
                    folder=folder,
                    uploaded_by=user,
                    position=position,
                )
                record.file.save(uploaded.name, uploaded, save=False)
                stored_objects.append((record.file.storage, record.file.name))
                record.full_clean(exclude=['file'])
                record.save(force_insert=True)
                created.append(record)
                position += 1
    except Exception:
        # Database transactions cannot roll back filesystem writes.
        for storage, name in reversed(stored_objects):
            try:
                if not File.objects.filter(file=name).exists():
                    storage.delete(name)
            except Exception:
                logger.exception('Unable to compensate failed upload for %s', name)
        raise
    return created


def collect_owned_folder_tree(root_ids, *, user):
    """Collect descendants iteratively while enforcing a single owner boundary."""
    seen = set()
    frontier = set(root_ids)
    while frontier:
        rows = list(
            Folder.objects.filter(id__in=frontier).values('id', 'created_by_id')
        )
        if len(rows) != len(frontier):
            raise PermissionDenied('文件夹不存在或无权访问')
        if any(row['created_by_id'] != user.id for row in rows):
            raise PermissionDenied('不能操作其他用户的文件夹')

        current = {row['id'] for row in rows}
        seen.update(current)
        child_rows = list(
            Folder.objects.filter(parent_id__in=current).values('id', 'created_by_id')
        )
        if any(row['created_by_id'] != user.id for row in child_rows):
            raise PermissionDenied('目录树包含其他用户的数据')
        frontier = {row['id'] for row in child_rows} - seen
    return seen


def delete_owned_items(*, user, folder_ids=(), file_ids=()):
    """Validate a complete delete request before changing rows or storage."""
    folder_ids = set(folder_ids)
    file_ids = set(file_ids)

    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)

        owned_folders = set(
            Folder.objects.filter(id__in=folder_ids, created_by=user)
            .values_list('id', flat=True)
        )
        owned_files = set(
            File.objects.filter(id__in=file_ids, uploaded_by=user)
            .values_list('id', flat=True)
        )
        if owned_folders != folder_ids or owned_files != file_ids:
            raise PermissionDenied('删除目标不存在或无权访问')

        tree_ids = collect_owned_folder_tree(owned_folders, user=user)
        if tree_ids and File.objects.filter(folder_id__in=tree_ids).exclude(
            uploaded_by=user
        ).exists():
            raise PermissionDenied('目录树包含其他用户的文件')

        files_to_delete = File.objects.filter(uploaded_by=user).filter(
            Q(id__in=file_ids) | Q(folder_id__in=tree_ids)
        )
        files_to_delete.delete()

        if tree_ids:
            # Break cycles and avoid recursive cascade collection. Temporary
            # globally unique positions keep conditional position constraints valid.
            folders = list(Folder.objects.filter(id__in=tree_ids).order_by('id'))
            max_position = (
                Folder.objects.filter(created_by=user)
                .aggregate(value=Max('position'))['value']
                or 0
            )
            for index, folder in enumerate(folders, start=1):
                folder.position = max_position + index
            Folder.objects.bulk_update(folders, ['position'])
            Folder.objects.filter(id__in=tree_ids).update(parent=None)
            Folder.objects.filter(id__in=tree_ids).delete()

    return len(folder_ids) + len(file_ids)


def move_owned_items(*, user, target_folder_id=None, folder_ids=(), file_ids=()):
    """Move one current-directory selection and append it to the target.

    The user row serializes position allocation. Items first receive temporary
    positions outside the user's current range so conditional unique position
    constraints remain valid while their parent scope changes.
    """
    folder_ids = set(folder_ids)
    file_ids = set(file_ids)
    if not folder_ids and not file_ids:
        raise ValidationError('请先选择要移动的项目')

    with transaction.atomic():
        User.objects.select_for_update().get(pk=user.pk)

        target = None
        if target_folder_id is not None:
            target = (
                Folder.objects.select_for_update()
                .filter(pk=target_folder_id, created_by=user)
                .first()
            )
            if target is None:
                raise PermissionDenied('目标文件夹不存在或无权访问')

        folders = list(
            Folder.objects.select_for_update()
            .filter(id__in=folder_ids, created_by=user)
            .order_by('id')
        )
        files = list(
            File.objects.select_for_update()
            .filter(id__in=file_ids, uploaded_by=user)
            .order_by('id')
        )
        if {item.id for item in folders} != folder_ids:
            raise PermissionDenied('移动列表包含无权访问的文件夹')
        if {item.id for item in files} != file_ids:
            raise PermissionDenied('移动列表包含无权访问的文件')

        # The UI only selects the visible directory. Enforce that boundary on
        # forged requests too, including a mixed selection of folders + files.
        source_scopes = {item.parent_id for item in folders}
        source_scopes.update(item.folder_id for item in files)
        if len(source_scopes) != 1:
            raise ValidationError('移动项目必须位于同一目录')
        source_folder_id = source_scopes.pop()
        if source_folder_id == target_folder_id:
            raise ValidationError('所选项目已在目标文件夹中')

        # Walking upward from the target is enough to reject both self moves
        # and moves into any selected folder's descendants.
        selected_folder_ids = {item.id for item in folders}
        ancestor = target
        seen_ancestors = set()
        while ancestor is not None:
            if ancestor.id in selected_folder_ids:
                raise ValidationError('不能将文件夹移动到自身或其子文件夹中')
            if ancestor.id in seen_ancestors:
                raise ValidationError('目标文件夹层级存在循环')
            seen_ancestors.add(ancestor.id)
            if ancestor.parent_id is None:
                break
            ancestor = (
                Folder.objects.select_for_update()
                .filter(pk=ancestor.parent_id, created_by=user)
                .first()
            )
            if ancestor is None:
                raise PermissionDenied('目标目录树包含无权访问的数据')

        if folders:
            maximum = (
                Folder.objects.filter(created_by=user)
                .aggregate(value=Max('position'))['value']
                or 0
            )
            for index, item in enumerate(folders, start=1):
                item.position = maximum + index
            Folder.objects.bulk_update(folders, ['position'])

            target_position = _next_position(
                Folder.objects.filter(parent=target, created_by=user)
            )
            for index, item in enumerate(folders):
                item.parent = target
                item.position = target_position + index
            Folder.objects.bulk_update(folders, ['parent', 'position'])

        if files:
            maximum = (
                File.objects.filter(uploaded_by=user)
                .aggregate(value=Max('position'))['value']
                or 0
            )
            for index, item in enumerate(files, start=1):
                item.position = maximum + index
            File.objects.bulk_update(files, ['position'])

            target_position = _next_position(
                File.objects.filter(folder=target, uploaded_by=user)
            )
            for index, item in enumerate(files):
                item.folder = target
                item.position = target_position + index
            File.objects.bulk_update(files, ['folder', 'position'])

    return len(folder_ids) + len(file_ids)
