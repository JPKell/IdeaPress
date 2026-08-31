"""ideapress.bootstrap — the composition root: settings, logging and the ASGI app, wired once.

This module sits outside the ``web``/``cli``/``services``/``domain`` ordering that
``.importlinter`` enforces, precisely so it can import both configuration and the web layer.
``ideapress.cli`` never imports it — the ``web-cli-independence`` contract forbids any import
chain from ``cli`` into ``web``, and this module imports ``web``. The CLI's ``serve`` command
hands uvicorn the dotted string ``"ideapress.bootstrap:create_app_from_environment"`` and lets
uvicorn perform that import itself; a string literal is invisible to import-linter's static
analysis, so the two surfaces stay decoupled at the source level while running one application in
one process.

Nothing here raises because a backend is unreachable. Spec §20 AC7 is explicit that an unavailable
backend is never a startup failure, and AC1 requires a zero-configuration start on loopback with
nothing reachable at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ideapress.config import LoadedSettings, load_settings
from ideapress.observability.logging import configure_logging
from ideapress.services.runtime import build_runtime

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["bootstrap", "create_app_from_environment"]


def bootstrap(*, config_path: str | None = None) -> LoadedSettings:
    """Resolve configuration and install logging, once, for a process.

    Args:
        config_path: An explicit ``--config`` path.

    Returns:
        The loaded settings.

    Raises:
        ConfigurationError: Configuration is invalid or a bind combination is unsafe. Never raised
            for an unreachable backend.
    """
    loaded = load_settings(config_path=config_path)
    configure_logging(
        level=loaded.settings.logging.level,
        fmt=loaded.settings.logging.format,
        include_content=loaded.settings.logging.include_content,
    )
    return loaded


def create_app_from_environment() -> FastAPI:
    """Build the application from the ambient environment, for uvicorn's factory import."""
    from ideapress.web.app import create_app

    loaded = bootstrap()
    return create_app(loaded.settings, runtime_builder=build_runtime)
