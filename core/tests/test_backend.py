import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError
from django.test import Client, TestCase, override_settings

from core.file_operations import save_uploaded_files
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
