"""ideapress.services.requirements — compiling requirements, and refusing what is not grounded.

The stage runs one bounded model task through the gateway and then **Python decides** what survives.
Every candidate the model returns is passed through
:func:`~ideapress.domain.requirements.ground_requirement`, which refuses anything whose quotation is
not a verbatim span of the author material. Nothing a model returns becomes a requirement because
the model said so.

The gate on the *stage* is separate and stricter still: a compilation that produced no requirement
at all does not satisfy the plan gate (workflows §2 stage 1 — "every requirement has an ID and a
checkable statement"), because a project with a brief full of constraints and an empty requirement
list is the T1 failure wearing a different hat.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from baseaicore import ValidationError

from ideapress.domain.inference import (
    Correlation,
    ResponseFormat,
    StageLimits,
    StageRequest,
    StageResult,
)
from ideapress.domain.requirements import (
    MIN_QUOTE_CHARS,
    CompiledBy,
    Requirement,
    RequirementCheck,
    SourceReference,
    ground_requirement,
    requirement_key,
)
from ideapress.services.prompts import render

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ideapress.services.inference import InferenceGateway

__all__ = [
    "COMPILE_PROMPT_ID",
    "CompilationResult",
    "RejectedRequirement",
    "assemble_documents",
    "compile_requirements",
    "parse_candidates",
]

logger = logging.getLogger(__name__)

COMPILE_PROMPT_ID = "stages.requirements.compile"

STRUCTURED_OUTPUT_TOKENS = 8192
"""The output budget for a structured-extraction stage.

**Includes the model's reasoning**, which is why it is this large. Measured on the reference
machine: `qwen3.5:9b-q8_0` compiling requirements from a six-line brief produced **nothing at all**
at 4 096 tokens — the whole allowance went on thinking — and completed in 278 tokens of answer at
8 192. A budget that a reasoning model cannot finish thinking inside returns an empty string, which
a JSON parser then reports as a malformed answer rather than as an absent one."""

_REQUIREMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["requirements"],
    "additionalProperties": False,
    "properties": {
        "requirements": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "blocking", "source_document", "source_quote"],
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "blocking": {"type": "boolean"},
                    "source_document": {"type": "string"},
                    "source_quote": {"type": "string"},
                    "source_anchor": {"type": "string"},
                    "checks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["kind"],
                            "additionalProperties": False,
                            "properties": {
                                "kind": {"type": "string"},
                                "values": {"type": "array", "items": {"type": "string"}},
                                "threshold": {"type": "integer"},
                            },
                        },
                    },
                },
            },
        }
    },
}


@dataclass(frozen=True, slots=True)
class RejectedRequirement:
    """A candidate the compiler returned and Python refused.

    Kept and shown, never silently dropped: a rejection is the anti-fabrication mechanism doing its
    job, and the user is entitled to see what the model tried to assert and why it did not stand.
    """

    text: str
    reason: str
    cited_document: str
    quote: str


@dataclass(frozen=True, slots=True)
class CompilationResult:
    """What one requirement-compilation stage produced.

    Attributes:
        requirements: The grounded requirements, keyed ``R-001``… in the order returned.
        rejected: Candidates that failed grounding, with the reason.
        prompt_id, prompt_version, prompt_sha256: Provenance for the compilation itself.
        raw_text: What the model actually said, for the attempt record.
        result: The full stage result, so the attempt records the model, usage and timing that
            produced the compilation — workflows §8 wants those on *every* attempt, and a plan
            attempt with no model identity is a hole in the provenance of everything built on it.
    """

    requirements: tuple[Requirement, ...]
    rejected: tuple[RejectedRequirement, ...]
    prompt_id: str
    prompt_version: str
    prompt_sha256: str
    raw_text: str
    result: StageResult

    @property
    def blocking(self) -> tuple[Requirement, ...]:
        """The requirements that gate the commit."""
        return tuple(r for r in self.requirements if r.blocking)


def assemble_documents(*, brief: str, sources: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect the author material Python will let the model see, and check quotes against.

    Args:
        brief: The project's brief.
        sources: Attached source documents, by title.

    Returns:
        A mapping of document name to text, always containing ``"brief"``. This is assembled by
        Python — the model never chooses what it reads, and the same mapping is what
        :func:`~ideapress.domain.requirements.ground_requirement` checks quotations against, so a
        quote from a document the model was not shown cannot pass.
    """
    documents = {"brief": brief}
    for title, text in (sources or {}).items():
        if title != "brief":
            documents[title] = text
    return documents


def _render_documents(documents: Mapping[str, str]) -> str:
    """Render the material for the prompt, one labelled block per document, in a fixed order."""
    return "\n\n".join(f"### {name}\n{text.strip()}" for name, text in sorted(documents.items()))


def parse_candidates(text: str) -> list[dict[str, Any]]:
    """Read the model's answer into candidate dictionaries.

    Args:
        text: What the model returned.

    Returns:
        The ``requirements`` array, or an empty list when the model returned an empty one.

    Raises:
        ValidationError: The answer is not a JSON object with a ``requirements`` array. Malformed
            output is retried by the stage runner and then fails cleanly — it is never committed,
            and it is never "recovered" by guessing what the model meant.
    """
    stripped = text.strip()
    # A model that wraps JSON in a fenced block is answering the question; unwrapping one fence is
    # tolerance for formatting, not for content.
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        stripped = stripped.rsplit("```", 1)[0]
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        message = f"The requirement compiler did not return JSON: {exc}"
        raise ValidationError(message, details={"answer": text[:400]}) from exc
    if not isinstance(payload, dict) or "requirements" not in payload:
        message = "The requirement compiler's answer has no 'requirements' array."
        raise ValidationError(message, details={"answer": text[:400]})
    candidates = payload["requirements"]
    if not isinstance(candidates, list):
        message = "'requirements' is not an array."
        raise ValidationError(message, details={"answer": text[:400]})
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _build_checks(raw: Any) -> list[RequirementCheck]:
    """Turn a candidate's check dictionaries into validated checks, dropping the invalid ones.

    A malformed check is dropped rather than failing the whole requirement: the requirement may
    still be grounded and useful, and it is then honestly recorded as having no mechanical check.
    """
    checks: list[RequirementCheck] = []
    for entry in raw if isinstance(raw, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            checks.append(
                RequirementCheck(
                    kind=entry.get("kind", ""),
                    values=tuple(str(v) for v in entry.get("values", ()) if str(v).strip()),
                    threshold=entry.get("threshold"),
                )
            )
        except ValidationError as exc:
            logger.info("requirements.check_dropped", extra={"detail": exc.message})
    return checks


def compile_requirements(
    gateway: InferenceGateway,
    *,
    project_id: str,
    brief: str,
    sources: Mapping[str, str] | None = None,
    generation: int = 1,
    attempt: int = 1,
) -> CompilationResult:
    """Compile requirements from the author material, keeping only what is grounded in it.

    Args:
        gateway: The single choke point every stage reaches a model through.
        project_id: For correlation and provenance.
        brief: The project's brief.
        sources: Attached source documents, by title.
        generation: Which compilation generation this is. Recompiling makes a new one; the old
            rows are retained, because a project records which generation it is working against.
        attempt: Which attempt within the stage.

    Returns:
        The grounded requirements and the rejected candidates, with full prompt provenance.

    Raises:
        ValidationError: The model's answer was not parseable as the declared shape.

    Every candidate goes through the domain's grounding rule. A rejection is recorded, not hidden:
    the compilation is "a separate reviewable artefact" (risk T6's mitigation), and a reviewer who
    cannot see what was rejected is reviewing half of it.
    """
    documents = assemble_documents(brief=brief, sources=sources)
    prompt = render(
        COMPILE_PROMPT_ID,
        {"documents": _render_documents(documents), "min_quote_chars": str(MIN_QUOTE_CHARS)},
    )
    result = gateway.run(
        StageRequest(
            stage="requirements",
            system=prompt.system or "",
            user=prompt.user,
            response_format=ResponseFormat(kind="json_schema", schema=_REQUIREMENTS_SCHEMA),
            limits=StageLimits(temperature=0.0, max_output_tokens=STRUCTURED_OUTPUT_TOKENS),
            correlation=Correlation(project_id=project_id, attempt=attempt),
            prompt_id=prompt.prompt_id,
            prompt_version=prompt.version,
            prompt_sha256=prompt.sha256,
        )
    )

    compiled_by = CompiledBy(
        prompt_id=prompt.prompt_id, version=prompt.version, prompt_sha256=prompt.sha256
    )
    requirements: list[Requirement] = []
    rejected: list[RejectedRequirement] = []
    for candidate in parse_candidates(result.text):
        source = SourceReference(
            document=str(candidate.get("source_document", "")),
            quote=str(candidate.get("source_quote", "")),
            anchor=(str(candidate["source_anchor"]) if candidate.get("source_anchor") else None),
        )
        try:
            requirements.append(
                ground_requirement(
                    key=requirement_key(len(requirements) + 1),
                    text=str(candidate.get("text", "")),
                    blocking=bool(candidate.get("blocking", False)),
                    source=source,
                    compiled_by=compiled_by,
                    checks=_build_checks(candidate.get("checks")),
                    documents=documents,
                    generation=generation,
                )
            )
        except ValidationError as exc:
            logger.info("requirements.rejected", extra={"detail": exc.message})
            rejected.append(
                RejectedRequirement(
                    text=str(candidate.get("text", ""))[:300],
                    reason=exc.message,
                    cited_document=source.document,
                    quote=source.quote[:200],
                )
            )

    return CompilationResult(
        requirements=tuple(requirements),
        rejected=tuple(rejected),
        prompt_id=prompt.prompt_id,
        prompt_version=prompt.version,
        prompt_sha256=prompt.sha256,
        raw_text=result.text,
        result=result,
    )
