import mimetypes
import re

from django.conf import settings
from django.http import FileResponse, HttpResponse, StreamingHttpResponse
from django.utils.http import content_disposition_header


_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def original_name(file_obj):
    """Return a safe display name without exposing the storage path."""
    stored_name = str(file_obj.file.name or '').replace('\\', '/')
    return stored_name.rsplit('/', 1)[-1] or 'download'


def _content_type(file_obj):
    guessed, _ = mimetypes.guess_type(original_name(file_obj))
    return guessed or 'application/octet-stream'


def _parse_single_range(value, size):
    match = _RANGE_RE.fullmatch((value or '').strip())
    if not match or size <= 0:
        return None

    raw_start, raw_end = match.groups()
    if not raw_start and not raw_end:
        return None

    try:
        start_value = int(raw_start) if raw_start else None
        end_value = int(raw_end) if raw_end else None
    except ValueError:
        return None

    if start_value is None:
        if not end_value or end_value <= 0:
            return None
        return max(size - end_value, 0), size - 1

    if start_value >= size:
        return None

    end = size - 1 if end_value is None else min(end_value, size - 1)
    if end < start_value:
        return None
    return start_value, end


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


def _set_headers(response, file_obj, size):
    response['Accept-Ranges'] = 'bytes'
    response['Content-Disposition'] = content_disposition_header(
        True,
        original_name(file_obj),
    )
    response['Content-Length'] = str(size)
    return response


def build_download_response(request, file_obj):
    """Return an attachment response for an already-authorized file record."""
    storage = file_obj.file.storage
    size = storage.size(file_obj.file.name)
    content_type = _content_type(file_obj)

    if request.method == 'HEAD':
        return _set_headers(HttpResponse(content_type=content_type), file_obj, size)

    range_header = request.headers.get('Range')
    if range_header:
        byte_range = _parse_single_range(range_header, size)
        if byte_range is None:
            response = HttpResponse(status=416)
            response['Content-Range'] = f'bytes */{size}'
            response['Accept-Ranges'] = 'bytes'
            return response

        start, end = byte_range
        length = end - start + 1
        response = StreamingHttpResponse(
            _iter_range(
                storage.open(file_obj.file.name, 'rb'),
                start,
                length,
                getattr(settings, 'FILE_DOWNLOAD_CHUNK_SIZE', 1024 * 1024),
            ),
            status=206,
            content_type=content_type,
        )
        response['Content-Range'] = f'bytes {start}-{end}/{size}'
        return _set_headers(response, file_obj, length)

    response = FileResponse(
        storage.open(file_obj.file.name, 'rb'),
        as_attachment=True,
        filename=original_name(file_obj),
        content_type=content_type,
    )
    response.block_size = getattr(settings, 'FILE_DOWNLOAD_CHUNK_SIZE', 1024 * 1024)
    response['Accept-Ranges'] = 'bytes'
    response['Content-Length'] = str(size)
    return response
