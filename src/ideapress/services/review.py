"""ideapress.services.review — audit, critique and bounded revision.

Workflows §2 stages 8–12 and §5's loops. The shape:

* an **audit** reads and reports findings; it returns :class:`~ideapress.domain.audit.AuditReport`,
  which has no content field, so it could not rewrite the unit if it wanted to;
* a **critique** returns a verdict, which *asks* for a revision and never decides one;
* **Python** decides, against the round limit and the diminishing-returns threshold, and records
  which stop applied;
* a revision that **increases validation failures** is discarded and the previous version kept.

Escalation to `audit_deep` happens at most **once per unit per round** (workflows §5), and only
when the fast audit's computed score falls below `workflow.audit_escalation_threshold`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError

from ideapress.domain.audit import SEVERITY_ORDER, AuditFinding, AuditReport
from ideapress.domain.critique import VERDICTS, Critique
from ideapress.domain.inference import Correlation, ResponseFormat, StageLimits, StageRequest
from ideapress.domain.requirements import Requirement
from ideapress.services.prompts import render
from ideapress.services.requirements import STRUCTURED_OUTPUT_TOKENS

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.stages import StageId
    from ideapress.services.inference import InferenceGateway

__all__ = [
    "FINDINGS_SCHEMA",
    "parse_critique",
    "parse_findings",
    "render_findings",
    "render_requirements",
    "run_audit",
    "run_critique",
    "run_revision",
]

logger = logging.getLogger(__name__)

FINDINGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["findings"],
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["category", "severity", "problem_text"],
                "additionalProperties": False,
                "properties": {
                    "category": {"type": "string"},
                    "severity": {"type": "string"},
                    "problem_text": {"type": "string"},
                    "evidence_text": {"type": "string"},
                    "required_fix_text": {"type": "string"},
                    "uncertain": {"type": "boolean"},
                },
            },
        }
    },
}

_CRITIQUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["verdict"],
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": sorted(VERDICTS)},
        "rationale": {"type": "string"},
    },
}


def render_requirements(requirements: Sequence[Requirement]) -> str:
    """Render requirements for a reviewer, from stored rows rather than a model's summary."""
    if not requirements:
        return "(none)"
    return "\n".join(
        f"- {r.key} [{'BLOCKING' if r.blocking else 'advisory'}] {r.text}" for r in requirements
    )


def render_findings(findings: Sequence[AuditFinding]) -> str:
    """Render findings for a critic or a reviser, worst first, from stored rows."""
    if not findings:
        return "(no findings)"
    ordered = sorted(findings, key=lambda f: SEVERITY_ORDER.index(f.severity))
    lines: list[str] = []
    for finding in ordered:
        lines.append(f"- [{finding.severity}] {finding.category}: {finding.problem_text}")
        if finding.evidence_text:
            lines.append(f"    evidence: {finding.evidence_text}")
        if finding.required_fix_text:
            lines.append(f"    would resolve it: {finding.required_fix_text}")
    return "\n".join(lines)


def _unwrap(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1].rsplit("```", 1)[0]
    return stripped


def parse_findings(text: str, *, stage: str, key_prefix: str = "F") -> AuditReport:
    """Read an auditor's answer into a report.

    Args:
        text: What the model returned.
        stage: Which audit stage produced it.
        key_prefix: Prefix for the generated finding keys.

    Returns:
        The report. Finding keys are **generated here**, never taken from the model: nothing a
        model produced becomes an identifier the system then trusts.

    Raises:
        ValidationError: The answer is not a JSON object with a ``findings`` array. A malformed
            audit is retried and then fails cleanly; it is never interpreted charitably.
    """
    try:
        payload = json.loads(_unwrap(text))
    except json.JSONDecodeError as exc:
        message = f"The {stage} stage did not return JSON: {exc}"
        raise ValidationError(message, details={"answer": text[:400]}) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("findings"), list):
        message = f"The {stage} stage's answer has no 'findings' array."
        raise ValidationError(message, details={"answer": text[:400]})

    findings: list[AuditFinding] = []
    for index, raw in enumerate(payload["findings"], start=1):
        if not isinstance(raw, dict):
            continue
        severity = str(raw.get("severity", "minor")).lower()
        if severity not in SEVERITY_ORDER:
            # An unknown severity becomes `minor` rather than being dropped: the finding may be
            # real, and a model inventing a severity name must not be able to inflate its weight.
            logger.info("audit.unknown_severity", extra={"severity": severity})
            severity = "minor"
        problem = str(raw.get("problem_text", "")).strip()
        if not problem:
            continue
        findings.append(
            AuditFinding(
                key=f"{key_prefix}-{index:03d}",
                category=str(raw.get("category", "general"))[:60],
                severity=severity,  # type: ignore[arg-type]  # checked against SEVERITY_ORDER
                problem_text=problem,
                evidence_text=str(raw.get("evidence_text", ""))[:2000],
                required_fix_text=str(raw.get("required_fix_text", ""))[:2000],
                uncertain=bool(raw.get("uncertain", False)),
                source_stage=stage,
            )
        )
    return AuditReport(findings=tuple(findings), stage=stage)


def parse_critique(text: str, *, improvement_delta: float | None = None) -> Critique:
    """Read a critic's answer into a verdict.

    Raises:
        ValidationError: The answer is not JSON, or names a verdict that is not one of the three.
            An unrecognised verdict is **refused**, not coerced: silently reading an invented
            verdict as "acceptable" would let a model end the loop with a word nobody defined.
    """
    try:
        payload = json.loads(_unwrap(text))
    except json.JSONDecodeError as exc:
        message = f"The critique stage did not return JSON: {exc}"
        raise ValidationError(message, details={"answer": text[:400]}) from exc
    if not isinstance(payload, dict):
        message = "The critique stage's answer is not a JSON object."
        raise ValidationError(message, details={"answer": text[:400]})
    verdict = str(payload.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        message = (
            f"The critique returned {verdict!r}, which is not a verdict. The verdicts are: "
            f"{', '.join(sorted(VERDICTS))}."
        )
        raise ValidationError(message, details={"verdict": verdict})
    return Critique(
        verdict=verdict,  # type: ignore[arg-type]  # checked against VERDICTS
        rationale_text=str(payload.get("rationale", ""))[:2000],
        improvement_delta=improvement_delta,
    )


@dataclass(frozen=True, slots=True)
class AuditOutcome:
    """One audit pass: the report, and the raw result for the attempt record."""

    report: AuditReport
    result: Any
    prompt_id: str
    prompt_version: str
    prompt_sha256: str


def run_audit(
    gateway: InferenceGateway,
    *,
    stage: StageId,
    project_id: str,
    unit_key: str,
    content: str,
    requirements: Sequence[Requirement],
    prior_findings: Sequence[AuditFinding] = (),
    round_number: int = 0,
    structured_output_tokens: int = STRUCTURED_OUTPUT_TOKENS,
) -> AuditOutcome:
    """Run one audit stage over a unit's text.

    Args:
        gateway: The single choke point.
        stage: ``audit_fast`` or ``audit_deep``.
        project_id, unit_key: For correlation.
        content: The unit's text.
        requirements: What it must satisfy.
        prior_findings: What a fast audit found, for a deep one.
        round_number: The revision round.
        structured_output_tokens: The output budget, reasoning included —
            ``workflow.structured_output_tokens`` when the caller holds settings.

    Returns:
        The findings, and everything the attempt record needs.

    Raises:
        ValidationError: The auditor's answer was not parseable.
    """
    prompt_id = f"stages.{stage}.review"
    variables: dict[str, str] = {
        "requirements": render_requirements(requirements),
        "content": content,
    }
    if stage == "audit_deep":
        variables["prior_findings"] = render_findings(prior_findings)
    prompt = render(prompt_id, variables)
    result = gateway.run(
        StageRequest(
            stage=stage,
            system=prompt.system or "",
            user=prompt.user,
            response_format=ResponseFormat(kind="json_schema", schema=FINDINGS_SCHEMA),
            limits=StageLimits(temperature=0.0, max_output_tokens=structured_output_tokens),
            correlation=Correlation(project_id=project_id, unit_id=unit_key, round=round_number),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    )
    prefix = "D" if stage == "audit_deep" else "A"
    return AuditOutcome(
        report=parse_findings(result.text, stage=stage, key_prefix=prefix),
        result=result,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


@dataclass(frozen=True, slots=True)
class CritiqueOutcome:
    """One critique pass."""

    critique: Critique
    result: Any
    prompt_id: str
    prompt_version: str
    prompt_sha256: str


def run_critique(
    gateway: InferenceGateway,
    *,
    project_id: str,
    unit_key: str,
    content: str,
    requirements: Sequence[Requirement],
    findings: Sequence[AuditFinding],
    rounds_used: int,
    max_rounds: int,
    improvement_delta: float | None = None,
    structured_output_tokens: int = STRUCTURED_OUTPUT_TOKENS,
) -> CritiqueOutcome:
    """Ask for a quality verdict. The verdict asks; it never decides.

    ``structured_output_tokens`` is the verdict's output budget, reasoning included —
    ``workflow.structured_output_tokens`` when the caller holds settings.
    """
    prompt = render(
        "stages.critique.judge",
        {
            "requirements": render_requirements(requirements),
            "findings": render_findings(findings),
            "content": content,
            "rounds_used": str(rounds_used),
            "max_rounds": str(max_rounds),
        },
    )
    result = gateway.run(
        StageRequest(
            stage="critique",
            system=prompt.system or "",
            user=prompt.user,
            response_format=ResponseFormat(kind="json_schema", schema=_CRITIQUE_SCHEMA),
            limits=StageLimits(temperature=0.0, max_output_tokens=structured_output_tokens),
            correlation=Correlation(project_id=project_id, unit_id=unit_key, round=rounds_used),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    )
    return CritiqueOutcome(
        critique=parse_critique(result.text, improvement_delta=improvement_delta),
        result=result,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )


@dataclass(frozen=True, slots=True)
class RevisionOutcome:
    """One revision pass: the proposed text, and what produced it."""

    text: str
    result: Any
    prompt_id: str
    prompt_version: str
    prompt_sha256: str


def run_revision(
    gateway: InferenceGateway,
    *,
    project_id: str,
    unit_key: str,
    context: str,
    content: str,
    findings: Sequence[AuditFinding],
    round_number: int,
    max_output_tokens: int,
) -> RevisionOutcome:
    """Revise a unit against findings. The result is a **proposal**, not a new version.

    Whether it becomes one is decided afterwards: a revision that increases validation failures is
    rejected and the previous version kept.
    """
    prompt = render(
        "stages.revise.improve",
        {"context": context, "current_text": content, "findings": render_findings(findings)},
    )
    result = gateway.run(
        StageRequest(
            stage="revise",
            system=prompt.system or "",
            user=prompt.user,
            limits=StageLimits(temperature=0.2, max_output_tokens=max_output_tokens),
            correlation=Correlation(project_id=project_id, unit_id=unit_key, round=round_number),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    )
    return RevisionOutcome(
        text=result.text,
        result=result,
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
    )
