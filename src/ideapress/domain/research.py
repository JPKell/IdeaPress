"""ideapress.domain.research — what the `research` stage decides, before anything is fetched.

Pure. No `toolyard`, no `httpx`, no filesystem, no clock — the module that says *which* targets a
project has, so the rule can be read and tested without a socket or a project directory.

The rule is deliberately the dullest one available (ADR-0116 decision 4): an **absolute `http(s)://`
URL, appearing verbatim in the brief**. Nothing here completes a bare hostname, resolves a relative
reference, follows a link out of a fetched document, or infers a URL from prose. A stage that
guessed at targets would be choosing its own egress, and the whole point of the host allowlist is
that an operator chose it.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlsplit

__all__ = ["MAX_URL_LENGTH", "brief_urls", "note_title_for"]

MAX_URL_LENGTH: Final = 2048
"""ToolYard's own `url` argument cap. A longer match is dropped here rather than sent to be
refused: the refusal would be correct and the record would say nothing an operator can act on."""

_URL_PATTERN: Final = re.compile(r"https?://[^\s<>\"'\)\]]+", re.IGNORECASE)
"""Whitespace and the characters that end a URL in prose or Markdown. Trailing punctuation is
stripped separately, because a sentence-final full stop is not part of the address and a path
genuinely may end in one."""

_TRAILING_PUNCTUATION: Final = ".,;:!?"


def brief_urls(brief: str) -> tuple[str, ...]:
    """Every absolute http(s) URL written verbatim in a brief, in order, de-duplicated.

    Args:
        brief: The project's brief, as the author wrote it. Untrusted text: it is the user's own
            content, but nothing here treats it as a command.

    Returns:
        The URLs, first occurrence order preserved and later duplicates dropped, so a brief that
        cites one source three times is fetched once. **Refuses**, silently and by omission:
        anything without an `http`/`https` scheme (a bare hostname, a `file:` path, a relative
        reference), anything with no host, and anything longer than :data:`MAX_URL_LENGTH`. Each
        of those would either be refused downstream with a reason an operator cannot act on, or —
        worse — be completed into an address nobody wrote.
    """
    found: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(brief):
        candidate = match.group(0).rstrip(_TRAILING_PUNCTUATION)
        if not candidate or len(candidate) > MAX_URL_LENGTH:
            continue
        parts = urlsplit(candidate)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        found.append(candidate)
    return tuple(found)


def note_title_for(citation: str) -> str:
    """The title a note carries, derived from its citation and never from its content.

    Args:
        citation: The URL, or the file name, the note came from.

    Returns:
        The citation itself, trimmed to what the `sources.title` column holds. Deriving a title
        from the fetched document would mean a note is named by text the source controls, and
        `assemble_context` ranks a note by whether the unit's goal mentions its **title** — so a
        fetched page could promote itself up the context budget by naming itself after the unit.
    """
    return citation.strip()[:300]
