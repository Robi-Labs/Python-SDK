from __future__ import annotations

from typing import Any, Optional


class RobiError(Exception):
    """Base class for all SDK errors."""


class ConnectionError(RobiError):  # noqa: A001 - mirrors other SDKs
    pass


class TimeoutError(ConnectionError):  # noqa: A001
    pass


class APIError(RobiError):
    """Non-2xx response (or an error event inside a stream).

    ``code`` and ``type`` are set when the server sent an object-style ``detail``
    (e.g. ``model_deprecated``).
    """

    def __init__(self, message: str, status: int, body: Any = None, request_id: Optional[str] = None,
                 code: Any = None, type: Optional[str] = None):  # noqa: A002
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.request_id = request_id
        self.code = code
        self.type = type


class BadRequestError(APIError): ...
class AuthenticationError(APIError): ...
class PaymentRequiredError(APIError): ...
class PermissionDeniedError(APIError): ...
class NotFoundError(APIError): ...
class PayloadTooLargeError(APIError): ...
class RateLimitError(APIError): ...
class InternalServerError(APIError): ...


class VideoGenerationError(RobiError):
    """A video job finished with status ``failed``."""

    def __init__(self, code: Optional[str], message: Optional[str], video: Any = None):
        super().__init__(f"Video generation failed ({code}): {message}")
        self.code = code
        self.message = message
        self.video = video


def error_for(status: int, body: Any, request_id: Optional[str]) -> APIError:
    detail: Any = None
    if isinstance(body, dict):
        detail = body.get("detail")
        if detail is None:
            detail = body.get("error")  # OpenAI-style fallback
    code = typ = None
    if isinstance(detail, dict):
        msg, code, typ = detail.get("message"), detail.get("code"), detail.get("type")
    elif isinstance(detail, str):
        msg = detail
    else:
        msg = None  # e.g. 422 validation lists
    if msg is None and isinstance(detail, list):
        msg = "; ".join(str(d.get("msg", d)) if isinstance(d, dict) else str(d) for d in detail)
    cls = (
        BadRequestError if status in (400, 422)
        else AuthenticationError if status == 401
        else PaymentRequiredError if status == 402
        else PermissionDeniedError if status == 403
        else NotFoundError if status == 404
        else PayloadTooLargeError if status == 413
        else RateLimitError if status == 429
        else InternalServerError if status >= 500
        else APIError
    )
    return cls(str(msg or f"Request failed with status {status}"), status, body, request_id, code, typ)
