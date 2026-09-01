"""ideapress.services.plan — compiling requirements and planning units, then storing both.

The plan stage is two bounded model tasks with a deterministic gate between them and another after.
Python decides at every join: which material the model sees, which requirements survive grounding,
which keys exist, and whether the plan may stand.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError
from sqlalchemy import delete, select

from ideapress.domain.inference import (
    Correlation,
    ResponseFormat,
    StageLimits,
    StageRequest,
    StageResult,
)
from ideapress.domain.plan import Plan, PlanUnit, check_plan, unit_key
from ideapress.domain.requirements import (
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
)
from ideapress.errors import GroundingUnavailable
from ideapress.infrastructure.db.models import Requirement as RequirementRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.prompts import render
from ideapress.services.requirements import (
    STRUCTURED_OUTPUT_TOKENS,
    CompilationResult,
    compile_requirements,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from ideapress.services.database import Database
    from ideapress.services.inference import InferenceGateway

__all__ = [
    "MAX_UNITS",
    "MIN_UNITS",
    "OUTLINE_PROMPT_ID",
    "PlanResult",
    "build_plan",
    "load_plan",
    "load_requirements",
    "store_plan",
]

logger = logging.getLogger(__name__)

OUTLINE_PROMPT_ID = "stages.outline.plan"
MIN_UNITS = 2
MAX_UNITS = 12

_PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["units"],
    "additionalProperties": False,
    "properties": {
        "units": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "goal_text", "requirement_keys"],
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "goal_text": {"type": "string"},
                    "requirement_keys": {"type": "array", "items": {"type": "string"}},
                    "target_words": {"type": "integer"},
                },
            },
        }
    },
}


@dataclass(frozen=True, slots=True)
class PlanResult:
    """What one plan stage produced: the compilation, the plan, and the prompt provenance."""

    compilation: CompilationResult
    plan: Plan
    outline_prompt_id: str
    outline_prompt_version: str
    outline_prompt_sha256: str
    outline_raw_text: str
    outline_result: StageResult


def _render_requirements(requirements: Sequence[Requirement]) -> str:
    """Render requirements for the outline prompt, keys first, blocking flagged.

    Rendered by Python from stored rows, so the model assigns identifiers **it did not invent** and
    cannot quietly reword a requirement on the way through (workflows §11).
    """
    return "\n".join(
        f"- {requirement.key} [{'BLOCKING' if requirement.blocking else 'advisory'}] "
        f"{requirement.text}"
        for requirement in requirements
    )


def _parse_units(text: str) -> list[dict[str, Any]]:
    """Read the model's plan into candidate unit dictionaries.

    Raises:
        ValidationError: The answer is not a JSON object with a ``units`` array.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        message = f"The outline stage did not return JSON: {exc}"
        raise ValidationError(message, details={"answer": text[:400]}) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("units"), list):
        message = "The outline stage's answer has no 'units' array."
        raise ValidationError(message, details={"answer": text[:400]})
    return [unit for unit in payload["units"] if isinstance(unit, dict)]


def refuse_ungroundable(
    requirements: Sequence[Requirement], *, sources: Mapping[str, str] | None
) -> None:
    """Refuse a plan whose blocking requirements ask for evidence the project does not have.

    Args:
        requirements: The compiled requirements.
        sources: The attached source documents, or ``None``/empty when there are none.

    Raises:
        GroundingUnavailable: A **blocking** requirement is marked `demands_grounding` and no
            source is attached. Names every such requirement and the two ways out.

    ADR-0043 §1. A requirement asking that claims rest on evidence, in a project with nothing for
    them to rest on, is not a hard requirement — it is an unsatisfiable one, and the honest moment
    to say so is before any unit is written.

    M8 is why this exists. A brief asked that claims be grounded in "usage figures, named programme
    types, and the specific services that have no other local provider" and attached no sources.
    The model supplied a daily footfall count, a workshop attendance figure and a named 2023 audit,
    none of which exist, and every gate passed: 23 checks, an audit scoring 1.00, a critique of
    `leave_it_alone`, full coverage. Nothing in the run observed that the requirement asked for
    evidence and the project had none.

    Only **blocking** requirements refuse. A non-blocking one asking for evidence is a preference
    the author can take or leave, and stopping a project over it would be the refusal outstaying
    its welcome.
    """
    if sources:
        return
    ungroundable = [r for r in requirements if r.blocking and r.demands_grounding]
    if not ungroundable:
        return
    named = ", ".join(f"{r.key} ({r.text.strip()[:80]})" for r in ungroundable)
    message = (
        f"{'This requirement asks' if len(ungroundable) == 1 else 'These requirements ask'} for "
        f"claims to be grounded in evidence, and this project has no sources attached: {named}. "
        "Attach a source, or reword the brief so the requirement is about content rather than "
        "about evidence."
    )
    raise GroundingUnavailable(
        message,
        details={
            "requirement_keys": [r.key for r in ungroundable],
            "sources_attached": 0,
            "remedy": "attach a source document, or reword the brief",
        },
    )


def build_plan(
    gateway: InferenceGateway,
    *,
    project_id: str,
    brief: str,
    sources: Mapping[str, str] | None = None,
    generation: int = 1,
    structured_output_tokens: int = STRUCTURED_OUTPUT_TOKENS,
) -> PlanResult:
    """Compile requirements, then plan units against them, then gate the plan.

    Args:
        gateway: The single choke point every stage reaches a model through.
        project_id: For correlation and provenance.
        brief: The project's brief.
        sources: Attached source documents.
        generation: Which compilation generation this produces.
        structured_output_tokens: The output budget for both model tasks, reasoning included —
            ``workflow.structured_output_tokens`` when the caller holds settings.

    Returns:
        The compilation and the gated plan.

    Raises:
        ValidationError: The compilation produced nothing, the outline was unparseable, or the plan
            failed :func:`~ideapress.domain.plan.check_plan` — which names every blocking
            requirement left unassigned. **The gate runs after the model has spoken and does not
            consult it**: a plan is accepted because a set difference is empty, never because the
            model reported that it covered everything.
    """
    compilation = compile_requirements(
        gateway,
        project_id=project_id,
        brief=brief,
        sources=sources,
        generation=generation,
        structured_output_tokens=structured_output_tokens,
    )
    refuse_ungroundable(compilation.requirements, sources=sources)
    prompt = render(
        OUTLINE_PROMPT_ID,
        {
            "brief": brief,
            "requirements": _render_requirements(compilation.requirements),
            "min_units": str(MIN_UNITS),
            "max_units": str(MAX_UNITS),
        },
    )
    result = gateway.run(
        StageRequest(
            stage="outline",
            system=prompt.system or "",
            user=prompt.user,
            response_format=ResponseFormat(kind="json_schema", schema=_PLAN_SCHEMA),
            limits=StageLimits(temperature=0.0, max_output_tokens=structured_output_tokens),
            correlation=Correlation(project_id=project_id),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    )

    units: list[PlanUnit] = []
    for index, candidate in enumerate(_parse_units(result.text), start=1):
        keys = tuple(
            str(key).strip() for key in candidate.get("requirement_keys", ()) if str(key).strip()
        )
        target = candidate.get("target_words")
        units.append(
            PlanUnit(
                key=unit_key(index),
                ordinal=index,
                title=str(candidate.get("title", "")).strip() or f"Unit {index}",
                goal_text=str(candidate.get("goal_text", "")).strip(),
                requirement_keys=keys,
                target_words=int(target) if isinstance(target, int) and target > 0 else None,
            )
        )

    plan = Plan(
        units=tuple(units),
        requirement_keys=tuple(r.key for r in compilation.requirements),
    )
    check_plan(plan, compilation.requirements)
    return PlanResult(
        compilation=compilation,
        plan=plan,
        outline_prompt_id=prompt.prompt_id,
        outline_prompt_version=prompt.version,
        outline_prompt_sha256=prompt.sha256,
        outline_raw_text=result.text,
        outline_result=result,
    )


def store_plan(database: Database, *, project_id: str, result: PlanResult) -> None:
    """Persist a compilation and its plan, replacing any previous plan for this project.

    Requirements are **added**, never replaced: recompilation creates a new generation and the old
    rows stay, because a committed unit's coverage report must remain readable against the
    requirements it was actually judged on (data model §2).

    Units *are* replaced, but only those not yet committed — a plan change must never delete work
    a person already accepted.
    """
    with database.write() as session:
        for requirement in result.compilation.requirements:
            session.add(
                RequirementRow(
                    project_id=project_id,
                    requirement_key=requirement.key,
                    generation=requirement.generation,
                    text=requirement.text,
                    blocking=requirement.blocking,
                    checks_json=[
                        {
                            "kind": check.kind,
                            "values": list(check.values),
                            "threshold": check.threshold,
                        }
                        for check in requirement.checks
                    ],
                    source_document=requirement.source.document,
                    source_quote=requirement.source.quote,
                    source_anchor=requirement.source.anchor,
                    compiled_by_prompt_id=requirement.compiled_by.prompt_id,
                    compiled_by_prompt_version=requirement.compiled_by.version,
                    compiled_by_prompt_sha256=requirement.compiled_by.prompt_sha256,
                )
            )
        session.execute(
            delete(UnitRow).where(UnitRow.project_id == project_id, UnitRow.state != "committed")
        )
        for unit in result.plan.units:
            session.add(
                UnitRow(
                    project_id=project_id,
                    unit_key=unit.key,
                    ordinal=unit.ordinal,
                    title=unit.title,
                    goal_text=unit.goal_text,
                    requirement_keys_json=list(unit.requirement_keys),
                    target_words=unit.target_words,
                    state="planned",
                )
            )


def _row_to_requirement(row: RequirementRow) -> Requirement:
    """Rebuild the value object from its row, dropping a check the domain would now refuse."""
    checks: list[RequirementCheck] = []
    for entry in row.checks_json:
        try:
            checks.append(
                RequirementCheck(
                    kind=entry.get("kind", ""),
                    values=tuple(entry.get("values", ())),
                    threshold=entry.get("threshold"),
                )
            )
        except ValidationError:  # pragma: no cover — a check stored by an older, laxer build
            logger.warning("requirements.stored_check_refused", extra={"kind": entry.get("kind")})
    return Requirement(
        key=row.requirement_key,
        text=row.text,
        blocking=row.blocking,
        source=SourceReference(
            document=row.source_document, quote=row.source_quote, anchor=row.source_anchor
        ),
        compiled_by=CompiledBy(
            prompt_id=row.compiled_by_prompt_id,
            version=row.compiled_by_prompt_version,
            prompt_sha256=row.compiled_by_prompt_sha256,
        ),
        checks=tuple(checks),
        generation=row.generation,
    )


def load_requirements(
    session: Session, project_id: str, *, generation: int | None = None
) -> list[Requirement]:
    """Read a project's requirements, newest generation by default."""
    if generation is None:
        generation = session.scalar(
            select(RequirementRow.generation)
            .where(RequirementRow.project_id == project_id)
            .order_by(RequirementRow.generation.desc())
            .limit(1)
        )
        if generation is None:
            return []
    rows = session.scalars(
        select(RequirementRow)
        .where(RequirementRow.project_id == project_id, RequirementRow.generation == generation)
        .order_by(RequirementRow.requirement_key)
    ).all()
    return [_row_to_requirement(row) for row in rows]


def load_plan(session: Session, project_id: str) -> Plan:
    """Read a project's unit plan, in reading order."""
    rows = session.scalars(
        select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
    ).all()
    return Plan(
        units=tuple(
            PlanUnit(
                key=row.unit_key,
                ordinal=row.ordinal,
                title=row.title,
                goal_text=row.goal_text,
                requirement_keys=tuple(row.requirement_keys_json),
                target_words=row.target_words,
            )
            for row in rows
        ),
        requirement_keys=tuple(
            requirement.key for requirement in load_requirements(session, project_id)
        ),
    )
