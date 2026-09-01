"""ideapress.services.diff — what changed between two versions of a unit.

A revision loop that a person cannot inspect is a loop they have to trust, and the whole product is
built on not having to. This renders the difference between two committed versions as structured
rows the template escapes — never as HTML, and never with a "safe" marker anywhere near it, because
both sides of a diff are model output (risk S1).

Line-based rather than word-based on purpose. `difflib`'s word-level output on a 5 000-word unit is
a wall of interleaved fragments that reads worse than the two texts side by side, and the question
a person actually asks between revisions is "which paragraphs changed?".
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.services.runtime import Runtime

__all__ = ["DiffLine", "DiffSummary", "diff_lines", "unit_diff"]

DiffKind = Literal["equal", "added", "removed"]

_CONTEXT_LINES = 3


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line of a rendered diff.

    Attributes:
        kind: ``equal``, ``added`` or ``removed``.
        text: The line, exactly as it appears in whichever version it came from. Never escaped
            here — escaping is the template's job, and doing it twice would show a reader
            ``&amp;lt;`` where the model wrote ``<``.
        old_number: Its 1-based line number in the earlier version, when it has one.
        new_number: Its 1-based line number in the later version, when it has one.
    """

    kind: DiffKind
    text: str
    old_number: int | None = None
    new_number: int | None = None

    @property
    def marker(self) -> str:
        """A character that carries the same information as the colour does.

        UI/UX Standards §13: colour is never the sole indicator of state. A reader who cannot
        distinguish the two backgrounds still sees ``+`` and ``-``.
        """
        return {"added": "+", "removed": "-", "equal": " "}[self.kind]


@dataclass(frozen=True, slots=True)
class DiffSummary:
    """A diff and its counts.

    Attributes:
        lines: The rendered rows, in reading order.
        added: How many lines only the later version has.
        removed: How many lines only the earlier one has.
        old_version: The earlier version number.
        new_version: The later version number.
        truncated: Whether unchanged runs were elided.
    """

    lines: tuple[DiffLine, ...]
    added: int
    removed: int
    old_version: int
    new_version: int
    truncated: bool = False

    @property
    def unchanged(self) -> bool:
        """Whether the two versions are identical — a real outcome, not an empty result.

        A revision that changed nothing is exactly what a "leave it alone" critique produces, and
        showing an empty diff without saying so reads as a rendering failure.
        """
        return self.added == 0 and self.removed == 0


def diff_lines(
    earlier: str, later: str, *, context_lines: int = _CONTEXT_LINES
) -> tuple[tuple[DiffLine, ...], bool]:
    """Diff two texts line by line, keeping ``context_lines`` of unchanged text around each change.

    Args:
        earlier: The earlier version's text.
        later: The later version's text.
        context_lines: How many unchanged lines to keep either side of a change. ``0`` keeps none;
            a very large number keeps the whole text.

    Returns:
        The rows, and whether anything was elided.

    Unicode and long lines are carried through untouched: the diff is computed over Python strings
    and the template wraps them with CSS, so a 900-character line with an emoji in it renders as
    itself rather than being truncated or re-encoded here (P8's named failure mode).
    """
    old_lines = earlier.splitlines()
    new_lines = later.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)

    rows: list[DiffLine] = []
    truncated = False
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            span = i2 - i1
            if span > context_lines * 2 + 1:
                truncated = True
                head = range(i1, i1 + context_lines)
                tail = range(i2 - context_lines, i2)
                keep: Sequence[int] = [*head, *tail]
            else:
                keep = range(i1, i2)
            for index in keep:
                rows.append(
                    DiffLine(
                        kind="equal",
                        text=old_lines[index],
                        old_number=index + 1,
                        new_number=j1 + (index - i1) + 1,
                    )
                )
            continue
        for index in range(i1, i2):
            rows.append(DiffLine(kind="removed", text=old_lines[index], old_number=index + 1))
        for index in range(j1, j2):
            rows.append(DiffLine(kind="added", text=new_lines[index], new_number=index + 1))
    return tuple(rows), truncated


def unit_diff(
    runtime: Runtime,
    *,
    project_id: str,
    unit_key: str,
    old_version: int | None = None,
    new_version: int | None = None,
) -> DiffSummary:
    """Diff two committed versions of one unit.

    Args:
        runtime: The process's handles.
        project_id: The project the unit belongs to — filtered on, never assumed.
        unit_key: The unit.
        old_version: The earlier version; defaults to the one before ``new_version``.
        new_version: The later version; defaults to the newest.

    Returns:
        The diff and its counts.

    Raises:
        UnitNotFound: No such unit in this project.
        ValidationError: A named version does not exist, or the unit has fewer than two versions
            so there is nothing to compare. Naming which is the point: "no diff" and "only one
            version exists" are different answers to the same click.
    """
    from baseaicore import ValidationError

    versions = _versions(runtime, project_id=project_id, unit_key=unit_key)
    if len(versions) < 2:
        message = (
            f"{unit_key} has {len(versions)} version(s), so there is nothing to compare. A diff "
            "needs two committed versions; run a revision first."
        )
        raise ValidationError(
            message, details={"unit_key": unit_key, "version_count": len(versions)}
        )

    ordered = sorted(versions)
    resolved_new = new_version if new_version is not None else ordered[-1]
    if resolved_new not in versions:
        raise _no_such_version(unit_key, resolved_new, ordered)
    earlier_candidates = [number for number in ordered if number < resolved_new]
    resolved_old = (
        old_version
        if old_version is not None
        else (earlier_candidates[-1] if earlier_candidates else ordered[0])
    )
    if resolved_old not in versions:
        raise _no_such_version(unit_key, resolved_old, ordered)
    if resolved_old > resolved_new:
        resolved_old, resolved_new = resolved_new, resolved_old

    rows, truncated = diff_lines(versions[resolved_old], versions[resolved_new])
    return DiffSummary(
        lines=rows,
        added=sum(1 for row in rows if row.kind == "added"),
        removed=sum(1 for row in rows if row.kind == "removed"),
        old_version=resolved_old,
        new_version=resolved_new,
        truncated=truncated,
    )


def _versions(runtime: Runtime, *, project_id: str, unit_key: str) -> dict[int, str]:
    """Every stored version of one unit, as ``{version: text}``.

    Args:
        runtime: The process's handles.
        project_id: The owning project, joined on rather than assumed — a unit key is unique
            within a project, not across the database.
        unit_key: The unit.

    Returns:
        The versions and their text. Read here rather than through `unit_history`, which carries
        coverage and hashes but deliberately not the content: a history listing that loaded every
        version's full text would be several megabytes for a long project, and it is rendered on
        every unit page.

    Raises:
        UnitNotFound: No such unit in this project.
    """
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import UnitVersion as UnitVersionRow
    from ideapress.services.units import load_unit

    with runtime.storage.read() as session:
        unit = load_unit(session, project_id, unit_key)
        rows = session.execute(
            select(UnitVersionRow.version, UnitVersionRow.content_text)
            .where(UnitVersionRow.unit_id == unit.id)
            .order_by(UnitVersionRow.version)
        ).all()
    return {int(version): str(text) for version, text in rows}


def _no_such_version(unit_key: str, version: int, available: Sequence[int]) -> Exception:
    """The refusal for a version that does not exist, naming the ones that do."""
    from baseaicore import ValidationError

    message = (
        f"{unit_key} has no version {version}. Its versions are: "
        f"{', '.join(str(number) for number in available)}."
    )
    return ValidationError(
        message, details={"unit_key": unit_key, "version": version, "available": list(available)}
    )


def diff_context(summary: DiffSummary) -> dict[str, Any]:
    """The diff as template context.

    Args:
        summary: What :func:`unit_diff` produced.

    Returns:
        Plain data — no markup, no pre-escaped strings — so the template escapes it exactly once.
    """
    return {
        "diff_lines": [
            {
                "kind": line.kind,
                "marker": line.marker,
                "text": line.text,
                "old_number": line.old_number,
                "new_number": line.new_number,
            }
            for line in summary.lines
        ],
        "diff_added": summary.added,
        "diff_removed": summary.removed,
        "diff_old_version": summary.old_version,
        "diff_new_version": summary.new_version,
        "diff_unchanged": summary.unchanged,
        "diff_truncated": summary.truncated,
    }
