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
    demands_grounding: bool = False
    """Whether the requirement asks for claims to rest on evidence (ADR-0043)."""
    checked_against_source: bool = False
    """Whether a source existed for `fact_check` to check those claims against.

    ADR-0043 §3: *satisfied* and *satisfied against no source* are different states, and reporting
    them the same way is what let M8 commit invented figures under a green report. A blocking
    grounding requirement in a project with no sources is refused at plan time (§1), so this can
    only be false for a **non-blocking** one — which is exactly the case the refusal deliberately
    lets through, and therefore exactly the case the report must be honest about.
    """

    @property
    def satisfied_label(self) -> str:
        """`yes`, `no`, or `yes` qualified by what could not be checked."""
        if not self.satisfied:
            return "no"
        if self.demands_grounding and not self.checked_against_source:
            return "yes — not checked against any source"
        return "yes"

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
class IncompleteUnit:
    """A planned unit that never committed, and the reason it did not.

    Attributes:
        key, ordinal, title, goal: The unit as planned.
        state: Where it stopped — ``planned``, ``paused``, ``drafting`` …
        reason: The pause reason when there is one, verbatim, so the export names the same remedy
            the unit page does.
        requirement_keys: The requirements that were this unit's to answer, and which therefore
            nothing answered.
    """

    key: str
    ordinal: int
    title: str
    goal: str
    state: str
    reason: str = ""
    requirement_keys: tuple[str, ...] = ()


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
    planned_units: int = 0
    """How many units the plan called for, committed or not.

    Carried so the provenance block can state planned-versus-committed rather than reporting the
    committed count alone. An export that says `Units: 4` for a five-unit plan is not wrong about
    what it contains; it is silent about what it does not, which is the same thing to a reader.
    """
    incomplete_units: tuple[IncompleteUnit, ...] = field(default_factory=tuple)
    """Planned units with no committed version, and why — a paused unit's reason included.

    Their **content** is deliberately absent: an export is of work that passed its gates, and
    putting an unvalidated draft in a file the user is entitled to trust would be worse than
    omitting it. Their *existence* is not optional.
    """

    @property
    def word_count(self) -> int:
        """The whole document's length."""
        return sum(unit.word_count for unit in self.units)

    @property
    def unit_version_ids(self) -> tuple[str, ...]:
        """The content hashes of the exported versions, for the export record."""
        return tuple(unit.content_hash for unit in self.units)

    def coverage_rows(self) -> Sequence[RequirementCoverage]:
        """Every requirement **once**, in requirement-key order, answered or not.

        Returns:
            One row per requirement key. A requirement several units share appears once, not once
            per unit. A requirement whose only unit never committed appears with
            ``satisfied=False`` and the reason, rather than not appearing.

        Two defects lived in the old shape, which walked the committed units and emitted a row per
        unit-requirement pair. A requirement assigned to four units produced four identical rows,
        implying four requirements. And a requirement whose unit paused produced **none** — so an
        export of a partially committed project listed only requirements that were met, every row
        reading `Satisfied: yes`, and the one it would have failed was simply absent. A reader saw
        a complete document. The coverage table exists for exactly that question.

        Sorted explicitly by key: a dict iterated in insertion order would make the document depend
        on the order rows came back from the database, which is not a guarantee any database makes.
        """
        by_key: dict[str, RequirementCoverage] = {}
        for unit in self.units:
            for entry in unit.coverage:
                seen = by_key.get(entry.key)
                # A requirement several units share is satisfied when every unit that owes it
                # satisfied it; one unsatisfied answer is the one worth reporting.
                if seen is None or (seen.satisfied and not entry.satisfied):
                    by_key[entry.key] = entry
        for incomplete in self.incomplete_units:
            for key in incomplete.requirement_keys:
                if key in by_key:
                    continue
                by_key[key] = RequirementCoverage(
                    key=key,
                    text="",
                    blocking=True,
                    satisfied=False,
                    satisfied_by="not_addressed",
                    detail=(
                        f"no committed unit answers it — {incomplete.key} "
                        f"({incomplete.title}) is {incomplete.state}"
                        + (f": {incomplete.reason}" if incomplete.reason else "")
                    ),
                    checks="",
                )
        return [by_key[key] for key in sorted(by_key)]

    @property
    def is_complete(self) -> bool:
        """Whether every planned unit committed."""
        return not self.incomplete_units
