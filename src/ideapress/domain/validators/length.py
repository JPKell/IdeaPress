"""Length checks: the word band a unit was planned to hit."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["DEFAULT_MAX_WORDS", "DEFAULT_MIN_WORDS", "LengthValidator"]

_WORD = re.compile(r"\b[\w'-]+\b", re.UNICODE)

DEFAULT_MIN_WORDS: Final = 40
"""A floor for a unit with no planned target. Deliberately low: it exists to catch a stub, not to
have an opinion about length."""

DEFAULT_MAX_WORDS: Final = 4000
_BAND_LOWER: Final = 0.5
_BAND_UPPER: Final = 1.8


class LengthValidator:
    """Whether the unit is within the band its plan asked for.

    The band is wide — half to nearly twice the target — because a length target is a planning
    estimate, not a contract, and risk T4 is validators too strict blocking legitimate content.
    Falling short is **blocking** (a stub is not a unit); running long is **advisory** (an editor
    can cut, and refusing good writing for being generous would be the trap).
    """

    kind = "length"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the length family against the unit's planned target, if it has one."""
        words = len(_WORD.findall(context.text))
        target = context.unit.target_words if context.unit is not None else None
        floor = int(target * _BAND_LOWER) if target else DEFAULT_MIN_WORDS
        ceiling = int(target * _BAND_UPPER) if target else DEFAULT_MAX_WORDS
        return (
            ValidationOutcome(
                check_kind=self.kind,
                check_key="minimum_words",
                passed=words >= floor,
                blocking=True,
                detail=f"{words} words; at least {floor} expected",
                evidence=(str(words),),
            ),
            ValidationOutcome(
                check_kind=self.kind,
                check_key="maximum_words",
                passed=words <= ceiling,
                blocking=False,
                detail=f"{words} words; around {ceiling} expected at most",
                evidence=(str(words),),
            ),
        )
