"""ideapress.services.runtime — the handles one process owns, and the health they report.

Separate from :mod:`ideapress.bootstrap` because the CLI needs these and may not reach the web
layer: the composition root imports ``web``, so anything the CLI imports from it would drag
``cli`` into ``web`` and break both the layering and the web/CLI independence contract.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mirrorwall import ComponentHealth, ComponentStatus

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ideapress.config import Settings

__all__ = ["Runtime", "build_runtime"]


class Runtime:
    """The handles one served process owns: the database, and the health checks that read it.

    Built by :func:`build_runtime` and closed by the application lifespan. Holding them on one
    object rather than on ``app.state`` directly keeps the CLI able to build the same handles
    without a FastAPI application in the picture.
    """

    def __init__(self, settings: Settings) -> None:
        """Open what this process needs. Never raises for an unreachable backend."""
        self.settings = settings
        self._database: Any | None = None

    @property
    def health_checkers(self) -> Sequence[Callable[[], ComponentHealth]]:
        """The three components spec §17 names: ``database``, ``backend`` and ``prompts``."""
        return (self._database_health, self._backend_health, self._prompts_health)

    def _database_health(self) -> ComponentHealth:
        """Report the database component. P1 storage lands in the next unit."""
        return ComponentHealth(
            name="database",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No schema yet; storage arrives with the project tables.",
        )

    def _backend_health(self) -> ComponentHealth:
        """Report the configured inference backend, naming which one and whether it answers."""
        mode = self.settings.inference.mode
        return ComponentHealth(
            name="backend",
            status=ComponentStatus.NOT_CONFIGURED,
            detail=f"Configured backend is {mode!r}; no adapter is wired yet.",
            data={"mode": mode},
        )

    def _prompts_health(self) -> ComponentHealth:
        """Report the prompt pack: present, parseable, and matching its manifest."""
        return ComponentHealth(
            name="prompts",
            status=ComponentStatus.NOT_CONFIGURED,
            detail="No prompt pack yet.",
        )

    def close(self) -> None:
        """Dispose every handle. Safe to call more than once."""
        if self._database is not None:
            self._database.close()
            self._database = None


def build_runtime(settings: Settings) -> Runtime:
    """Open the handles a served process needs.

    Args:
        settings: The validated configuration.

    Returns:
        The runtime. Never raises because an inference backend is unreachable — that is a runtime
        condition reported through ``/health``, not a reason to refuse to start.
    """
    return Runtime(settings)
