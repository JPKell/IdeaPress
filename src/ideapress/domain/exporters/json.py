"""JSON export: the full structure and provenance, in a shape a script can read.

Determinism here is exact: ``sort_keys=True``, fixed separators, fixed ``ensure_ascii``, and a
trailing newline. Nothing in the payload comes from a clock or the environment.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ideapress.domain.exporters.model import ExportDocument

__all__ = ["build_payload", "render_json"]


def build_payload(document: ExportDocument) -> dict[str, Any]:
    """Build the export payload.

    Returns:
        A plain structure. Every list is already in a defined order — units by ordinal, coverage by
        requirement key — so ``sort_keys`` on the *keys* plus ordered *values* makes the whole
        document byte-stable.
    """
    return {
        "export_format_version": document.format_version,
        "project": {
            "id": document.project_id,
            "title": document.title,
            "slug": document.slug,
            "content_type": document.content_type,
            "content_type_version": document.content_type_version,
            "workflow_id": document.workflow_id,
            "workflow_version": document.workflow_version,
            "brief": document.brief,
            "word_count": document.word_count,
            "unit_count": len(document.units),
            "planned_unit_count": document.planned_units,
            "complete": document.is_complete,
        },
        "incomplete_units": [
            {
                "key": u.key,
                "ordinal": u.ordinal,
                "title": u.title,
                "goal": u.goal,
                "state": u.state,
                "reason": u.reason,
                "requirement_keys": list(u.requirement_keys),
            }
            for u in sorted(document.incomplete_units, key=lambda u: u.ordinal)
        ],
        "units": [
            {
                "key": unit.key,
                "ordinal": unit.ordinal,
                "title": unit.title,
                "goal": unit.goal,
                "content": unit.content,
                "version": unit.version,
                "content_hash": unit.content_hash,
                "word_count": unit.word_count,
                "committed_at": unit.committed_at,
                "coverage": [
                    {
                        "requirement_key": entry.key,
                        "text": entry.text,
                        "blocking": entry.blocking,
                        "satisfied": entry.satisfied,
                        "satisfied_by": entry.satisfied_by,
                        "mechanical": entry.is_mechanical,
                        "detail": entry.detail,
                        "checks": entry.checks,
                        # The grounding evidence (risk T6): the verbatim quote travels into the
                        # export so a reader can weigh the claim against the material.
                        "source": {
                            "document": entry.source_document,
                            "quote": entry.source_quote,
                            "anchor": entry.source_anchor,
                        },
                    }
                    for entry in sorted(unit.coverage, key=lambda entry: entry.key)
                ],
                "provenance": [
                    # `asdict` preserves the tuple as it arrived; the other two exporters sort at
                    # render time and this one must too, or the same project produces two
                    # different JSON files depending on row order.
                    {**asdict(attempt), "degradations": sorted(attempt.degradations)}
                    for attempt in unit.provenance
                ],
                "findings": [dict(sorted(finding.items())) for finding in unit.findings],
                "critiques": [dict(sorted(critique.items())) for critique in unit.critiques],
            }
            for unit in document.units
        ],
        "review_findings": [dict(sorted(f.items())) for f in document.review_findings],
    }


def render_json(document: ExportDocument) -> str:
    """Serialise the export payload.

    Returns:
        Pretty-printed JSON with sorted keys, fixed separators and a trailing newline.
        ``ensure_ascii=False`` is deliberate and fixed: the content is the user's writing, often not
        ASCII, and escaping it would make the file unreadable to the person it belongs to. Fixed
        either way is what byte-stability needs.
    """
    return (
        json.dumps(
            build_payload(document),
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ": "),
        )
        + "\n"
    )
