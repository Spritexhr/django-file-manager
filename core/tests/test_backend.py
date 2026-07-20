import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings

from core.file_operations import save_uploaded_files
from core.forms import FolderForm
from core.models import File, Folder


class MediaTestCase(TestCase):
    def setUp(self):
        self.media_directory = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_directory.name,
            FILE_DOWNLOAD_CHUNK_SIZE=4,
        )
        self.settings_override.enable()
        self.owner = User.objects.create_user('owner', password='Audit-pass-2026')
        self.other = User.objects.create_user('other', password='Audit-pass-2026')

    def tearDown(self):
        self.settings_override.disable()
        self.media_directory.cleanup()

    def create_file(self, name='secret.txt', content=b'secret', **kwargs):
        return File.objects.create(
            file=SimpleUploadedFile(name, content),
            uploaded_by=kwargs.pop('uploaded_by', self.owner),
            position=kwargs.pop('position', 1),
            **kwargs,
        )


class DownloadSecurityTests(MediaTestCase):
    def test_download_requires_login_and_owner(self):
        file_obj = self.create_file()

        anonymous = self.client.get(f'/download/{file_obj.id}/')
        self.assertEqual(anonymous.status_code, 302)

        self.client.force_login(self.other)
        foreign = self.client.get(f'/download/{file_obj.id}/')
        self.assertEqual(foreign.status_code, 404)

    def test_owner_download_is_attachment_and_supports_ranges(self):
        file_obj = self.create_file(content=b'abcdefghij')
        self.client.force_login(self.owner)

        response = self.client.get(f'/download/{file_obj.id}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(b''.join(response.streaming_content), b'abcdefghij')

        partial = self.client.get(
            f'/download/{file_obj.id}/',
            HTTP_RANGE='bytes=2-5',
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial['Content-Range'], 'bytes 2-5/10')
        self.assertEqual(b''.join(partial.streaming_content), b'cdef')

    def test_raw_media_url_is_not_served(self):
        file_obj = self.create_file()
        response = self.client.get(file_obj.file.url)
        self.assertEqual(response.status_code, 404)


class StorageConsistencyTests(MediaTestCase):
    def test_invalid_bulk_request_does_not_delete_anything(self):
        folder = Folder.objects.create(
            name='keep', created_by=self.owner, position=1
        )
        file_obj = self.create_file(folder=folder)
        stored_path = Path(file_obj.file.path)
        self.client.force_login(self.owner)

        response = self.client.post(
            '/delete/bulk/',
            {'folder_ids[]': [folder.id], 'file_ids[]': [999999]},
        )

        self.assertEqual(response.status_code, 403)
        self.assertTrue(Folder.objects.filter(id=folder.id).exists())
        self.assertTrue(File.objects.filter(id=file_obj.id).exists())
        self.assertTrue(stored_path.exists())

    def test_committed_delete_removes_database_and_storage(self):
        file_obj = self.create_file()
        stored_path = Path(file_obj.file.path)
        self.client.force_login(self.owner)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/delete/file/{file_obj.id}/')

        self.assertEqual(response.status_code, 302)
        self.assertFalse(File.objects.filter(id=file_obj.id).exists())
        self.assertFalse(stored_path.exists())

    def test_model_and_cascade_deletes_clean_storage(self):
        direct = self.create_file(name='direct.txt', position=1)
        direct_path = Path(direct.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            direct.delete()
        self.assertFalse(direct_path.exists())

        folder = Folder.objects.create(
            name='cascade', created_by=self.owner, position=1
        )
        nested = self.create_file(name='nested.txt', folder=folder, position=1)
        nested_path = Path(nested.file.path)
        with self.captureOnCommitCallbacks(execute=True):
            folder.delete()
        self.assertFalse(nested_path.exists())

    def test_cyclic_folder_tree_can_be_deleted_without_recursion(self):
        first = Folder.objects.create(
            name='first', created_by=self.owner, position=1
        )
        second = Folder.objects.create(
            name='second', parent=first, created_by=self.owner, position=1
        )
        first.parent = second
        first.save(update_fields=['parent'])
        self.client.force_login(self.owner)

        response = self.client.post(f'/delete/folder/{first.id}/')

        self.assertEqual(response.status_code, 302)
        self.assertFalse(Folder.objects.filter(id__in=[first.id, second.id]).exists())

    def test_failed_batch_upload_compensates_all_files(self):
        uploads = [
            SimpleUploadedFile('one.txt', b'one'),
            SimpleUploadedFile('two.txt', b'two'),
        ]
        original_save = File.save
        call_count = 0

        def flaky_save(instance, *args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise IntegrityError('simulated insert failure')
            return original_save(instance, *args, **kwargs)

        with patch.object(File, 'save', flaky_save):
            with self.assertRaises(IntegrityError):
                save_uploaded_files(uploads, user=self.owner, folder=None)

        self.assertEqual(File.objects.filter(uploaded_by=self.owner).count(), 0)
        stored_files = [path for path in Path(self.media_directory.name).rglob('*') if path.is_file()]
        self.assertEqual(stored_files, [])


class OrderingTests(MediaTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_invalid_and_cross_scope_reorder_are_rejected(self):
        invalid = self.client.post(
            '/reorder/', {'kind': 'folder', 'ids[]': ['not-an-id']}
        )
        self.assertEqual(invalid.status_code, 400)

        root = Folder.objects.create(
            name='root', created_by=self.owner, position=1
        )
        parent = Folder.objects.create(
            name='parent', created_by=self.owner, position=2
        )
        child = Folder.objects.create(
            name='child', parent=parent, created_by=self.owner, position=1
        )
        cross_scope = self.client.post(
            '/reorder/', {'kind': 'folder', 'ids[]': [root.id, child.id]}
        )
        self.assertEqual(cross_scope.status_code, 400)

    def test_valid_complete_reorder_is_persisted(self):
        first = Folder.objects.create(
            name='first', created_by=self.owner, position=1
        )
        second = Folder.objects.create(
            name='second', created_by=self.owner, position=2
        )

        response = self.client.post(
            '/reorder/', {'kind': 'folder', 'ids[]': [second.id, first.id]}
        )

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual((second.position, first.position), (1, 2))


class MoveOperationTests(MediaTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_move_mixed_selection_appends_to_target(self):
        target = Folder.objects.create(
            name='target', created_by=self.owner, position=1
        )
        moved_folder = Folder.objects.create(
            name='folder', created_by=self.owner, position=2
        )
        existing_file = self.create_file(
            name='existing.txt', folder=target, position=1
        )
        moved_file = self.create_file(name='move.txt', position=1)

        response = self.client.post(
            '/move/',
            {
                'target_folder_id': target.id,
                'folder_ids[]': [moved_folder.id],
                'file_ids[]': [moved_file.id],
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['moved'], 2)
        moved_folder.refresh_from_db()
        moved_file.refresh_from_db()
        existing_file.refresh_from_db()
        self.assertEqual(moved_folder.parent_id, target.id)
        self.assertEqual(moved_folder.position, 1)
        self.assertEqual(moved_file.folder_id, target.id)
        self.assertEqual((existing_file.position, moved_file.position), (1, 2))

    def test_move_to_root_is_supported(self):
        source = Folder.objects.create(
            name='source', created_by=self.owner, position=1
        )
        moved_file = self.create_file(folder=source, position=1)

        response = self.client.post(
            '/move/',
            {'target_folder_id': 'root', 'file_ids[]': [moved_file.id]},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 200)
        moved_file.refresh_from_db()
        self.assertIsNone(moved_file.folder_id)
        self.assertEqual(moved_file.position, 1)

    def test_move_folder_into_descendant_is_rejected(self):
        parent = Folder.objects.create(
            name='parent', created_by=self.owner, position=1
        )
        child = Folder.objects.create(
            name='child', parent=parent, created_by=self.owner, position=1
        )

        response = self.client.post(
            '/move/',
            {'target_folder_id': child.id, 'folder_ids[]': [parent.id]},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        parent.refresh_from_db()
        self.assertIsNone(parent.parent_id)

    def test_cross_owner_and_cross_scope_moves_are_rejected(self):
        foreign_target = Folder.objects.create(
            name='foreign', created_by=self.other, position=1
        )
        root_file = self.create_file(name='root.txt', position=1)
        denied = self.client.post(
            '/move/',
            {'target_folder_id': foreign_target.id, 'file_ids[]': [root_file.id]},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(denied.status_code, 403)

        source = Folder.objects.create(
            name='source', created_by=self.owner, position=1
        )
        nested_file = self.create_file(
            name='nested.txt', folder=source, position=1
        )
        cross_scope = self.client.post(
            '/move/',
            {
                'target_folder_id': 'root',
                'file_ids[]': [root_file.id, nested_file.id],
            },
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(cross_scope.status_code, 400)

        root_file.refresh_from_db()
        nested_file.refresh_from_db()
        self.assertIsNone(root_file.folder_id)
        self.assertEqual(nested_file.folder_id, source.id)

    def test_move_to_current_folder_is_rejected(self):
        source = Folder.objects.create(
            name='source', created_by=self.owner, position=1
        )
        moved_file = self.create_file(folder=source, position=1)

        response = self.client.post(
            '/move/',
            {'target_folder_id': source.id, 'file_ids[]': [moved_file.id]},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )

        self.assertEqual(response.status_code, 400)
        moved_file.refresh_from_db()
        self.assertEqual(moved_file.folder_id, source.id)


class ResponseSafetyTests(MediaTestCase):
    def test_external_referer_is_not_used_for_redirect(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            '/delete/bulk/',
            HTTP_REFERER='https://evil.example/phish',
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_json_script_escapes_database_content(self):
        payload = '</script><script id="stored-xss">window.pwned=1</script>'
        Folder.objects.create(
            name=payload,
            created_by=self.owner,
            position=1,
        )
        self.client.force_login(self.owner)

        response = self.client.get('/')
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(payload, body)
        self.assertIn('\\u003C/script\\u003E', body)


class RoutingAndNamingTests(MediaTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.owner)

    def test_nested_folder_uses_canonical_hierarchical_url(self):
        parent = Folder.objects.create(
            name='项目资料', created_by=self.owner, position=1
        )
        child = Folder.objects.create(
            name='历史版本', parent=parent, created_by=self.owner, position=1
        )
        canonical_url = child.get_absolute_url()

        response = self.client.get(canonical_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '项目资料')
        self.assertContains(response, '历史版本')
        self.assertIn(f'{parent.id}-', canonical_url)
        self.assertIn(f'{child.id}-', canonical_url)

        legacy = self.client.get(f'/folder/{child.id}/')
        self.assertEqual(legacy.status_code, 301)
        self.assertEqual(legacy['Location'], canonical_url)

        stale_slug = self.client.get(
            f'/files/{parent.id}-old/{child.id}-old/'
        )
        self.assertEqual(stale_slug.status_code, 301)
        self.assertEqual(stale_slug['Location'], canonical_url)

        wrong_hierarchy = self.client.get(f'/files/{child.id}-wrong/')
        self.assertEqual(wrong_hierarchy.status_code, 404)

    def test_folder_name_is_normalized_and_invalid_names_are_rejected(self):
        valid = FolderForm({'name': '  项目   资料  '})
        self.assertTrue(valid.is_valid())
        self.assertEqual(valid.cleaned_data['name'], '项目 资料')

        for invalid_name in ('CON', '.hidden', '资料/备份', '结尾.', '控制\u200b字符'):
            with self.subTest(name=invalid_name):
                form = FolderForm({'name': invalid_name})
                self.assertFalse(form.is_valid())
                self.assertEqual(len(form.errors['name']), 1)

    def test_case_insensitive_sibling_duplicate_is_rejected(self):
        Folder.objects.create(
            name='Projects', created_by=self.owner, position=1
        )
        duplicate = Folder(
            name='projects', created_by=self.owner, position=2
        )

        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_file_page_exposes_visible_select_all_and_download_confirmation(self):
        self.create_file()
        response = self.client.get('/')
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn('data-testid="select-all"', body)
        self.assertIn('确认下载', body)
