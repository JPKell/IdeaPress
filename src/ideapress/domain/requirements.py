"""ideapress.domain.requirements — compiled requirements, and the rules that keep them honest.

Requirements are what make every later gate checkable rather than aesthetic (workflows §3). They
are compiled **once**, carried unchanged through every stage, and evaluated by deterministic checks
that Python runs — never by asking a model whether it thinks the requirement was met.

Two properties are load-bearing and both live here:

**Grounding (risk T6).** A compiled requirement carries a ``source`` naming a document and a
**verbatim quote** from it, and :func:`ground_requirement` refuses any requirement whose quote is
not actually a substring of the author material. A model that invents a constraint must also invent
its evidence, and invented evidence is not a substring. This is deterministic, cheap, and hard to
fool — but it is **not complete**, and the docstring says so where it matters: a model can quote
real text and attach an unrelated requirement to it. The mitigation for the residual case is that
the quote is displayed beside the requirement in every view and every export, so a person reviewing
the plan sees the evidence next to the claim.

**No model-supplied patterns.** Check kinds are a closed set of literal-string and numeric
comparisons. There is deliberately **no regular-expression check**: a pattern is a small program,
and workflows §11 forbids a model causing code execution. A model that wants a shape asks for
literal strings.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from baseaicore import ValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

__all__ = [
    "CHECK_KINDS",
    "MIN_QUOTE_CHARS",
    "CheckKind",
    "CheckOutcome",
    "CompiledBy",
    "Requirement",
    "RequirementCheck",
    "SourceReference",
    "evaluate_check",
    "evaluate_requirement",
    "ground_requirement",
    "normalise_for_matching",
    "requirement_key",
]

CheckKind = Literal[
    "must_contain_any",
    "must_contain_all",
    "must_not_contain",
    "min_words",
    "max_words",
    "heading_present",
]

CHECK_KINDS: Final[frozenset[str]] = frozenset(
    {
        "must_contain_any",
        "must_contain_all",
        "must_not_contain",
        "min_words",
        "max_words",
        "heading_present",
    }
)
"""The closed set. **No regular expressions**: a pattern is a small program, and a model does not
get to supply one (workflows §11). Extending this set is an architectural decision, not a
convenience — every kind is something a coverage report can explain to a person in one line."""

MIN_QUOTE_CHARS: Final = 24
"""How much verbatim text counts as grounding.

A requirement "supported" by the word "the" is not supported. Twenty-four characters is roughly a
short clause — long enough that a model must have read the material to produce it, short enough
that a genuinely terse constraint ("Never mention pricing.") still fits."""

_MAX_VALUES_PER_CHECK: Final = 32
_MAX_VALUE_CHARS: Final = 200
_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)


def normalise_for_matching(text: str) -> str:
    """Fold text for substring comparison, without changing what it says.

    Args:
        text: Any text — author material, a model's quote, a unit's draft.

    Returns:
        NFKC-normalised, lowercased, with every run of whitespace collapsed to one space. This is
        what makes a quote match across a line wrap the model did not reproduce, and what stops a
        non-breaking space or a smart quote from reading as different text. It is deliberately
        conservative: it never removes or reorders words, so it cannot make an ungrounded quote
        match.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.split())


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Where a requirement came from, and the evidence for it.

    Attributes:
        document: Which piece of author material — ``"brief"``, or a source's title.
        quote: A **verbatim** span of that document. Checked, not trusted.
        anchor: An optional section name, for display.
    """

    document: str
    quote: str
    anchor: str | None = None

    @property
    def label(self) -> str:
        """How this reference reads in a report: ``brief#privacy`` or ``brief``."""
        return f"{self.document}#{self.anchor}" if self.anchor else self.document


@dataclass(frozen=True, slots=True)
class CompiledBy:
    """Which prompt record compiled a requirement, so the plan itself has provenance."""

    prompt_id: str
    version: str
    prompt_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class RequirementCheck:
    """One deterministic check the coverage gate runs against a unit's text.

    Attributes:
        kind: One of :data:`CHECK_KINDS`.
        values: Literal strings, for the string kinds.
        threshold: The number, for ``min_words`` and ``max_words``.
    """

    kind: CheckKind
    values: tuple[str, ...] = ()
    threshold: int | None = None

    def __post_init__(self) -> None:
        """Refuse a check that could not be evaluated, or that is not really a check.

        Raises:
            ValidationError: The kind is unknown; a string kind has no values, or values that are
                blank or absurdly long; a numeric kind has no threshold or a negative one. Each is
                a way a compiled requirement can look like a guarantee and enforce nothing — a
                ``must_contain_any`` with an empty list passes against every text ever written.
        """
        if self.kind not in CHECK_KINDS:
            message = f"{self.kind!r} is not a check kind. Known: {', '.join(sorted(CHECK_KINDS))}."
            raise ValidationError(message, details={"kind": self.kind})
        if self.kind in {"min_words", "max_words"}:
            if self.threshold is None or self.threshold < 0:
                message = f"A {self.kind} check needs a non-negative threshold."
                raise ValidationError(message, details={"kind": self.kind})
            return
        if not self.values:
            message = (
                f"A {self.kind} check with no values passes against every text ever written, "
                "which is a guarantee that guarantees nothing."
            )
            raise ValidationError(message, details={"kind": self.kind})
        if len(self.values) > _MAX_VALUES_PER_CHECK:
            message = f"A check may name at most {_MAX_VALUES_PER_CHECK} values."
            raise ValidationError(message, details={"kind": self.kind, "count": len(self.values)})
        for value in self.values:
            if not value.strip():
                message = f"A {self.kind} check has a blank value."
                raise ValidationError(message, details={"kind": self.kind})
            if len(value) > _MAX_VALUE_CHARS:
                message = f"A check value may be at most {_MAX_VALUE_CHARS} characters."
                raise ValidationError(message, details={"kind": self.kind, "value": value[:60]})

    def describe(self) -> str:
        """One line a person can read in a coverage report."""
        if self.kind == "min_words":
            return f"at least {self.threshold} words"
        if self.kind == "max_words":
            return f"at most {self.threshold} words"
        joined = ", ".join(repr(value) for value in self.values)
        return {
            "must_contain_any": f"contains any of: {joined}",
            "must_contain_all": f"contains all of: {joined}",
            "must_not_contain": f"contains none of: {joined}",
            "heading_present": f"has a heading among: {joined}",
        }[self.kind]


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    """What one deterministic check found.

    Attributes:
        check: The check that ran.
        passed: Whether it passed.
        detail: What was observed, in a person's words.
        evidence: The matched (or missing) values, for the report.
    """

    check: RequirementCheck
    passed: bool
    detail: str
    evidence: tuple[str, ...] = ()


def _headings(text: str) -> list[str]:
    """Every Markdown ATX heading in ``text``, normalised."""
    return [
        normalise_for_matching(line.lstrip("#").strip())
        for line in text.splitlines()
        if line.lstrip().startswith("#")
    ]


def evaluate_check(check: RequirementCheck, text: str) -> CheckOutcome:
    """Run one deterministic check against a unit's text.

    Args:
        check: The check to run.
        text: The unit's content.

    Returns:
        The outcome, with what was observed. **No model is consulted**: this function is the
        difference between a gate and an opinion, and it is why five stages in workflows §2 involve
        no model at all.
    """
    haystack = normalise_for_matching(text)
    if check.kind == "min_words":
        count = len(_WORD.findall(text))
        threshold = check.threshold or 0
        return CheckOutcome(
            check=check,
            passed=count >= threshold,
            detail=f"{count} words; needs at least {threshold}",
            evidence=(str(count),),
        )
    if check.kind == "max_words":
        count = len(_WORD.findall(text))
        threshold = check.threshold or 0
        return CheckOutcome(
            check=check,
            passed=count <= threshold,
            detail=f"{count} words; allows at most {threshold}",
            evidence=(str(count),),
        )
    if check.kind == "heading_present":
        headings = _headings(text)
        wanted = [normalise_for_matching(value) for value in check.values]
        found = tuple(
            original
            for original, needle in zip(check.values, wanted, strict=True)
            if any(needle in heading for heading in headings)
        )
        return CheckOutcome(
            check=check,
            passed=bool(found),
            detail=(
                f"found heading(s): {', '.join(found)}"
                if found
                else f"no heading among {', '.join(check.values)}"
            ),
            evidence=found,
        )

    present = tuple(value for value in check.values if normalise_for_matching(value) in haystack)
    missing = tuple(value for value in check.values if value not in present)
    if check.kind == "must_contain_any":
        return CheckOutcome(
            check=check,
            passed=bool(present),
            detail=(
                f"found: {', '.join(present)}"
                if present
                else f"none of {', '.join(check.values)} appears"
            ),
            evidence=present or missing,
        )
    if check.kind == "must_contain_all":
        return CheckOutcome(
            check=check,
            passed=not missing,
            detail="all present" if not missing else f"missing: {', '.join(missing)}",
            evidence=missing or present,
        )
    return CheckOutcome(
        check=check,
        passed=not present,
        detail="none present" if not present else f"forbidden text present: {', '.join(present)}",
        evidence=present,
    )


@dataclass(frozen=True, slots=True)
class Requirement:
    """One compiled requirement.

    Immutable by construction and by rule: workflows §3 says requirements are compiled once and
    carried unchanged through every stage, and workflows §11 says a model may never modify them.
    """

    key: str
    text: str
    blocking: bool
    source: SourceReference
    compiled_by: CompiledBy
    checks: tuple[RequirementCheck, ...] = ()
    unit_keys: tuple[str, ...] = ()
    demands_grounding: bool = False
    """Whether this requirement asks for claims to rest on evidence (ADR-0043).

    Marked once at compile time and carried on the requirement, rather than re-derived from prose
    whenever a gate needs it: a property the system will refuse a plan over must be inspectable in
    the plan a person reads, not recomputed behind them.

    Its consequence is a refusal. A blocking requirement with this set, in a project with no
    sources attached, is unsatisfiable — there is nothing for a claim to be grounded in — and
    `plan build` says so instead of committing invented figures against it.
    """
    generation: int = 1

    @property
    def is_mechanically_checkable(self) -> bool:
        """Whether a deterministic check decides this requirement.

        A requirement with no check is evaluated by audit and **flagged as such** in the coverage
        report (workflows §3), so the user can see which guarantees are mechanical and which are
        model-assisted. That flag is the honest part; hiding the distinction would be the lie.
        """
        return bool(self.checks)

    def describe_checks(self) -> str:
        """The checks, joined for a report; the honest phrase when there are none."""
        if not self.checks:
            return "no deterministic check — evaluated by audit only"
        return "; ".join(check.describe() for check in self.checks)


def evaluate_requirement(requirement: Requirement, text: str) -> tuple[CheckOutcome, ...]:
    """Run every deterministic check for one requirement against a unit's text."""
    return tuple(evaluate_check(check, text) for check in requirement.checks)


def requirement_key(ordinal: int) -> str:
    """The stable key for the *n*-th requirement: ``R-001``, ``R-014``.

    Args:
        ordinal: 1-based position in the compiled generation.

    Returns:
        The key. Generated by IdeaPress, never by a model — risk S2's rule applied to identifiers:
        nothing a model produced becomes an identifier the system then trusts.
    """
    return f"R-{ordinal:03d}"


def ground_requirement(
    *,
    key: str,
    text: str,
    blocking: bool,
    source: SourceReference,
    compiled_by: CompiledBy,
    checks: Sequence[RequirementCheck] = (),
    documents: Mapping[str, str],
    generation: int = 1,
    demands_grounding: bool = False,
) -> Requirement:
    """Build a requirement, refusing one the author material does not support.

    Args:
        key: The IdeaPress-generated key.
        text: The requirement's statement.
        blocking: Whether it gates the commit.
        source: The document and the verbatim quote said to support it.
        compiled_by: Which prompt record produced it.
        checks: Its deterministic checks, if any.
        documents: The author material, by name — the brief and every source.
        generation: Which compilation generation this belongs to.
        demands_grounding: Whether it asks for claims to rest on evidence (ADR-0043).

    Returns:
        The requirement.

    Raises:
        ValidationError: Any of the four ways a compiled requirement can be dishonest:

            * **the statement is empty or unreadably short** — a requirement nobody can evaluate;
            * **the named document does not exist** in the author material;
            * **the quote is too short to ground anything** (under :data:`MIN_QUOTE_CHARS`);
            * **the quote is not a verbatim span of that document** — the fabrication marker. A
              model that invents a constraint must invent its evidence too, and invented evidence
              is not a substring of text it did not write.

    **What this does not catch, stated plainly:** a model can quote real text and attach an
    unrelated requirement to it. No deterministic check can settle that, because it is a semantic
    judgement about support. The mitigation is presentational and deliberate — the quote travels
    with the requirement into every view and every export, so a person reviewing the plan reads the
    claim and its evidence side by side. Risk T6 is *reduced* here, not eliminated, and a document
    that claimed otherwise would be the fabrication it warns about.
    """
    statement = text.strip()
    if len(statement) < 8:
        message = f"Requirement {key} has no statement anyone could evaluate: {text!r}"
        raise ValidationError(message, details={"requirement_key": key})

    document = documents.get(source.document)
    if document is None:
        message = (
            f"Requirement {key} cites {source.document!r}, which is not part of this project's "
            f"author material. Available: {', '.join(sorted(documents)) or 'nothing'}."
        )
        raise ValidationError(
            message,
            details={
                "requirement_key": key,
                "cited": source.document,
                "available": sorted(documents),
            },
        )

    quote = source.quote.strip()
    if len(quote) < MIN_QUOTE_CHARS:
        message = (
            f"Requirement {key} is grounded in {len(quote)} characters of quotation; "
            f"{MIN_QUOTE_CHARS} are needed for a quote to be evidence of anything."
        )
        raise ValidationError(
            message, details={"requirement_key": key, "quote": quote, "minimum": MIN_QUOTE_CHARS}
        )

    if normalise_for_matching(quote) not in normalise_for_matching(document):
        message = (
            f"Requirement {key} quotes text that does not appear in {source.document!r}. The "
            "compiler may not invent a requirement the source material does not support, and an "
            "invented quote is how that shows."
        )
        raise ValidationError(
            message,
            details={"requirement_key": key, "document": source.document, "quote": quote[:160]},
        )

    return Requirement(
        key=key,
        text=statement,
        blocking=blocking,
        source=source,
        compiled_by=compiled_by,
        checks=tuple(checks),
        generation=generation,
        demands_grounding=demands_grounding,
    )
