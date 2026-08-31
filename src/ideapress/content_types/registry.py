"""ideapress.content_types.registry — the open content-type registry.

Workflows §10: a content type supplies the unit taxonomy, the validators specific to its structure,
its default workflow and its export templates, and **the engine knows only units and requirements**
— never chapters, sections or quests. Risk G2 is content-type vocabulary leaking into the engine,
and the shape here is the mitigation: a content type is data the engine reads, never a branch the
engine takes.

Shipped at 1.0: ``article`` and ``report``. The registry is open — an entry-point group is read at
import, so a third-party package can add one without this module changing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CONTENT_TYPES",
    "ENTRY_POINT_GROUP",
    "ContentType",
    "discover",
    "get_content_type",
]

ENTRY_POINT_GROUP: Final = "ideapress.content_types"
"""Where a third-party content type registers itself."""


@dataclass(frozen=True, slots=True)
class ContentType:
    """One content type, as data the engine reads.

    Attributes:
        name: ``article``, ``report``, …
        version: This content type's own version, recorded on every project that uses it.
        description: One line, for the UI.
        default_workflow: Which workflow a new project of this type starts with.
        min_units, max_units: The band the outline stage plans within.
        target_words_per_unit: What a unit aims for when the plan states nothing.
        structural_expectations: Extra structural rules, as data — never code the engine runs.
        fact_check_by_default: Whether `fact_check` is on. Workflows §2 says it is "on for
            research-backed content types", and this is where that is decided.
    """

    name: str
    version: str
    description: str
    default_workflow: str = "standard"
    min_units: int = 2
    max_units: int = 12
    target_words_per_unit: int = 400
    structural_expectations: Mapping[str, str] = field(default_factory=dict)
    fact_check_by_default: bool = False


ARTICLE: Final = ContentType(
    name="article",
    version="1.0",
    description="A piece of prose with a point, read start to finish.",
    min_units=3,
    max_units=8,
    target_words_per_unit=400,
    structural_expectations={
        "opening": "The first unit states what the piece is about without preamble.",
        "closing": "The last unit ends; it does not summarise what the reader just read.",
    },
)

REPORT: Final = ContentType(
    name="report",
    version="1.0",
    description="A structured account of findings, read by someone who will act on it.",
    min_units=4,
    max_units=12,
    target_words_per_unit=500,
    structural_expectations={
        "findings": "Each unit states a finding and the evidence for it, in that order.",
        "evidence": "A claim without evidence is a claim the reader cannot act on.",
    },
    # A report is the research-backed type of the two, so fact checking is on by default here and
    # off for an article (workflows §2, stage 10).
    fact_check_by_default=True,
)

CONTENT_TYPES: Final[dict[str, ContentType]] = {ARTICLE.name: ARTICLE, REPORT.name: REPORT}
"""The two shipped at 1.0. :func:`discover` adds anything an installed package registered."""


def discover() -> dict[str, ContentType]:
    """Return every content type, including any a third-party package registered.

    Returns:
        The shipped types plus discovered ones. A discovered type that is not a
        :class:`ContentType` is ignored with a warning rather than crashing the application: a
        broken plugin must not stop a user opening their own projects.
    """
    import logging
    from importlib.metadata import entry_points

    found = dict(CONTENT_TYPES)
    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            candidate = entry.load()
        except Exception as exc:  # noqa: BLE001 — a broken plugin must not break the application
            logging.getLogger(__name__).warning(
                "content_type.load_failed", extra={"entry_point": entry.name, "detail": str(exc)}
            )
            continue
        if isinstance(candidate, ContentType):
            found[candidate.name] = candidate
        else:
            logging.getLogger(__name__).warning(
                "content_type.not_a_content_type", extra={"entry_point": entry.name}
            )
    return found


def get_content_type(name: str) -> ContentType:
    """Return one content type by name.

    Raises:
        ValidationError: No such content type. Names the ones that exist, because a typo in a
            project's content type is otherwise a silent fallback to whatever the default is.
    """
    from baseaicore import ValidationError

    available = discover()
    if name not in available:
        message = f"{name!r} is not a content type. Available: {', '.join(sorted(available))}."
        raise ValidationError(
            message, details={"content_type": name, "available": sorted(available)}
        )
    return available[name]
