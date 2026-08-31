"""Consistency checks: project terms spelled the project's way, across units."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["ConsistencyValidator"]


class ConsistencyValidator:
    """Whether the unit uses the project's own vocabulary.

    Risk M4 is style drift across units, and a glossary is the only part of it a deterministic
    check can reach: names and terms have a required spelling, and a variant is a real defect that
    a reader notices. Everything else about consistency — tone, register, argument — is what the
    `project_review` stage is for, and pretending a regex could judge it would be exactly the
    fake measurement the suite refuses.
    """

    kind = "consistency"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the consistency family."""
        return (self._glossary_terms(context),)

    def _glossary_terms(self, context: ValidationContext) -> ValidationOutcome:
        """Every glossary variant must appear in its canonical spelling instead.

        The glossary maps a **variant** to the **canonical** term: ``{"AI suite": "the suite"}``
        means "if you wrote 'AI suite', write 'the suite'". Matching is whole-word and
        case-insensitive, so ``rerank`` does not fire on ``reranking``.
        """
        if not context.glossary:
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="glossary_terms",
                passed=True,
                blocking=False,
                detail="no glossary; nothing to check",
            )
        offenders: list[str] = []
        for variant, canonical in context.glossary.items():
            if not variant.strip():
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(variant)}(?!\w)", re.IGNORECASE)
            if pattern.search(context.text):
                offenders.append(f"{variant!r} → {canonical!r}")
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="glossary_terms",
            passed=not offenders,
            blocking=False,
            detail=(
                "every term matches the glossary"
                if not offenders
                else f"{len(offenders)} term(s) differ from the project glossary"
            ),
            evidence=tuple(offenders[:5]),
        )
