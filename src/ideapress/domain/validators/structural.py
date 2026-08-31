"""Structural checks: heading depth, section presence, lists, truncation, unclosed markup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["StructuralValidator"]

_HEADING = re.compile(r"^(#{1,6})\s*(.*)$")
_LIST_ITEM = re.compile(r"^\s*([-*+]|\d+[.)])\s+")
_FENCE = re.compile(r"^\s*(```|~~~)")
# A sentence that stops without a terminator, mid-word or mid-clause. Deliberately narrow: a
# heading, a list item, a table row and a fenced block all legitimately end without punctuation.
_SENTENCE_END: Final = frozenset(".!?:;\"'`)]}—-")
_MAX_HEADING_DEPTH: Final = 4


class StructuralValidator:
    """Whether the text is shaped like a document rather than like an interrupted one."""

    kind = "structural"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the structural family.

        Returns:
            One outcome per check. All blocking: a truncated sentence or unclosed markup is
            content that would render wrong, and no amount of style preference makes it fine.
        """
        text = context.text
        lines = text.splitlines()
        return (
            self._non_empty(text),
            self._heading_depth(lines),
            self._no_unclosed_fence(lines),
            self._no_unclosed_emphasis(text),
            self._lists_well_formed(lines),
            self._no_truncated_ending(text),
        )

    def _outcome(
        self, key: str, *, passed: bool, detail: str, evidence: tuple[str, ...] = ()
    ) -> ValidationOutcome:
        return ValidationOutcome(
            check_kind=self.kind,
            check_key=key,
            passed=passed,
            blocking=True,
            detail=detail,
            evidence=evidence,
        )

    def _non_empty(self, text: str) -> ValidationOutcome:
        """The first check anything else depends on, and the one a truncated generation trips."""
        stripped = text.strip()
        return self._outcome(
            "non_empty",
            passed=bool(stripped),
            detail=f"{len(stripped)} characters" if stripped else "the unit is empty",
        )

    def _heading_depth(self, lines: Sequence[str]) -> ValidationOutcome:
        """No heading deeper than four levels: below that a reader has lost the thread."""
        too_deep = [
            line.strip()
            for line in lines
            if (match := _HEADING.match(line)) and len(match.group(1)) > _MAX_HEADING_DEPTH
        ]
        return self._outcome(
            "heading_depth",
            passed=not too_deep,
            detail=(
                f"deepest heading is level {_MAX_HEADING_DEPTH} or less"
                if not too_deep
                else f"{len(too_deep)} heading(s) deeper than level {_MAX_HEADING_DEPTH}"
            ),
            evidence=tuple(too_deep[:3]),
        )

    def _no_unclosed_fence(self, lines: Sequence[str]) -> ValidationOutcome:
        """An odd number of fences means a code block runs to the end of the document."""
        fences = sum(1 for line in lines if _FENCE.match(line))
        return self._outcome(
            "closed_code_fences",
            passed=fences % 2 == 0,
            detail=f"{fences} fence marker(s)" + ("" if fences % 2 == 0 else "; one is unclosed"),
        )

    def _no_unclosed_emphasis(self, text: str) -> ValidationOutcome:
        """Unbalanced ``**``. Checked outside code fences, where asterisks are just characters."""
        outside = _strip_fenced(text)
        count = outside.count("**")
        return self._outcome(
            "closed_emphasis",
            passed=count % 2 == 0,
            detail=f"{count} '**' marker(s)" + ("" if count % 2 == 0 else "; one is unclosed"),
        )

    def _lists_well_formed(self, lines: Sequence[str]) -> ValidationOutcome:
        """A list item with no content after its marker is a list the writer abandoned."""
        empty = [
            line
            for line in lines
            if line.strip() in {"-", "*", "+"} or re.fullmatch(r"\s*\d+[.)]\s*", line)
        ]
        return self._outcome(
            "lists_well_formed",
            passed=not empty,
            detail="no empty list items" if not empty else f"{len(empty)} empty list item(s)",
            evidence=tuple(item.strip() for item in empty[:3]),
        )

    def _no_truncated_ending(self, text: str) -> ValidationOutcome:
        """The last prose line ends with punctuation, not mid-clause.

        Only prose: a document that ends on a heading, a list item, a table row or a code fence is
        ending on a structure, and demanding a full stop there would fail correct writing.
        """
        stripped = text.rstrip()
        if not stripped:
            return self._outcome("complete_ending", passed=False, detail="the unit is empty")
        last = stripped.splitlines()[-1].rstrip()
        structural = (
            _HEADING.match(last)
            or _LIST_ITEM.match(last)
            or _FENCE.match(last)
            or last.startswith(("|", ">"))
        )
        passed = bool(structural) or (bool(last) and last[-1] in _SENTENCE_END)
        return self._outcome(
            "complete_ending",
            passed=passed,
            detail="ends completely" if passed else "the last line stops mid-sentence",
            evidence=() if passed else (last[-80:],),
        )


def _strip_fenced(text: str) -> str:
    """Return ``text`` with fenced code blocks removed, so their contents are not misread."""
    out: list[str] = []
    inside = False
    for line in text.splitlines():
        if _FENCE.match(line):
            inside = not inside
            continue
        if not inside:
            out.append(line)
    return "\n".join(out)
