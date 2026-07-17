import os
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings

from core.filesystem_sync import materialize_storage, sync_directory
from core.forms import FileUploadForm, FolderForm
from core.models import File, Folder


class SharedFilesystemTests(TestCase):
    def setUp(self):
        self.temp_media = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_media.name)
        self.settings_override.enable()
        self.user = User.objects.create_user(username='sync-user', password='pw')

    def tearDown(self):
        self.settings_override.disable()
        self.temp_media.cleanup()

    @property
    def user_root(self):
        return Path(self.temp_media.name) / f'user_{self.user.pk}'

    def test_upload_path_mirrors_nested_folder_names(self):
        parent = Folder.objects.create(name='资料', created_by=self.user)
        child = Folder.objects.create(name='2026', parent=parent, created_by=self.user)
        file_obj = File(folder=child, uploaded_by=self.user)

        self.assertEqual(
            File.get_upload_path(file_obj, '报告.pdf'),
            f'user_{self.user.pk}/资料/2026/报告.pdf',
        )

    def test_sync_imports_external_nested_tree_and_metadata(self):
        external = self.user_root / 'Photos' / 'Summer'
        external.mkdir(parents=True)
        payload = external / 'beach.jpg'
        payload.write_bytes(b'jpeg-data')

        stats = sync_directory(self.user)

        photos = Folder.objects.get(created_by=self.user, parent=None, name='Photos')
        summer = Folder.objects.get(created_by=self.user, parent=photos, name='Summer')
        imported = File.objects.get(uploaded_by=self.user, folder=summer)
        self.assertEqual(
            imported.file.name,
            f'user_{self.user.pk}/Photos/Summer/beach.jpg',
        )
        self.assertEqual(imported.original_name, 'beach.jpg')
        self.assertEqual(imported.size, 9)
        self.assertEqual(imported.content_type, 'image/jpeg')
        self.assertEqual(stats['folders_imported'], 2)
        self.assertEqual(stats['files_imported'], 1)

        repeated = sync_directory(self.user)
        self.assertEqual(repeated['folders_imported'], 0)
        self.assertEqual(repeated['files_imported'], 0)
        self.assertEqual(
            File.objects.filter(uploaded_by=self.user).count(),
            1,
        )

    def test_sync_preserves_legacy_flat_file_logical_folder(self):
        logical = Folder.objects.create(name='Logical', created_by=self.user)
        self.user_root.mkdir(parents=True)
        legacy_path = self.user_root / 'legacy.txt'
        legacy_path.write_text('legacy', encoding='utf-8')
        record = File.objects.create(
            file=f'user_{self.user.pk}/legacy.txt',
            folder=logical,
            uploaded_by=self.user,
            original_name='legacy.txt',
        )

        stats = sync_directory(self.user, prune=True)

        record.refresh_from_db()
        self.assertEqual(record.folder, logical)
        self.assertEqual(record.size, len(b'legacy'))
        self.assertGreaterEqual(stats['legacy_preserved'], 1)

    def test_materialize_is_dry_run_then_moves_legacy_file(self):
        logical = Folder.objects.create(name='Documents', created_by=self.user)
        self.user_root.mkdir(parents=True)
        source = self.user_root / 'report.txt'
        source.write_bytes(b'report')
        record = File.objects.create(
            file=f'user_{self.user.pk}/report.txt',
            folder=logical,
            uploaded_by=self.user,
            original_name='report.txt',
        )

        preview = materialize_storage()
        self.assertEqual(preview['would_move'], 1)
        self.assertTrue(source.exists())

        applied = materialize_storage(apply=True)
        destination = self.user_root / 'Documents' / 'report.txt'
        record.refresh_from_db()
        self.assertEqual(applied['moved'], 1)
        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())
        self.assertEqual(
            record.file.name,
            f'user_{self.user.pk}/Documents/report.txt',
        )
        self.assertEqual(record.size, 6)

    def test_sync_rejects_symlink_without_following_it(self):
        self.user_root.mkdir(parents=True)
        outside = Path(self.temp_media.name) / 'outside'
        outside.mkdir()
        link = self.user_root / 'escape'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('Creating symlinks is not available on this host')

        stats = sync_directory(self.user)

        self.assertEqual(stats['symlinks_rejected'], 1)
        self.assertFalse(Folder.objects.filter(created_by=self.user, name='escape').exists())


class PortableNameValidationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='name-user', password='pw')

    def test_folder_rejects_case_insensitive_sibling_duplicate(self):
        Folder.objects.create(name='Reports', created_by=self.user)
        form = FolderForm(
            {'name': 'reports'},
            user=self.user,
            parent=None,
        )

        self.assertFalse(form.is_valid())
        self.assertIn('同一目录下已存在同名文件夹', form.errors['name'])

    def test_upload_rejects_windows_reserved_filename(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.utils.datastructures import MultiValueDict

        form = FileUploadForm(
            files=MultiValueDict({
                'files': [SimpleUploadedFile('CON.txt', b'data')],
            })
        )

        self.assertFalse(form.is_valid())
        self.assertIn('保留名称', str(form.non_field_errors()))

    def test_database_rejects_case_insensitive_sibling_duplicate(self):
        Folder.objects.create(name='Reports', created_by=self.user)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Folder.objects.create(name='reports', created_by=self.user)

    def test_database_rejects_duplicate_storage_name(self):
        storage_name = f'user_{self.user.pk}/report.txt'
        File.objects.create(
            file=storage_name,
            uploaded_by=self.user,
            original_name='report.txt',
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            File.objects.create(
                file=storage_name,
                uploaded_by=self.user,
                original_name='report.txt',
            )
