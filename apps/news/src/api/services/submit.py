"""Shared submit helpers for the REST and GUI entry points (Phase 7.10-7.12).

The admin REST endpoints and the ``/ui`` handlers must apply exactly the same
validation, so the rules live in the services: routes translate
:class:`SubmitError` into an ``HTTPException``, the GUI into a flash message.
"""

from __future__ import annotations

from starlette.datastructures import UploadFile

#: Multipart framing and the other form fields ride along with the file.
MULTIPART_OVERHEAD = 1_000_000


class SubmitError(Exception):
    """A user-facing submit failure that carries its HTTP status code.

    Attributes:
        message: Human-readable reason (already in Spanish, as flashed).
        status_code: HTTP status the REST route should answer.
    """

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def check_content_length(content_length: str, max_bytes: int) -> None:
    """Reject an oversized request before reading its body.

    Args:
        content_length: The raw ``Content-Length`` header (may be blank).
        max_bytes: File budget of the feature accepting the upload.

    Raises:
        SubmitError: 413 when the declared size exceeds the budget.
    """
    if content_length.isdigit() and int(content_length) > max_bytes + MULTIPART_OVERHEAD:
        raise SubmitError(
            f"request larger than {max_bytes} bytes",
            status_code=413,
        )


async def read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    """Read a multipart file bounded by ``max_bytes``.

    Args:
        upload: The multipart file part.
        max_bytes: Maximum accepted size in bytes.

    Returns:
        The raw bytes.

    Raises:
        SubmitError: 422 when the body is empty, 413 when it is too large.
    """
    data = await upload.read(max_bytes + 1)
    if not data:
        raise SubmitError("uploaded file is empty", status_code=422)
    if len(data) > max_bytes:
        raise SubmitError(
            f"file larger than {max_bytes} bytes",
            status_code=413,
        )
    return data
