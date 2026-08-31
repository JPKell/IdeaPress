"""ideapress.domain.project — the project value objects and the rules about identity.

Pure domain: no framework, no database, no clock of its own. Slug derivation lives here because it
is the rule that keeps model output and user input away from the filesystem (risks S2, S3): a
project's directory name comes from this function and never from a title, a heading, or anything a
model produced.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

__all__ = [
    "MAX_SLUG_LENGTH",
    "PROJECT_STATUSES",
    "Project",
    "ProjectStatus",
    "SLUG_PATTERN",
    "is_safe_slug",
    "slugify",
]

ProjectStatus = Literal["draft", "planning", "generating", "paused", "complete", "archived"]
PROJECT_STATUSES: Final[frozenset[str]] = frozenset(
    {"draft", "planning", "generating", "paused", "complete", "archived"}
)

MAX_SLUG_LENGTH: Final = 64

SLUG_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$")
"""What a slug may be: lowercase alphanumerics and single hyphens, never leading or trailing.

Deliberately narrower than "safe": it excludes ``.`` and ``..`` by construction, so no slug can
name a parent directory however it was derived, and it excludes the Windows reserved device names
because none of them can match a pattern that forbids a bare alphabetic word of length 3 — see
:func:`is_safe_slug`, which checks that explicitly rather than relying on the reader to notice.
"""

_RESERVED_NAMES: Final[frozenset[str]] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{d}" for d in "123456789"),
        *(f"lpt{d}" for d in "123456789"),
    }
)
"""Windows device names, which cannot be used as a directory name on that platform. IdeaPress is
the component most likely to run on Windows (spec §16), so this is not hypothetical."""


def slugify(title: str, *, fallback: str = "project") -> str:
    """Derive a filesystem-safe slug from a human title.

    Args:
        title: The user's title. May contain anything at all, including path separators, dots,
            control characters, right-to-left overrides and emoji.
        fallback: What to return when the title reduces to nothing — a title of ``"../.."`` or
            ``"🙂"`` has no usable characters and must still produce a valid, safe name.

    Returns:
        A slug matching :data:`SLUG_PATTERN`, at most :data:`MAX_SLUG_LENGTH` characters, never a
        reserved device name, never ``.`` or ``..``, and never containing a path separator. The
        result is not guaranteed unique — the project service resolves collisions.

    Refuses nothing: every input produces a valid slug, because refusing here would mean a user
    could not name a project in their own language. Uniqueness and containment are checked where
    the path is actually built, not here.
    """
    normalized = unicodedata.normalize("NFKD", title)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    truncated = collapsed[:MAX_SLUG_LENGTH].strip("-")
    if not truncated or truncated in _RESERVED_NAMES:
        return fallback
    return truncated


def is_safe_slug(slug: str) -> bool:
    """Whether ``slug`` may be used as a directory name.

    Args:
        slug: A candidate slug, from :func:`slugify` or from a URL path.

    Returns:
        ``True`` only for a value matching :data:`SLUG_PATTERN` that is not a reserved device name.
        Refuses ``.``, ``..``, anything containing a separator or a null byte, anything with
        uppercase or non-ASCII characters, and the empty string — checked here rather than assumed
        from the pattern, because the pattern is the kind of thing that gets loosened by someone
        fixing an unrelated bug.
    """
    if not slug or len(slug) > MAX_SLUG_LENGTH:
        return False
    if slug in _RESERVED_NAMES or slug in {".", ".."}:
        return False
    if "/" in slug or "\\" in slug or "\x00" in slug:
        return False
    return bool(SLUG_PATTERN.match(slug))


@dataclass(frozen=True, slots=True)
class Project:
    """A project, as the rest of the application sees it.

    A frozen value object, not a SQLAlchemy row: models never leave the repository layer, so a
    service hands back one of these and a template renders it without a live session anywhere.
    """

    id: str
    title: str
    slug: str
    content_type: str
    content_type_version: str
    workflow_id: str
    workflow_version: str
    status: ProjectStatus
    brief_text: str
    author_material: dict[str, object]
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    archived_at: datetime | None = None

    @property
    def is_archived(self) -> bool:
        """Whether this project has been archived. Archived projects are hidden, never deleted."""
        return self.status == "archived"
