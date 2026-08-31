"""Safety checks: model output that would do something if it were rendered or executed.

Risk S1 — model output rendered unsafely — is the highest-impact security risk this application
carries, because it renders more model output than anything else in the suite. The **primary**
control is not here: it is autoescaping in every template and every exporter, applied by
construction rather than by inspection, and there is no `| safe` on model content anywhere.

This family is the second line: it *flags* what arrived so a person sees it, and so a repair can
ask for it to be removed. It is deliberately **advisory** for the flags that are only dangerous
when escaping fails — an article about web security that quotes a `<script>` tag is legitimate
writing, and blocking it would be the validator-too-strict trap (T4). What is **blocking** is the
one thing that is never legitimate content: a path that walks out of a directory, which risk S2
says must never reach a filesystem and which no piece of prose needs.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from ideapress.domain.validation import ValidationOutcome

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.validation import ValidationContext

__all__ = ["SafetyValidator"]

_SCRIPT = re.compile(r"<\s*script\b", re.IGNORECASE)
_EVENT_HANDLER = re.compile(r"\bon(?:click|error|load|mouseover)\s*=", re.IGNORECASE)
_JAVASCRIPT_URL = re.compile(r"javascript\s*:", re.IGNORECASE)
_TEMPLATE_SYNTAX = re.compile(r"\{\{.*?\}\}|\{%.*?%\}", re.DOTALL)
_TRAVERSAL = re.compile(r"(?:^|[\s\"'(=])(?:\.\./|\.\.\\\\|/etc/passwd|~/\.ssh)")
_DATA_URI: Final = re.compile(r"data:text/html", re.IGNORECASE)


class SafetyValidator:
    """What arrived that would act if something else went wrong."""

    kind = "safety"

    def check(self, context: ValidationContext) -> Sequence[ValidationOutcome]:
        """Run the safety family."""
        text = context.text
        return (
            self._flag("no_script_tags", _SCRIPT, text, blocking=False, what="a <script> tag"),
            self._flag(
                "no_event_handlers",
                _EVENT_HANDLER,
                text,
                blocking=False,
                what="an HTML event handler",
            ),
            self._flag(
                "no_javascript_urls",
                _JAVASCRIPT_URL,
                text,
                blocking=False,
                what="a javascript: URL",
            ),
            self._flag(
                "no_html_data_uri", _DATA_URI, text, blocking=False, what="a text/html data: URI"
            ),
            self._flag(
                "no_template_syntax",
                _TEMPLATE_SYNTAX,
                text,
                blocking=False,
                what="template syntax",
            ),
            self._flag(
                "no_path_traversal", _TRAVERSAL, text, blocking=True, what="a path traversal"
            ),
        )

    def _flag(
        self,
        key: str,
        pattern: re.Pattern[str],
        text: str,
        *,
        blocking: bool,
        what: str,
    ) -> ValidationOutcome:
        """Report every occurrence of one pattern, with the surrounding text as evidence."""
        found = [match.group(0).strip() for match in pattern.finditer(text)]
        return ValidationOutcome(
            check_kind=self.kind,
            check_key=key,
            passed=not found,
            blocking=blocking,
            detail=(
                f"contains no {what}"
                if not found
                else f"contains {len(found)} instance(s) of {what}; stored and rendered inert"
            ),
            evidence=tuple(item[:80] for item in found[:5]),
        )
