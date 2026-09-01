"""ideapress.domain.exporters.model — the one rendering model every format serialises.

Risk G4 is export formats multiplying, and the mitigation is a shared rendering model with
format-specific serializers only. Every exporter takes a :class:`ExportDocument` and writes bytes;
none of them reads the database, a clock, or the environment.

**Determinism is a property of this module as much as of the serializers.** The document is
assembled from committed data only — no "generated at" wall-clock, no locale-dependent formatting,
no iteration over an unsorted collection — so two exports of the same committed project are
byte-identical by construction rather than by care (spec §11 contract 4, risk T8).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "EXPORT_FORMAT_VERSION",
    "ExportDocument",
    "ExportUnit",
    "RequirementCoverage",
    "UnitProvenance",
]

EXPORT_FORMAT_VERSION: Final = "1.0"
"""The export format's own version (spec §19).

Recorded on every export row and embedded in every rendered document, so a re-export of an old
project can be checked against the version it was produced under rather than assumed compatible."""


@dataclass(frozen=True, slots=True)
class RequirementCoverage:
    """One requirement and how this unit answered it.

    Attributes:
        key, text, blocking: The requirement as compiled.
        satisfied, satisfied_by, detail: What the coverage gate decided, and on what.
        checks: The deterministic checks, described; the honest phrase when there are none.
        source_document: Which piece of author material grounds the requirement.
        source_quote: The **verbatim** span of that document the compiler cited. This is risk
            T6's residual-case evidence — a model can attach an unrelated requirement to a real
            quote, and the only mitigation is that a person reads the claim and its evidence side
            by side — so it travels into every export, not only the live views (M7 finding 2).
        source_anchor: An optional section name, for display.

    The three source fields are empty only when the requirement row could not be resolved at
    all — the same degenerate case that leaves ``text`` empty.
    """

    key: str
    text: str
    blocking: bool
    satisfied: bool
    satisfied_by: str
    detail: str
    checks: str
    source_document: str = ""
    source_quote: str = ""
    source_anchor: str | None = None

    @property
    def is_mechanical(self) -> bool:
        """Whether a deterministic check decided it, rather than a model."""
        return self.satisfied_by == "deterministic_check"

    @property
    def source_label(self) -> str:
        """How the grounding reference reads: ``brief#privacy`` or ``brief``."""
        if self.source_anchor:
            return f"{self.source_document}#{self.source_anchor}"
        return self.source_document


@dataclass(frozen=True, slots=True)
class UnitProvenance:
    """What produced one unit — workflows §8's list, flattened for rendering."""

    stage: str
    attempt: int
    round: int
    outcome: str
    backend: str
    model_canonical_id: str | None
    prompt_id: str | None
    prompt_version: str | None
    prompt_sha256: str | None
    response_hash: str | None
    input_tokens: int | None
    output_tokens: int | None
    provider_ms: float | None
    degradations: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExportUnit:
    """One committed unit, with everything a reader or an auditor needs."""

    key: str
    ordinal: int
    title: str
    goal: str
    content: str
    version: int
    content_hash: str
    word_count: int
    committed_at: str
    coverage: tuple[RequirementCoverage, ...] = ()
    provenance: tuple[UnitProvenance, ...] = ()
    findings: tuple[dict[str, Any], ...] = ()
    critiques: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class ExportDocument:
    """A committed project, ready to serialise.

    Attributes:
        project_id, title, slug: Identity.
        content_type, content_type_version: Which content type produced it.
        workflow_id, workflow_version: Which workflow.
        brief: The author's brief.
        units: The committed units, in reading order.
        format_version: :data:`EXPORT_FORMAT_VERSION` at the time of writing.
        review_findings: The project-level review's findings, when one has run.

    **There is deliberately no ``generated_at``.** A wall-clock stamp is the single most common
    cause of an export that differs from itself (risk T8), and it carries nothing a reader needs
    that the units' own ``committed_at`` does not already say.
    """

    project_id: str
    title: str
    slug: str
    content_type: str
    content_type_version: str
    workflow_id: str
    workflow_version: str
    brief: str
    units: tuple[ExportUnit, ...]
    format_version: str = EXPORT_FORMAT_VERSION
    review_findings: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def word_count(self) -> int:
        """The whole document's length."""
        return sum(unit.word_count for unit in self.units)

    @property
    def unit_version_ids(self) -> tuple[str, ...]:
        """The content hashes of the exported versions, for the export record."""
        return tuple(unit.content_hash for unit in self.units)

    def coverage_rows(self) -> Sequence[RequirementCoverage]:
        """Every unit's coverage, in unit order then requirement-key order.

        Sorted explicitly. A set or a dict iterated in insertion order would produce a document
        that depends on the order rows came back from the database, which is not a guarantee any
        database makes.
        """
        rows: list[RequirementCoverage] = []
        for unit in self.units:
            rows.extend(sorted(unit.coverage, key=lambda entry: entry.key))
        return rows
