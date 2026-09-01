"""ideapress.services.project_review — reading the whole document for cross-unit inconsistency.

Workflows §2 stage 15. It runs after every unit has committed, and its findings are **advisory**:
blocking here would mean un-committing work that already passed its own gates, which contradicts
"committed units are never rolled back by a later failure" (workflows §9).

Risk M4 is style drift across units, and this is the stage that can actually see it — a per-unit
audit reads one section and cannot know the term was spelled differently three sections earlier.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ideapress.domain.inference import Correlation, ResponseFormat, StageLimits, StageRequest
from ideapress.errors import StagePreconditionFailed
from ideapress.infrastructure.db.models import AuditFinding as AuditFindingRow
from ideapress.services.prompts import render
from ideapress.services.review import FINDINGS_SCHEMA, parse_findings
from ideapress.services.stages import record_attempt
from ideapress.services.units import committed_units

if TYPE_CHECKING:
    from collections.abc import Callable

    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["PROJECT_REVIEW_PROMPT_ID", "project_review_body", "render_units"]

logger = logging.getLogger(__name__)

PROJECT_REVIEW_PROMPT_ID = "stages.project_review.consistency"


def render_units(units: dict[str, str], titles: dict[str, str]) -> str:
    """Render every committed unit for the reviewer, in reading order and labelled by key."""
    return "\n\n".join(
        f"### {key} — {titles.get(key, '')}\n{text.strip()}" for key, text in units.items()
    )


def project_review_body(runtime: Runtime, *, project_id: str) -> Callable[[StageTask], None]:
    """Build the stage body that reviews the whole project.

    Raises:
        StagePreconditionFailed: Fewer than two units are committed. A cross-unit review of one
            unit has nothing to compare, and running it anyway would produce findings that are
            really per-unit ones the audit already made.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Unit as UnitRow

    with runtime.storage.read() as session:
        units = committed_units(session, project_id)
        titles = {
            row.unit_key: row.title
            for row in session.scalars(
                select(UnitRow).where(UnitRow.project_id == project_id)
            ).all()
        }
    if len(units) < 2:
        message = (
            f"Only {len(units)} unit(s) are committed. A cross-unit review needs at least two "
            "to compare; the per-unit audit has already read each one on its own."
        )
        raise StagePreconditionFailed(
            message, details={"project_id": project_id, "committed_units": len(units)}
        )

    def body(task: StageTask) -> None:
        database = runtime.storage
        sink = runtime.events
        runtime.runner.checkpoint(task)

        prompt = render(PROJECT_REVIEW_PROMPT_ID, {"units": render_units(units, titles)})
        result = runtime.runner.gateway.run(
            StageRequest(
                stage="project_review",
                system=prompt.system or "",
                user=prompt.user,
                response_format=ResponseFormat(kind="json_schema", schema=FINDINGS_SCHEMA),
                limits=StageLimits(
                    temperature=0.0,
                    max_output_tokens=runtime.settings.workflow.structured_output_tokens,
                ),
                correlation=Correlation(project_id=project_id),
                prompt_id=prompt.prompt_id,
                prompt_version=prompt.version,
                prompt_sha256=prompt.sha256,
            )
        )
        report = parse_findings(result.text, stage="project_review", key_prefix="P")
        attempt_id = record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="project_review",
            result=result,
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
            store_content=runtime.runner.store_content,
        )
        with database.write() as session:
            for finding in report.findings:
                session.add(
                    AuditFindingRow(
                        attempt_id=attempt_id,
                        finding_key=finding.key,
                        category=finding.category,
                        severity=finding.severity,
                        confidence=finding.confidence,
                        problem_text=finding.problem_text,
                        evidence_text=finding.evidence_text or None,
                        required_fix_text=finding.required_fix_text or None,
                        uncertain=finding.uncertain,
                        escalated=False,
                        source_stage="project_review",
                    )
                )
        sink.emit(
            database,
            task.run_id,
            event_type="project_review.completed",
            message=f"{report.summary()} across {len(units)} committed unit(s)",
            data={
                "units_reviewed": len(units),
                "score": round(report.score, 3),
                "advisory": True,
                "findings": [
                    {
                        "key": f.key,
                        "severity": f.severity,
                        "category": f.category,
                        "problem": f.problem_text,
                        "evidence": f.evidence_text,
                    }
                    for f in report.findings
                ],
            },
        )

    return body
