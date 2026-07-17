import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from core.models import File, Folder
from core.views import _delete_file_record


class _PathForbiddenFieldFile:
    """A minimal FieldFile double that fails if local-path APIs are used."""

    name = "user_1/private.txt"

    def __init__(self):
        self.storage = Mock()

    def __bool__(self):
        return True

    @property
    def path(self):
        raise AssertionError("deletion must not access FieldFile.path")


class StorageAgnosticDeletionTests(SimpleTestCase):
    def test_web_upload_permissions_keep_samba_group_writable(self):
        self.assertEqual(settings.FILE_UPLOAD_PERMISSIONS, 0o660)
        self.assertEqual(settings.FILE_UPLOAD_DIRECTORY_PERMISSIONS, 0o2770)

    def test_delete_file_record_uses_field_storage_without_accessing_path(self):
        field_file = _PathForbiddenFieldFile()
        file_obj = SimpleNamespace(file=field_file, delete=Mock())

        _delete_file_record(file_obj)

        field_file.storage.delete.assert_called_once_with(field_file.name)
        file_obj.delete.assert_called_once_with()


class DownloadAuthorizationTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other_user = User.objects.create_user(
            username="other", password="pw"
        )
        self.file_obj = File.objects.create(
            file=f"user_{self.owner.pk}/private.txt",
            uploaded_by=self.owner,
            original_name="private.txt",
            size=7,
            content_type="text/plain",
        )

    def test_other_user_cannot_download_owned_file(self):
        self.client.force_login(self.other_user)

        with patch("core.views.build_download_response") as build_response:
            response = self.client.get(
                reverse("download_file", args=[self.file_obj.pk])
            )

        self.assertEqual(response.status_code, 404)
        build_response.assert_not_called()


class FolderStorageCreationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="folder-owner", password="pw")
        self.client.force_login(self.user)
        self.temp_media = tempfile.TemporaryDirectory()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.temp_media.name,
            FILESYSTEM_SYNC_ON_BROWSE=False,
        )
        self.settings_override.enable()

    def tearDown(self):
        self.settings_override.disable()
        self.temp_media.cleanup()

    def test_create_folder_materializes_directory_below_user_root(self):
        response = self.client.post(
            reverse("file_manager_root"),
            {"create_folder": "1", "name": "Documents"},
        )

        self.assertEqual(response.status_code, 302)
        folder = Folder.objects.get(created_by=self.user, name="Documents")
        self.assertIsNone(folder.parent)
        self.assertTrue(
            (
                Path(self.temp_media.name)
                / f"user_{self.user.pk}"
                / "Documents"
            ).is_dir()
        )

    def test_storage_error_rolls_back_folder_and_surfaces_message(self):
        with patch(
            "core.views.ensure_folder_directory",
            side_effect=OSError("share unavailable"),
        ):
            response = self.client.post(
                reverse("file_manager_root"),
                {"create_folder": "1", "name": "Documents"},
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Folder.objects.filter(created_by=self.user, name="Documents").exists()
        )
        self.assertContains(response, "无法在共享存储中创建文件夹，请稍后重试")
