import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.filesystem_sync import FilesystemSyncError, sync_directory
from core.models import Folder


class Command(BaseCommand):
    help = 'Import Samba-side folder/file changes into the Django database.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--user-id',
            type=int,
            help='Synchronize only this Django user id (default: every user).',
        )
        parser.add_argument(
            '--folder-id',
            type=int,
            help='Synchronize only this database folder subtree.',
        )
        parser.add_argument(
            '--prune',
            action='store_true',
            help='Delete DB rows whose physical objects are gone. Legacy misplaced files are preserved.',
        )

    def handle(self, *args, **options):
        user_id = options.get('user_id')
        folder_id = options.get('folder_id')
        prune = bool(options.get('prune'))
        User = get_user_model()

        if folder_id is not None:
            try:
                folder = Folder.objects.select_related('created_by').get(pk=folder_id)
            except Folder.DoesNotExist as exc:
                raise CommandError(f'Folder {folder_id} does not exist') from exc
            if user_id is not None and folder.created_by_id != user_id:
                raise CommandError('The selected folder does not belong to --user-id')
            targets = [(folder.created_by, folder)]
        else:
            users = User.objects.order_by('pk')
            if user_id is not None:
                users = users.filter(pk=user_id)
                if not users.exists():
                    raise CommandError(f'User {user_id} does not exist')
            targets = [(user, None) for user in users]

        totals = {}
        all_errors = []
        for user, folder in targets:
            scope = f'folder {folder.pk}' if folder is not None else 'root'
            try:
                stats = sync_directory(user, folder=folder, prune=prune)
            except (FilesystemSyncError, OSError) as exc:
                raise CommandError(f'Unable to synchronize user {user.pk} {scope}: {exc}') from exc

            self.stdout.write(
                f'user={user.pk} scope={scope} '
                + json.dumps(stats, ensure_ascii=False, sort_keys=True)
            )
            for key, value in stats.items():
                if key == 'errors':
                    all_errors.extend(value)
                elif isinstance(value, int):
                    totals[key] = totals.get(key, 0) + value

        totals['errors'] = len(all_errors)
        style = self.style.WARNING if all_errors else self.style.SUCCESS
        self.stdout.write(style('Summary: ' + json.dumps(totals, sort_keys=True)))
