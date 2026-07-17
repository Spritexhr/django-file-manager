import mimetypes
import re

from django.conf import settings
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _content_type(file_obj):
    stored = str(file_obj.content_type or '').strip()
    if stored and '\r' not in stored and '\n' not in stored and '/' in stored:
        return stored
    guessed, _ = mimetypes.guess_type(file_obj.original_name)
    return guessed or "application/octet-stream"


def _parse_single_range(value, size):
    """Return an inclusive (start, end) pair for one HTTP byte range."""
    match = _RANGE_RE.fullmatch((value or "").strip())
    if not match or size <= 0:
        return None

    raw_start, raw_end = match.groups()
    if not raw_start and not raw_end:
        return None

    try:
        start_value = int(raw_start) if raw_start else None
        end_value = int(raw_end) if raw_end else None
    except ValueError:
        # Python limits decimal-to-int conversion length. Treat an oversized
        # Range value as unsatisfiable instead of turning it into a 500.
        return None

    if start_value is None:
        suffix_length = end_value
        if suffix_length <= 0:
            return None
        start = max(size - suffix_length, 0)
        return start, size - 1

    start = start_value
    if start >= size:
        return None

    end = size - 1 if end_value is None else min(end_value, size - 1)
    if end < start:
        return None
    return start, end


def _iter_range(file_handle, start, length, chunk_size):
    try:
        file_handle.seek(start)
        remaining = length
        while remaining:
            chunk = file_handle.read(min(chunk_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        file_handle.close()


def _set_download_headers(response, file_obj, size):
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition_header(
        True, file_obj.original_name
    )
    response["Content-Length"] = str(size)
    return response


def build_download_response(request, file_obj):
    """Build an authenticated, storage-agnostic download response.

    A single HTTP byte range is supported so interrupted large downloads and
    media clients can resume without reading the object from the beginning.
    """
    storage = file_obj.file.storage
    # Samba clients can replace a file without touching the Django row. Stat on
    # each download so Range and Content-Length never rely on stale metadata.
    size = storage.size(file_obj.file.name)
    if file_obj.size != size:
        type(file_obj).objects.filter(pk=file_obj.pk).update(size=size)
        file_obj.size = size

    content_type = _content_type(file_obj)
    range_header = request.headers.get("Range")

    if request.method == "HEAD":
        response = HttpResponse(content_type=content_type)
        return _set_download_headers(response, file_obj, size)

    if range_header:
        byte_range = _parse_single_range(range_header, size)
        if byte_range is None:
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            response["Accept-Ranges"] = "bytes"
            return response

        start, end = byte_range
        length = end - start + 1
        file_handle = storage.open(file_obj.file.name, "rb")
        response = StreamingHttpResponse(
            _iter_range(
                file_handle,
                start,
                length,
                getattr(settings, "FILE_DOWNLOAD_CHUNK_SIZE", 1024 * 1024),
            ),
            status=206,
            content_type=content_type,
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        return _set_download_headers(response, file_obj, length)

    file_handle = storage.open(file_obj.file.name, "rb")
    response = FileResponse(
        file_handle,
        as_attachment=True,
        filename=file_obj.original_name,
        content_type=content_type,
    )
    response.block_size = getattr(settings, "FILE_DOWNLOAD_CHUNK_SIZE", 1024 * 1024)
    response["Accept-Ranges"] = "bytes"
    response["Content-Length"] = str(size)
    return response
