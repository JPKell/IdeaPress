"""ideapress.web.app — the FastAPI application factory.

``create_app`` is a pure function of :class:`~ideapress.config.Settings`: it opens nothing, so a
test can build an app without touching the filesystem. The database handle is created by the
lifespan, which runs only when the application is actually served.

Host validation, the request-ID middleware and the error envelope come from MirrorWall, not from
this module. Three implementations of one security control are three chances to get it subtly
different, and the difference will be in the application nobody audited (ADR-0026 §1). IdeaPress is
the first application that writes none of its own plumbing, so the only middleware defined locally
are the two shaped by this application's own limits.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

from baseaicore import SuiteError, new_id
from fastapi import FastAPI, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from mirrorwall import (
    CsrfMiddleware,
    HostValidationMiddleware,
    RequestIdMiddleware,
    error_body,
    loopback_allowlist,
    mount_static,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from ideapress.__about__ import __version__
from ideapress.config import LOOPBACK_HOSTS, Settings
from ideapress.web.limits import BodySizeLimitMiddleware, SameOriginMiddleware
from ideapress.web.rendering import render, templates
from ideapress.web.routes import backends as backend_routes
from ideapress.web.routes import export as export_routes
from ideapress.web.routes import plan as plan_routes
from ideapress.web.routes import projects as project_routes
from ideapress.web.routes import settings as settings_routes
from ideapress.web.routes import stages as stage_routes
from ideapress.web.routes import system as system_routes
from ideapress.web.routes import units as unit_routes
from ideapress.web.routes import workspace as workspace_routes

__all__ = ["create_app", "register_exception_handlers"]

logger = logging.getLogger(__name__)

_STATUS_BY_CODE: Final[dict[str, int]] = {
    # Spec §13's fifteen, plus the shared ones from baseaicore.
    "BACKEND_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "BACKEND_VERSION_MISMATCH": status.HTTP_502_BAD_GATEWAY,
    "MODEL_NOT_CONFIGURED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "PROVIDER_TIMEOUT": status.HTTP_504_GATEWAY_TIMEOUT,
    "CONTEXT_LIMIT_EXCEEDED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "VALIDATION_FAILED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "REQUIREMENTS_UNMET": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "STAGE_PRECONDITION_FAILED": status.HTTP_409_CONFLICT,
    "REVISION_LIMIT_REACHED": status.HTTP_409_CONFLICT,
    # A refusal is the model declining, not the workflow breaking: 200-family would hide it from a
    # script, 500-family would call it an outage. 422 says "your request, as posed, was not done".
    "CONTENT_REJECTED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "PROJECT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "UNIT_NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "STAGE_ALREADY_RUNNING": status.HTTP_409_CONFLICT,
    "EXPORT_FAILED": status.HTTP_500_INTERNAL_SERVER_ERROR,
    # Not 500: the machine is busy, not broken. 503 is the status a caller retries.
    "INSUFFICIENT_VRAM": status.HTTP_503_SERVICE_UNAVAILABLE,
    "SCHEMA_VERSION_UNSUPPORTED": status.HTTP_422_UNPROCESSABLE_CONTENT,
    "VALIDATION_ERROR": status.HTTP_400_BAD_REQUEST,
    "CONFIGURATION_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "INSECURE_BINDING": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "NOT_FOUND": status.HTTP_404_NOT_FOUND,
    "CONFLICT": status.HTTP_409_CONFLICT,
    "MISDIRECTED_REQUEST": 421,
    "CSRF_FAILED": status.HTTP_403_FORBIDDEN,
    "DATABASE_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "DATABASE_UNAVAILABLE": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_REQUIRED": status.HTTP_503_SERVICE_UNAVAILABLE,
    "MIGRATION_FAILED": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "SCHEMA_AHEAD": status.HTTP_500_INTERNAL_SERVER_ERROR,
    "STORAGE_BUSY": status.HTTP_503_SERVICE_UNAVAILABLE,
    "STORAGE_FULL": status.HTTP_507_INSUFFICIENT_STORAGE,
    "INTERNAL_ERROR": status.HTTP_500_INTERNAL_SERVER_ERROR,
}

_CODE_BY_HTTP_STATUS: Final[dict[int, str]] = {
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    413: "PAYLOAD_TOO_LARGE",
    415: "UNSUPPORTED_MEDIA_TYPE",
    421: "MISDIRECTED_REQUEST",
}


def _request_id_of(request: Request) -> str:
    """Return this request's ID, generating one if the request-ID middleware did not run."""
    state_id = getattr(request.state, "request_id", None)
    return state_id if isinstance(state_id, str) and state_id else new_id()


def _wants_html(request: Request) -> bool:
    """Whether this is a page request: outside ``/api``, from a client that accepts HTML."""
    if request.url.path.startswith("/api/"):
        return False
    return "text/html" in request.headers.get("accept", "")


def _error_response(
    *,
    request_id: str,
    code: str,
    message: str,
    status_code: int,
    details: Mapping[str, Any] | None = None,
    request: Request | None = None,
) -> Response:
    """Render one error as HTML for a page request and as the JSON envelope everywhere else."""
    if request is not None and _wants_html(request):
        html = render(
            "error.html",
            page=None,
            page_title="Error",
            code=code,
            message=message,
            status_code=status_code,
            request_id=request_id,
            path=request.url.path,
        )
        return HTMLResponse(html, status_code=status_code, headers={"X-Request-ID": request_id})
    return JSONResponse(
        status_code=status_code,
        content=error_body(code=code, message=message, request_id=request_id, details=details),
        headers={"X-Request-ID": request_id},
    )


def _resolve_allowed_hosts(settings: Settings) -> frozenset[str]:
    """The Host-header allowlist for this bind (ADR-0026 §1)."""
    host = settings.server.host.lower()
    if host in LOOPBACK_HOSTS:
        return loopback_allowlist(host)
    return frozenset(name.lower() for name in settings.server.allowed_hosts) | {host}


def _docs_allowed(settings: Settings) -> bool:
    """Interactive API docs are loopback-only by default (API standards §11)."""
    return settings.server.host in LOOPBACK_HOSTS


def register_exception_handlers(app: FastAPI) -> None:
    """Register the handlers that translate every exception type into the standard envelope."""

    @app.exception_handler(SuiteError)
    async def _suite_error_handler(request: Request, exc: SuiteError) -> Response:
        status_code = _STATUS_BY_CODE.get(exc.code, status.HTTP_500_INTERNAL_SERVER_ERROR)
        if status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
            logger.error("request.failed", extra={"code": exc.code}, exc_info=exc)
        else:
            logger.warning("request.rejected", extra={"code": exc.code})
        return _error_response(
            request_id=_request_id_of(request),
            code=exc.code,
            message=exc.message,
            status_code=status_code,
            details=exc.details,
            request=request,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError) -> Response:
        fields = [
            {
                "path": ".".join(str(part) for part in error["loc"] if part != "body"),
                "problem": error["msg"],
            }
            for error in exc.errors()
        ]
        return _error_response(
            request_id=_request_id_of(request),
            code="VALIDATION_ERROR",
            message="Request body failed validation.",
            status_code=status.HTTP_400_BAD_REQUEST,
            details={"fields": fields},
            request=request,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
        code = _CODE_BY_HTTP_STATUS.get(exc.status_code, "HTTP_ERROR")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Request failed."
        return _error_response(
            request_id=_request_id_of(request),
            code=code,
            message=message,
            status_code=exc.status_code,
            request=request,
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
        logger.error("request.unhandled_error", exc_info=exc)
        return _error_response(
            request_id=_request_id_of(request),
            code="INTERNAL_ERROR",
            message="An unexpected error occurred.",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            request=request,
        )


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Own the process's handles for as long as the server serves.

    Nothing here may raise because a backend is unreachable: spec §20 AC7 requires that never to
    be a startup failure, and the health endpoint is how a user finds out.
    """
    builder = getattr(app.state, "runtime_builder", None)
    runtime = builder(app.state.settings) if builder is not None else None
    app.state.runtime = runtime
    app.state.health_checkers = runtime.health_checkers if runtime is not None else ()
    try:
        yield
    finally:
        if runtime is not None:
            runtime.close()
        app.state.runtime = None
        app.state.health_checkers = None


def create_app(settings: Settings, *, runtime_builder: Any | None = None) -> FastAPI:
    """Build the FastAPI application for the given settings.

    Args:
        settings: The validated configuration.
        runtime_builder: Callable taking the settings and returning the object that owns the
            database handle, the backend and the health checkers. Injected so that a test can
            build an app with no runtime at all, and so the composition root stays the only place
            that knows how to open a database.

    Returns:
        The application. Still a pure function of its arguments — it opens nothing; the runtime is
        created by the lifespan, which runs only when the application is served (or when a test
        enters ``TestClient`` as a context manager).
    """
    app = FastAPI(
        title="IdeaPress",
        version=__version__,
        docs_url="/api/v1/docs" if _docs_allowed(settings) else None,
        openapi_url="/api/v1/openapi.json" if _docs_allowed(settings) else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.runtime = None
    app.state.runtime_builder = runtime_builder
    app.state.health_checkers = None

    # Starlette wraps in reverse order of these calls, so the stack from the outside in is:
    # body limit, same-origin, CSRF, Host validation, the request ID. Host validation therefore
    # runs before routing on every request (ADR-0026 §1) — a DNS-rebinding attempt is 421 before
    # a route function exists to receive it.
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(HostValidationMiddleware, allowed_hosts=_resolve_allowed_hosts(settings))
    app.add_middleware(CsrfMiddleware)
    app.add_middleware(SameOriginMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.server.max_body_bytes)

    register_exception_handlers(app)

    app.include_router(system_routes.router, prefix="/api/v1")
    app.include_router(project_routes.router, prefix="/api/v1")
    app.include_router(backend_routes.router, prefix="/api/v1")
    app.include_router(backend_routes.ui_router)
    app.include_router(stage_routes.router, prefix="/api/v1")
    app.include_router(plan_routes.ui_router)
    app.include_router(workspace_routes.ui_router)
    app.include_router(unit_routes.router, prefix="/api/v1")
    app.include_router(unit_routes.ui_router)
    app.include_router(export_routes.router, prefix="/api/v1")
    app.include_router(settings_routes.router, prefix="/api/v1")
    app.include_router(project_routes.ui_router)
    app.include_router(system_routes.ui_router)

    # MirrorWall's own assets, from the installed package: no CDN, no network request at page load.
    mount_static(app, environment=templates())

    return app
