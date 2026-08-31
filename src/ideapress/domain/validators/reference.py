"""Reference-integrity checks: internal links resolve, citations name a real source."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ideapress.domain.requirements import normalise_for_matching
from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["ReferenceIntegrityValidator"]

_INTERNAL_REFERENCE = re.compile(r"\[[^\]]+\]\(#([^)]+)\)")
_UNIT_REFERENCE = re.compile(r"\b(U-\d{2,})\b")
_CITATION = re.compile(r"\[\^?([^\]\d][^\]]{2,60})\]\((?!http|#)([^)]*)\)")
_HEADING = re.compile(r"^#{1,6}\s*(.+)$", re.MULTILINE)


def _anchor(heading: str) -> str:
    """The GitHub-style anchor a heading produces."""
    folded = normalise_for_matching(heading)
    return re.sub(r"[^a-z0-9\s-]", "", folded).replace(" ", "-")


class ReferenceIntegrityValidator:
    """Whether every reference points at something that exists."""

    kind = "reference"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the reference family."""
        return (
            self._internal_anchors(context.text),
            self._unit_references(context),
            self._citations(context),
        )

    def _internal_anchors(self, text: str) -> ValidationOutcome:
        """A ``[link](#anchor)` must name a heading in this unit."""
        anchors = {_anchor(heading) for heading in _HEADING.findall(text)}
        broken = [
            reference
            for reference in _INTERNAL_REFERENCE.findall(text)
            if _anchor(reference) not in anchors
        ]
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="internal_anchors",
            passed=not broken,
            blocking=True,
            detail=(
                "every internal link resolves"
                if not broken
                else f"{len(broken)} internal link(s) point at no heading here"
            ),
            evidence=tuple(broken[:5]),
        )

    def _unit_references(self, context: ValidationContext) -> ValidationOutcome:
        """A unit key mentioned in the text must be a unit of this project."""
        known = set(context.committed_units) | (
            {context.unit.key} if context.unit is not None else set()
        )
        if not known:
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="unit_references",
                passed=True,
                blocking=True,
                detail="no unit context to check against",
            )
        mentioned = set(_UNIT_REFERENCE.findall(context.text))
        unknown = sorted(mentioned - known)
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="unit_references",
            passed=not unknown,
            blocking=True,
            detail=(
                "every unit reference resolves"
                if not unknown
                else f"references unit(s) that do not exist: {', '.join(unknown)}"
            ),
            evidence=tuple(unknown[:5]),
        )

    def _citations(self, context: ValidationContext) -> ValidationOutcome:
        """A citation must name a source the project actually holds.

        Advisory rather than blocking: a project with no attached sources cannot have a broken
        citation, and a prose unit that reads like a citation is more often a turn of phrase than
        a defect. Risk M2 is real, but the *fact-check* stage is where it is answered.
        """
        if not context.source_titles:
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="citations",
                passed=True,
                blocking=False,
                detail="no sources attached; nothing to cite",
            )
        titles = {normalise_for_matching(title) for title in context.source_titles}
        unresolved = [
            target
            for _, target in _CITATION.findall(context.text)
            if target and normalise_for_matching(target) not in titles
        ]
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="citations",
            passed=not unresolved,
            blocking=False,
            detail=(
                "every citation names an attached source"
                if not unresolved
                else f"{len(unresolved)} citation(s) name no attached source"
            ),
            evidence=tuple(unresolved[:5]),
        )
