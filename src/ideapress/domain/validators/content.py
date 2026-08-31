"""Content-constraint checks: required and forbidden phrases, and banned meta-commentary."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from ideapress.domain.requirements import evaluate_check, normalise_for_matching
from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["META_COMMENTARY_PATTERNS", "ContentConstraintValidator"]

META_COMMENTARY_PATTERNS: Final[tuple[str, ...]] = (
    r"\bas an ai\b",
    r"\bas a language model\b",
    r"\bi'?m an ai\b",
    r"\bi am an ai\b",
    r"\bi cannot (?:provide|generate|create)\b",
    r"\bhere(?:'s| is) (?:the|a|an) (?:draft|article|section|response)\b",
    r"\bi hope this helps\b",
    r"\blet me know if\b",
    r"\bcertainly[!,]\s",
    r"\bin conclusion, (?:as|this) (?:an|ai)\b",
)
"""Phrases that are the model talking about the task rather than doing it.

Written as patterns rather than literals because the shapes vary ("I'm an AI", "I am an AI"), and
they are **this module's** patterns, not a model's — workflows §11's prohibition is on a *model*
supplying a pattern, not on the application having one."""

_COMPILED: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(pattern, re.IGNORECASE) for pattern in META_COMMENTARY_PATTERNS
)


class ContentConstraintValidator:
    """Required phrases, forbidden phrases, and the model narrating itself."""

    kind = "content"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the content family: every requirement's own checks, plus the project's."""
        outcomes: list[ValidationOutcome] = [
            self._no_meta_commentary(context.text),
            self._no_forbidden_phrases(context),
        ]
        outcomes.extend(self._requirement_checks(context))
        return tuple(outcomes)

    def _no_meta_commentary(self, text: str) -> ValidationOutcome:
        """The model must produce the work, not describe producing it."""
        found = [
            match.group(0) for pattern in _COMPILED if (match := pattern.search(text)) is not None
        ]
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="no_meta_commentary",
            passed=not found,
            blocking=True,
            detail=(
                "no meta-commentary"
                if not found
                else f"the model narrates itself: {', '.join(repr(f) for f in found[:3])}"
            ),
            evidence=tuple(found[:3]),
        )

    def _no_forbidden_phrases(self, context: ValidationContext) -> ValidationOutcome:
        """Text the project said must not appear."""
        haystack = normalise_for_matching(context.text)
        present = [
            phrase
            for phrase in context.forbidden_phrases
            if normalise_for_matching(phrase) in haystack
        ]
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="no_forbidden_phrases",
            passed=not present,
            blocking=True,
            detail=(
                "no forbidden phrase appears"
                if not present
                else f"forbidden text present: {', '.join(present)}"
            ),
            evidence=tuple(present[:5]),
        )

    def _requirement_checks(self, context: ValidationContext) -> list[ValidationOutcome]:
        """Every compiled requirement's own deterministic checks, run here rather than at coverage.

        The same checks decide coverage later. Running them at validation too is deliberate: a
        blocking requirement's check failing is a *repairable* defect, and finding it before the
        audit stage saves a model call that would only rediscover it.
        """
        outcomes: list[ValidationOutcome] = []
        for requirement in context.requirements:
            for outcome in (evaluate_check(check, context.text) for check in requirement.checks):
                outcomes.append(
                    ValidationOutcome(
                        check_kind=self.kind,
                        check_key=f"requirement:{requirement.key}:{outcome.check.kind}",
                        passed=outcome.passed,
                        blocking=requirement.blocking,
                        detail=f"{requirement.key}: {outcome.detail}",
                        evidence=outcome.evidence[:5],
                    )
                )
        return outcomes
