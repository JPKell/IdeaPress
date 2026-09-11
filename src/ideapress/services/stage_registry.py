"""ideapress.services.stage_registry — which stages have a body, in one table.

Risk G1: the prior project's orchestrator reached 2 103 lines because every stage added a branch.
Here a stage is a factory in a mapping, the runner never learns a stage's name, and a stage that is
not implemented yet is a missing key rather than a silent no-op.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

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
        overrides: Mapping[str, Any],
    ) -> Callable[[StageTask], None]:
        """Return the stage body. ``overrides`` are the run's, already checked."""
        ...


def _draft(
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    resume: bool,
    overrides: Mapping[str, Any],
) -> Callable[[StageTask], None]:
    """The core loop: draft, validate, repair, coverage, commit."""
    from ideapress.services.unit_loop import draft_body

    return draft_body(
        runtime, project_id=project_id, unit_keys=unit_keys, resume=resume, overrides=overrides
    )


def _research(
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    resume: bool,
    overrides: Mapping[str, Any],
) -> Callable[[StageTask], None]:
    """Fetch and read the project's sources into notes. Optional, and never automatic."""
    from ideapress.services.research import research_factory

    return research_factory(runtime, project_id=project_id, unit_keys=unit_keys, resume=resume)


def _revise(
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    resume: bool,
    overrides: Mapping[str, Any],
) -> Callable[[StageTask], None]:
    """A revision of committed units into new versions (row WP5). There is nothing to resume."""
    from ideapress.services.unit_loop import revise_body

    return revise_body(runtime, project_id=project_id, unit_keys=unit_keys, overrides=overrides)


def _project_review(
    runtime: Runtime,
    *,
    project_id: str,
    unit_keys: Sequence[str],
    resume: bool,
    overrides: Mapping[str, Any],
) -> Callable[[StageTask], None]:
    """Cross-unit consistency findings over every committed unit. Advisory."""
    from ideapress.services.project_review import project_review_body

    return project_review_body(runtime, project_id=project_id)


STAGE_BODIES: Final[dict[StageId, StageBodyFactory]] = {
    "research": _research,
    "draft": _draft,
    "revise": _revise,
    "project_review": _project_review,
}
"""Populated as each phase lands its stages.

`draft` is the whole core loop rather than one step of it, because `validate`, `repair`, `coverage`
and `commit` are not separately startable: they are decided *within* a unit's attempt, and a user
who could run `commit` on its own could commit a unit that never passed validation.

`revise` is its own body rather than a draft run (row WP5): a committed unit's only arrow is
`committed -> revising`, so a draft run over one fails at its first move.

`research` is the one entry that takes neither units nor a resume flag: workflows §2 puts it at
position 2 and the plan at position 4, so it runs before any unit exists (row M1, ADR-0116)."""
