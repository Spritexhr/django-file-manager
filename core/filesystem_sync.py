"""Synchronize the database tree with the Samba-visible ``MEDIA_ROOT``.

The filesystem is treated as untrusted input: a Samba client may create names,
symlinks, or special files that never passed through Django's forms.  All public
helpers therefore keep operations below the owning ``user_<id>`` directory and
never follow symlinks.
"""

from __future__ import annotations

import mimetypes
import os
import shutil
import stat
from pathlib import Path, PurePosixPath

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q

from .models import File, Folder, _validate_storage_component


class FilesystemSyncError(OSError):
    """Base error for an unsafe or inconsistent media tree."""


class UnsafeStoragePath(FilesystemSyncError):
    """Raised when a path could leave a user's media directory."""


def _media_root() -> Path:
    return Path(settings.MEDIA_ROOT).resolve(strict=False)


def _lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _require_component(value, *, label: str) -> str:
    try:
        return _validate_storage_component(value, label=label)
    except ValueError as exc:
        raise UnsafeStoragePath(str(exc)) from exc


def _assert_beneath(path: Path, boundary: Path) -> None:
    boundary = boundary.resolve(strict=False)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(boundary):
        raise UnsafeStoragePath(f'{path} is outside {boundary}')


def _assert_no_symlink_components(boundary: Path, target: Path) -> None:
    """Reject existing symlinks between *boundary* and *target*, inclusive."""
    boundary = boundary.resolve(strict=False)
    _assert_beneath(target, boundary)
    try:
        relative = target.relative_to(boundary)
    except ValueError as exc:
        raise UnsafeStoragePath(f'{target} is outside {boundary}') from exc

    current = boundary
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeStoragePath(f'Symlinks are not allowed in media paths: {current}')


def _ensure_real_directory(path: Path, *, boundary: Path) -> Path:
    """Create *path* component-by-component without accepting symlinks."""
    boundary = boundary.resolve(strict=False)
    boundary.mkdir(parents=True, exist_ok=True)
    if boundary.is_symlink() or not boundary.is_dir():
        raise UnsafeStoragePath(f'Invalid media root: {boundary}')

    _assert_beneath(path, boundary)
    current = boundary
    for part in path.relative_to(boundary).parts:
        current = current / part
        if _lexists(current):
            if current.is_symlink() or not current.is_dir():
                raise UnsafeStoragePath(f'Expected a real directory: {current}')
            continue
        try:
            current.mkdir()
        except FileExistsError:
            # A Samba client may have raced the mkdir; inspect what won.
            pass
        if current.is_symlink() or not current.is_dir():
            raise UnsafeStoragePath(f'Expected a real directory: {current}')
    return path


def user_root_path(user_id) -> Path:
    """Return the absolute Samba directory for a numeric Django user id."""
    try:
        normalized_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise UnsafeStoragePath(f'Invalid user id: {user_id!r}') from exc
    if normalized_id <= 0 or str(normalized_id) != str(user_id).strip():
        raise UnsafeStoragePath(f'Invalid user id: {user_id!r}')

    root = _media_root()
    candidate = root / f'user_{normalized_id}'
    _assert_beneath(candidate, root)
    if candidate.is_symlink():
        raise UnsafeStoragePath(f'User root may not be a symlink: {candidate}')
    return candidate


def _folder_components(folder: Folder) -> tuple[int, list[str]]:
    user_id = folder.created_by_id
    if not user_id:
        raise UnsafeStoragePath('Folder must have an owner before resolving its path')

    components = []
    seen = set()
    current = folder
    while current is not None:
        marker = current.pk if current.pk is not None else id(current)
        if marker in seen:
            raise UnsafeStoragePath('Folder hierarchy contains a cycle')
        seen.add(marker)
        if current.created_by_id != user_id:
            raise UnsafeStoragePath('Folder hierarchy crosses user boundaries')
        components.append(_require_component(current.name, label='folder name'))
        current = current.parent
    components.reverse()
    return user_id, components


def folder_absolute_path(folder: Folder) -> Path:
    """Return a folder's expected absolute path without creating it."""
    user_id, components = _folder_components(folder)
    user_root = user_root_path(user_id)
    candidate = user_root.joinpath(*components)
    _assert_beneath(candidate, user_root)
    _assert_no_symlink_components(user_root, candidate)
    return candidate


def _ensure_user_root(user_id) -> Path:
    root = _media_root()
    user_root = user_root_path(user_id)
    return _ensure_real_directory(user_root, boundary=root)


def ensure_folder_directory(folder: Folder) -> Path:
    """Create the physical directory corresponding to a database Folder."""
    user_id, _ = _folder_components(folder)
    user_root = _ensure_user_root(user_id)
    path = folder_absolute_path(folder)
    return _ensure_real_directory(path, boundary=user_root)


def _assert_tree_has_no_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise UnsafeStoragePath(f'Refusing to operate on symlink: {path}')
    for current, directory_names, file_names in os.walk(path, followlinks=False):
        current_path = Path(current)
        for name in [*directory_names, *file_names]:
            child = current_path / name
            if child.is_symlink():
                raise UnsafeStoragePath(f'Refusing to remove tree containing symlink: {child}')


def remove_folder_directory(folder: Folder) -> bool:
    """Remove a folder tree, refusing ambiguous paths and all symlinks."""
    if folder.pk and Folder.objects.filter(
        created_by_id=folder.created_by_id,
        parent_id=folder.parent_id,
        name=folder.name,
    ).exclude(pk=folder.pk).exists():
        raise FilesystemSyncError('Duplicate sibling folder names share one physical path')

    path = folder_absolute_path(folder)
    if not _lexists(path):
        return False
    if path.is_symlink() or not path.is_dir():
        raise UnsafeStoragePath(f'Expected a real folder directory: {path}')
    _assert_tree_has_no_symlinks(path)
    shutil.rmtree(path)
    return True


def _storage_name_for_path(path: Path) -> str:
    root = _media_root()
    _assert_beneath(path, root)
    _assert_no_symlink_components(root, path)
    return path.relative_to(root).as_posix()


def _absolute_path_for_storage_name(storage_name: str, *, user_id) -> Path:
    normalized = str(storage_name or '').replace('\\', '/')
    pure = PurePosixPath(normalized)
    if pure.is_absolute() or not pure.parts or any(part in ('', '.', '..') for part in pure.parts):
        raise UnsafeStoragePath(f'Invalid stored file name: {storage_name!r}')
    for part in pure.parts:
        _require_component(part, label='stored path component')
    if pure.parts[0] != f'user_{int(user_id)}':
        raise UnsafeStoragePath('Stored file does not belong to its recorded user directory')

    user_root = user_root_path(user_id)
    path = _media_root().joinpath(*pure.parts)
    _assert_beneath(path, user_root)
    _assert_no_symlink_components(user_root, path)
    return path


def _infer_content_type(filename: str) -> str:
    guessed, _ = mimetypes.guess_type(filename, strict=False)
    if not guessed or '\r' in guessed or '\n' in guessed:
        return 'application/octet-stream'
    return guessed


def _next_position(model, **scope) -> int:
    maximum = model.objects.filter(**scope).aggregate(value=Max('position'))['value']
    return (maximum or 0) + 1


def _new_sync_stats() -> dict:
    return {
        'folders_imported': 0,
        'folders_existing': 0,
        'files_imported': 0,
        'files_existing': 0,
        'metadata_updated': 0,
        'files_pruned': 0,
        'folders_pruned': 0,
        'legacy_preserved': 0,
        'symlinks_rejected': 0,
        'special_entries_rejected': 0,
        'invalid_names_rejected': 0,
        'conflicts': 0,
        'errors': [],
    }


def _record_error(stats: dict, path: Path, exc: Exception) -> None:
    stats['errors'].append({'path': os.fspath(path), 'error': str(exc)})


def _same_folder(file_obj: File, folder: Folder | None) -> bool:
    return file_obj.folder_id == (folder.pk if folder is not None else None)


def _refresh_metadata(file_obj: File, *, name: str, size: int, content_type: str) -> bool:
    updates = {}
    if not file_obj.original_name:
        updates['original_name'] = name
    if file_obj.size != size:
        updates['size'] = size
    if file_obj.content_type != content_type:
        updates['content_type'] = content_type
    if updates:
        File.objects.filter(pk=file_obj.pk).update(**updates)
        return True
    return False


def _import_or_match_file(
    *,
    user,
    folder: Folder | None,
    path: Path,
    entry_stat,
    seen_file_ids: set[int],
    stats: dict,
) -> None:
    storage_name = _storage_name_for_path(path)
    alternate_name = storage_name.replace('/', '\\')
    rows = list(
        File.objects.filter(Q(file=storage_name) | Q(file=alternate_name))
        .select_related('folder')
        .order_by('pk')
    )
    owned_rows = [row for row in rows if row.uploaded_by_id == user.pk]
    foreign_rows = [row for row in rows if row.uploaded_by_id != user.pk]
    if foreign_rows:
        stats['conflicts'] += len(foreign_rows)
    if owned_rows:
        content_type = _infer_content_type(path.name)
        for row in owned_rows:
            if _refresh_metadata(
                row,
                name=path.name,
                size=entry_stat.st_size,
                content_type=content_type,
            ):
                stats['metadata_updated'] += 1
            if _same_folder(row, folder):
                seen_file_ids.add(row.pk)
            else:
                # A legacy flat file can legitimately belong to a nested DB
                # folder until materialize_storage explicitly moves it.
                stats['legacy_preserved'] += 1
        stats['files_existing'] += len(owned_rows)
        if len(owned_rows) > 1:
            stats['conflicts'] += len(owned_rows) - 1
        return
    if foreign_rows:
        return

    position = _next_position(File, folder=folder, uploaded_by=user)
    content_type = _infer_content_type(path.name)
    file_obj, created = File.objects.get_or_create(
        file=storage_name,
        defaults={
            'folder': folder,
            'uploaded_by': user,
            'original_name': path.name,
            'size': entry_stat.st_size,
            'content_type': content_type,
            'position': position,
        },
    )
    if not created:
        if file_obj.uploaded_by_id != user.pk:
            stats['conflicts'] += 1
            return
        if _refresh_metadata(
            file_obj,
            name=path.name,
            size=entry_stat.st_size,
            content_type=content_type,
        ):
            stats['metadata_updated'] += 1
        if _same_folder(file_obj, folder):
            seen_file_ids.add(file_obj.pk)
        else:
            stats['legacy_preserved'] += 1
        stats['files_existing'] += 1
        return

    seen_file_ids.add(file_obj.pk)
    stats['files_imported'] += 1


def _stored_object_exists(file_obj: File) -> bool:
    try:
        path = _absolute_path_for_storage_name(file_obj.file.name, user_id=file_obj.uploaded_by_id)
    except FilesystemSyncError:
        # Unsafe records must be fixed manually; prune is deliberately
        # conservative and must never turn a validation error into data loss.
        return True
    if not _lexists(path):
        return False
    if path.is_symlink() or not path.is_file():
        return True
    return True


def _descendant_folder_ids(folder: Folder) -> list[int]:
    result = []
    frontier = [folder.pk]
    seen = set()
    while frontier:
        folder_id = frontier.pop()
        if folder_id in seen:
            continue
        seen.add(folder_id)
        result.append(folder_id)
        frontier.extend(
            Folder.objects.filter(parent_id=folder_id).values_list('id', flat=True)
        )
    return result


def _folder_has_existing_objects(folder: Folder) -> bool:
    try:
        expected = folder_absolute_path(folder)
    except FilesystemSyncError:
        return True
    if _lexists(expected):
        return True
    folder_ids = _descendant_folder_ids(folder)
    return any(
        _stored_object_exists(file_obj)
        for file_obj in File.objects.filter(folder_id__in=folder_ids).iterator()
    )


def _prune_level(
    *,
    user,
    folder: Folder | None,
    physical_directory_names: set[str],
    seen_file_ids: set[int],
    stats: dict,
) -> None:
    for file_obj in File.objects.filter(folder=folder, uploaded_by=user).iterator():
        if file_obj.pk in seen_file_ids:
            continue
        if _stored_object_exists(file_obj):
            stats['legacy_preserved'] += 1
            continue
        file_obj.delete()
        stats['files_pruned'] += 1

    missing_folders = Folder.objects.filter(parent=folder, created_by=user).exclude(
        name__in=physical_directory_names
    )
    for missing in list(missing_folders):
        if _folder_has_existing_objects(missing):
            stats['legacy_preserved'] += 1
            continue
        folder_ids = _descendant_folder_ids(missing)
        removed_files = File.objects.filter(folder_id__in=folder_ids).count()
        missing.delete()
        stats['folders_pruned'] += len(folder_ids)
        stats['files_pruned'] += removed_files


def _sync_level(*, user, folder: Folder | None, path: Path, prune: bool, stats: dict) -> None:
    _assert_no_symlink_components(user_root_path(user.pk), path)
    seen_file_ids: set[int] = set()
    physical_directory_names: set[str] = set()

    try:
        with os.scandir(path) as iterator:
            entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
    except OSError as exc:
        _record_error(stats, path, exc)
        return

    for entry in entries:
        entry_path = Path(entry.path)
        try:
            if entry.is_symlink():
                stats['symlinks_rejected'] += 1
                continue
            name = _require_component(entry.name, label='Samba entry name')
            if entry.is_dir(follow_symlinks=False):
                physical_directory_names.add(name)
                matches = list(
                    Folder.objects.filter(parent=folder, created_by=user, name=name)
                    .order_by('pk')[:2]
                )
                if len(matches) > 1:
                    stats['conflicts'] += 1
                    continue
                if matches:
                    child_folder = matches[0]
                    stats['folders_existing'] += 1
                else:
                    try:
                        child_folder, created = Folder.objects.get_or_create(
                            name=name,
                            parent=folder,
                            created_by=user,
                            defaults={
                                'position': _next_position(
                                    Folder,
                                    parent=folder,
                                    created_by=user,
                                ),
                            },
                        )
                    except IntegrityError:
                        # A case-insensitive sibling or concurrent importer won
                        # the unique constraint. Do not attach this path to an
                        # ambiguous logical folder.
                        stats['conflicts'] += 1
                        continue
                    if created:
                        stats['folders_imported'] += 1
                    else:
                        stats['folders_existing'] += 1
                _sync_level(
                    user=user,
                    folder=child_folder,
                    path=entry_path,
                    prune=prune,
                    stats=stats,
                )
            elif entry.is_file(follow_symlinks=False):
                entry_stat = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(entry_stat.st_mode):
                    stats['special_entries_rejected'] += 1
                    continue
                _import_or_match_file(
                    user=user,
                    folder=folder,
                    path=entry_path,
                    entry_stat=entry_stat,
                    seen_file_ids=seen_file_ids,
                    stats=stats,
                )
            else:
                stats['special_entries_rejected'] += 1
        except UnsafeStoragePath as exc:
            stats['invalid_names_rejected'] += 1
            _record_error(stats, entry_path, exc)
        except (OSError, ValueError) as exc:
            _record_error(stats, entry_path, exc)

    if prune:
        _prune_level(
            user=user,
            folder=folder,
            physical_directory_names=physical_directory_names,
            seen_file_ids=seen_file_ids,
            stats=stats,
        )


def sync_directory(user, folder: Folder | None = None, prune: bool = False) -> dict:
    """Import a Samba directory tree into the DB and optionally prune stale rows.

    Existing rows are matched by their exact FileField storage name before their
    logical folder is considered.  Consequently, legacy flat files retain their
    existing DB folder until the explicit materialization command moves them.
    """
    if user.pk is None:
        raise FilesystemSyncError('User must be saved before synchronizing storage')
    if folder is not None and folder.created_by_id != user.pk:
        raise FilesystemSyncError('Folder does not belong to the supplied user')

    if folder is None:
        path = _ensure_user_root(user.pk)
    else:
        path = ensure_folder_directory(folder)
    stats = _new_sync_stats()
    _sync_level(user=user, folder=folder, path=path, prune=bool(prune), stats=stats)
    return stats


def _desired_file_path(file_obj: File) -> tuple[Path, str]:
    current_name = str(file_obj.file.name or '').replace('\\', '/')
    fallback_name = current_name.rsplit('/', 1)[-1]
    original_name = file_obj.original_name or fallback_name
    original_name = _require_component(original_name, label='original file name')
    if file_obj.folder_id:
        directory = folder_absolute_path(file_obj.folder)
    else:
        directory = user_root_path(file_obj.uploaded_by_id)
    destination = directory / original_name
    _assert_beneath(destination, user_root_path(file_obj.uploaded_by_id))
    _assert_no_symlink_components(user_root_path(file_obj.uploaded_by_id), destination)
    return destination, original_name


def _new_materialize_stats(*, apply: bool) -> dict:
    return {
        'apply': apply,
        'scanned': 0,
        'already_materialized': 0,
        'would_move': 0,
        'moved': 0,
        'metadata_updated': 0,
        'missing': 0,
        'conflicts': 0,
        'rejected': 0,
        'errors': [],
        'actions': [],
    }


def _append_action(stats: dict, file_obj: File, source: Path, destination: Path, status: str, reason=''):
    stats['actions'].append({
        'file_id': file_obj.pk,
        'source': os.fspath(source),
        'destination': os.fspath(destination),
        'status': status,
        'reason': reason,
    })


def materialize_storage(*, apply: bool = False, user_id=None) -> dict:
    """Move legacy/misplaced files into the Folder-derived physical tree.

    The default is a pure dry-run.  With ``apply=True`` each filesystem rename
    is paired with a DB update; a failed DB transaction triggers a best-effort
    rename back to the original path.
    """
    stats = _new_materialize_stats(apply=bool(apply))
    queryset = File.objects.select_related('uploaded_by', 'folder').order_by('pk')
    if user_id is not None:
        queryset = queryset.filter(uploaded_by_id=user_id)

    reserved_destinations: set[str] = set()
    for file_obj in queryset.iterator():
        stats['scanned'] += 1
        source = Path()
        destination = Path()
        try:
            source = _absolute_path_for_storage_name(
                file_obj.file.name,
                user_id=file_obj.uploaded_by_id,
            )
            destination, original_name = _desired_file_path(file_obj)
            if not _lexists(source):
                stats['missing'] += 1
                _append_action(stats, file_obj, source, destination, 'missing')
                continue
            if source.is_symlink() or not source.is_file():
                raise UnsafeStoragePath(f'Expected a real file: {source}')

            source_resolved = source.resolve(strict=True)
            destination_resolved = destination.resolve(strict=False)
            destination_key = os.path.normcase(os.fspath(destination_resolved))
            same_path = source_resolved == destination_resolved
            if destination_key in reserved_destinations and not same_path:
                stats['conflicts'] += 1
                _append_action(stats, file_obj, source, destination, 'conflict', 'duplicate destination')
                continue
            reserved_destinations.add(destination_key)

            if same_path:
                stats['already_materialized'] += 1
                _append_action(stats, file_obj, source, destination, 'already')
                if apply:
                    file_stat = source.stat(follow_symlinks=False)
                    desired_storage_name = _storage_name_for_path(destination)
                    updates = {
                        'original_name': original_name,
                        'size': file_stat.st_size,
                        'content_type': _infer_content_type(original_name),
                    }
                    if file_obj.file.name != desired_storage_name:
                        updates['file'] = desired_storage_name
                    File.objects.filter(pk=file_obj.pk).update(**updates)
                    stats['metadata_updated'] += 1
                continue

            if _lexists(destination):
                stats['conflicts'] += 1
                _append_action(stats, file_obj, source, destination, 'conflict', 'destination exists')
                continue

            stats['would_move'] += 1
            _append_action(stats, file_obj, source, destination, 'move')
            if not apply:
                continue

            if file_obj.folder_id:
                ensure_folder_directory(file_obj.folder)
            else:
                _ensure_user_root(file_obj.uploaded_by_id)
            _assert_no_symlink_components(user_root_path(file_obj.uploaded_by_id), destination.parent)
            if _lexists(destination):
                raise FileExistsError(f'Destination appeared during migration: {destination}')

            moved = False
            try:
                with transaction.atomic():
                    os.rename(source, destination)
                    moved = True
                    file_stat = destination.stat(follow_symlinks=False)
                    File.objects.filter(pk=file_obj.pk).update(
                        file=_storage_name_for_path(destination),
                        original_name=original_name,
                        size=file_stat.st_size,
                        content_type=_infer_content_type(original_name),
                    )
            except Exception:
                if moved and _lexists(destination) and not _lexists(source):
                    try:
                        os.rename(destination, source)
                    except OSError as rollback_exc:
                        stats['errors'].append({
                            'file_id': file_obj.pk,
                            'error': f'rollback failed: {rollback_exc}',
                        })
                raise
            stats['moved'] += 1
            stats['metadata_updated'] += 1
        except UnsafeStoragePath as exc:
            stats['rejected'] += 1
            stats['errors'].append({'file_id': file_obj.pk, 'error': str(exc)})
            _append_action(stats, file_obj, source, destination, 'rejected', str(exc))
        except (OSError, ValueError) as exc:
            stats['errors'].append({'file_id': file_obj.pk, 'error': str(exc)})
            _append_action(stats, file_obj, source, destination, 'error', str(exc))
    return stats
