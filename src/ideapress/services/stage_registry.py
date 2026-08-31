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


STAGE_BODIES: Final[dict[StageId, StageBodyFactory]] = {}
"""Populated as each phase lands its stages. Empty here; P4 registers `draft`, `validate`,
`repair`, `coverage` and `commit`, and P5 the review stages."""
