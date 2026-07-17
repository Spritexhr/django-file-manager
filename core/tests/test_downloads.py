import io
from types import SimpleNamespace
from unittest.mock import Mock

from django.test import RequestFactory, SimpleTestCase, override_settings
from django.utils.http import content_disposition_header

from core.downloads import _parse_single_range, build_download_response


class ParseSingleRangeTests(SimpleTestCase):
    def test_parses_closed_open_ended_and_suffix_ranges(self):
        cases = {
            "bytes=2-5": (2, 5),
            "bytes=6-": (6, 9),
            "bytes=-4": (6, 9),
            "bytes=0-999": (0, 9),
            " bytes=1-3 ": (1, 3),
        }

        for header, expected in cases.items():
            with self.subTest(header=header):
                self.assertEqual(_parse_single_range(header, 10), expected)

    def test_rejects_malformed_or_unsatisfiable_ranges(self):
        headers = (
            None,
            "",
            "bytes=-",
            "bytes=10-",
            "bytes=8-2",
            "bytes=-0",
            "bytes=0-1,4-5",
            "items=0-1",
        )

        for header in headers:
            with self.subTest(header=header):
                self.assertIsNone(_parse_single_range(header, 10))

        self.assertIsNone(_parse_single_range("bytes=0-0", 0))

    def test_rejects_oversized_numeric_range_without_raising(self):
        self.assertIsNone(_parse_single_range(f"bytes={'9' * 5000}-", 10))


@override_settings(FILE_DOWNLOAD_CHUNK_SIZE=3)
class DownloadResponseTests(SimpleTestCase):
    data = b"0123456789"

    def setUp(self):
        self.factory = RequestFactory()
        self.storage = Mock()
        self.storage.size.return_value = len(self.data)
        self.handles = []

        def open_file(name, mode):
            handle = io.BytesIO(self.data)
            self.handles.append(handle)
            return handle

        self.storage.open.side_effect = open_file
        self.file_obj = SimpleNamespace(
            pk=17,
            original_name="\u6d4b\u8bd5 report.txt",
            size=len(self.data),
            content_type="text/plain",
            file=SimpleNamespace(
                name="user_1/opaque-file",
                storage=self.storage,
            ),
        )

    @staticmethod
    def consume(response):
        return b"".join(response.streaming_content)

    def assert_common_headers(self, response, length):
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["Content-Length"], str(length))
        self.assertEqual(
            response["Content-Disposition"],
            content_disposition_header(True, self.file_obj.original_name),
        )
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_full_download_streams_from_field_storage(self):
        response = build_download_response(
            self.factory.get("/files/17/download/"), self.file_obj
        )

        self.assertEqual(response.status_code, 200)
        self.assert_common_headers(response, len(self.data))
        self.assertEqual(self.consume(response), self.data)
        self.storage.open.assert_called_once_with("user_1/opaque-file", "rb")

    def test_head_returns_headers_without_opening_storage(self):
        response = build_download_response(
            self.factory.head("/files/17/download/"), self.file_obj
        )

        self.assertEqual(response.status_code, 200)
        self.assert_common_headers(response, len(self.data))
        self.assertEqual(response.content, b"")
        self.storage.open.assert_not_called()

    def test_closed_range(self):
        response = build_download_response(
            self.factory.get(
                "/files/17/download/", HTTP_RANGE="bytes=2-5"
            ),
            self.file_obj,
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 2-5/10")
        self.assert_common_headers(response, 4)
        self.assertEqual(self.consume(response), b"2345")
        self.assertTrue(self.handles[0].closed)

    def test_open_ended_range(self):
        response = build_download_response(
            self.factory.get(
                "/files/17/download/", HTTP_RANGE="bytes=6-"
            ),
            self.file_obj,
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 6-9/10")
        self.assert_common_headers(response, 4)
        self.assertEqual(self.consume(response), b"6789")

    def test_suffix_range(self):
        response = build_download_response(
            self.factory.get(
                "/files/17/download/", HTTP_RANGE="bytes=-3"
            ),
            self.file_obj,
        )

        self.assertEqual(response.status_code, 206)
        self.assertEqual(response["Content-Range"], "bytes 7-9/10")
        self.assert_common_headers(response, 3)
        self.assertEqual(self.consume(response), b"789")

    def test_unsatisfiable_range_returns_416_without_opening_storage(self):
        response = build_download_response(
            self.factory.get(
                "/files/17/download/", HTTP_RANGE="bytes=99-100"
            ),
            self.file_obj,
        )

        self.assertEqual(response.status_code, 416)
        self.assertEqual(response["Content-Range"], "bytes */10")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.storage.open.assert_not_called()

    def test_content_disposition_uses_original_name(self):
        response = build_download_response(
            self.factory.head("/files/17/download/"), self.file_obj
        )

        self.assertEqual(
            response["Content-Disposition"],
            "attachment; filename*=utf-8''%E6%B5%8B%E8%AF%95%20report.txt",
        )
