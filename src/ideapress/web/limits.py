"""ideapress.web.limits — request-size and same-origin middleware.

Security standards §14: an oversize body is refused before it is buffered, and a cross-origin JSON
write is refused the way a forged form post is. Both are small enough that a shared implementation
would be more coupling than it saves; MirrorWall supplies the two controls that must be identical
everywhere (host validation and CSRF), and these two are shaped by the application's own limits.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from baseaicore import new_id
from mirrorwall import error_body
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

__all__ = ["BodySizeLimitMiddleware", "SameOriginMiddleware"]

_UNSAFE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _refuse(request: Request, *, code: str, message: str, status: int) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or new_id()
    return JSONResponse(
        status_code=status,
        content=error_body(code=code, message=message, request_id=request_id),
        headers={"X-Request-ID": request_id},
    )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Refuse a request whose declared body exceeds ``max_bytes``, before it is read."""

    def __init__(self, app: object, *, max_bytes: int) -> None:
        """Store the limit. ``max_bytes`` comes from ``server.max_body_bytes``."""
        super().__init__(app)  # type: ignore[arg-type]  # Starlette types this as ASGIApp
        self._max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Return 413 when ``Content-Length`` exceeds the limit; otherwise continue.

        A chunked request declares no length, so this cannot see it; the ASGI server's own limits
        apply there. The check is worth having anyway: the common oversize upload declares a size.
        """
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > self._max_bytes:
            return _refuse(
                request,
                code="PAYLOAD_TOO_LARGE",
                message=f"Request body exceeds the {self._max_bytes} byte limit.",
                status=413,
            )
        return await call_next(request)


class SameOriginMiddleware(BaseHTTPMiddleware):
    """Refuse a cross-origin state-changing request.

    A browser sends ``Origin`` on every cross-origin write, so an absent header is a non-browser
    caller (curl, the CLI) and is allowed; a present header that is not this host is refused. This
    complements CSRF tokens rather than replacing them: the token covers forged forms, this covers
    a JSON write from a page the user happened to visit.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Return 403 when a state-changing request declares a foreign origin."""
        if request.method in _UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                expected = f"{request.url.scheme}://{request.headers.get('host', '')}"
                if origin != expected:
                    return _refuse(
                        request,
                        code="FORBIDDEN",
                        message="Cross-origin state-changing requests are refused.",
                        status=403,
                    )
        return await call_next(request)
