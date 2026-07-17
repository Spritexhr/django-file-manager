import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.filesystem_sync import materialize_storage


class Command(BaseCommand):
    help = 'Move legacy files into Folder-derived Samba paths (dry-run by default).'

    def add_arguments(self, parser):
        parser.add_argument(
            '--apply',
            action='store_true',
            help='Actually move files and update File.file. Without this flag nothing is changed.',
        )
        parser.add_argument(
            '--user-id',
            type=int,
            help='Process only this Django user id.',
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get('apply'))
        user_id = options.get('user_id')
        if user_id is not None and not get_user_model().objects.filter(pk=user_id).exists():
            raise CommandError(f'User {user_id} does not exist')

        if not apply_changes:
            self.stdout.write(self.style.WARNING('DRY RUN: pass --apply to move files.'))

        stats = materialize_storage(apply=apply_changes, user_id=user_id)
        for action in stats['actions']:
            status = action['status'].upper()
            line = f"[{status}] file={action['file_id']} {action['source']} -> {action['destination']}"
            if action['reason']:
                line += f" ({action['reason']})"
            self.stdout.write(line)

        summary = {key: value for key, value in stats.items() if key not in ('actions', 'errors')}
        summary['errors'] = len(stats['errors'])
        style = self.style.WARNING if stats['errors'] else self.style.SUCCESS
        self.stdout.write(style('Summary: ' + json.dumps(summary, sort_keys=True)))
        for error in stats['errors']:
            self.stderr.write(self.style.ERROR(json.dumps(error, ensure_ascii=False, sort_keys=True)))
