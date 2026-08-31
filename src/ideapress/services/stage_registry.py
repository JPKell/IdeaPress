"""ideapress.services.stage_registry — which stages have a body, in one table.

Risk G1: the prior project's orchestrator reached 2 103 lines because every stage added a branch.
Here a stage is a factory in a mapping, the runner never learns a stage's name, and a stage that is
not implemented yet is a missing key rather than a silent no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from ideapress.domain.stages import StageId
    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["STAGE_BODIES", "StageBodyFactory"]


class StageBodyFactory(Protocol):
    """Builds the callable the runner will execute for one stage over one selection of units."""

    def __call__(
        self,
        runtime: Runtime,
        *,
        project_id: str,
        unit_keys: Sequence[str],
        resume: bool,
    ) -> Callable[[StageTask], None]:
        """Return the stage body."""
        ...


def _draft(
    runtime: Runtime, *, project_id: str, unit_keys: Sequence[str], resume: bool
) -> Callable[[StageTask], None]:
    """The core loop: draft, validate, repair, coverage, commit."""
    from ideapress.services.unit_loop import draft_body

    return draft_body(runtime, project_id=project_id, unit_keys=unit_keys, resume=resume)


STAGE_BODIES: Final[dict[StageId, StageBodyFactory]] = {"draft": _draft}
"""Populated as each phase lands its stages.

`draft` is the whole core loop rather than one step of it, because `validate`, `repair`, `coverage`
and `commit` are not separately startable: they are decided *within* a unit's attempt, and a user
who could run `commit` on its own could commit a unit that never passed validation."""
