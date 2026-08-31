"""Format checks: front matter, and JSON validity for a structured unit."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["FormatValidator"]

_FRONT_MATTER = "---"


class FormatValidator:
    """Whether the unit parses as the kind of document it claims to be."""

    kind = "format"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the format family."""
        return (self._front_matter(context.text), self._json_body(context))

    def _front_matter(self, text: str) -> ValidationOutcome:
        """A document that opens a front-matter block must close it.

        A unit with no front matter passes: front matter is optional, and a validator that
        required it would fail every ordinary section.
        """
        lines = text.strip().splitlines()
        if not lines or lines[0].strip() != _FRONT_MATTER:
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="front_matter",
                passed=True,
                blocking=True,
                detail="no front matter, which is fine",
            )
        closed = any(line.strip() == _FRONT_MATTER for line in lines[1:])
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="front_matter",
            passed=closed,
            blocking=True,
            detail="front matter is closed" if closed else "front matter is never closed",
        )

    def _json_body(self, context: ValidationContext) -> ValidationOutcome:
        """A structured unit must be valid JSON. A prose unit is not checked as JSON at all."""
        if context.content_type != "structured":
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="json_body",
                passed=True,
                blocking=True,
                detail="not a structured unit; JSON validity does not apply",
            )
        try:
            json.loads(context.text)
        except json.JSONDecodeError as exc:
            return ValidationOutcome(
                check_kind=self.kind,
                check_key="json_body",
                passed=False,
                blocking=True,
                detail=f"not valid JSON: {exc}",
                evidence=(context.text[:120],),
            )
        return ValidationOutcome(
            check_kind=self.kind,
            check_key="json_body",
            passed=True,
            blocking=True,
            detail="valid JSON",
        )
