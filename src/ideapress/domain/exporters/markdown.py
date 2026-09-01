"""Markdown export: the document, and an appendix a person can audit."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ideapress.domain.exporters.model import ExportDocument, RequirementCoverage

__all__ = ["render_markdown"]


def render_markdown(document: ExportDocument) -> str:
    """Render a committed project as Markdown.

    Args:
        document: The committed project.

    Returns:
        The document, then a provenance appendix. Deterministic: ``\\n`` line endings only, no
        wall-clock stamp, every collection sorted before it is iterated.
    """
    lines: list[str] = [f"# {document.title}", ""]
    for unit in document.units:
        lines.append(f"## {unit.title}")
        lines.append("")
        lines.append(unit.content.strip())
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Provenance")
    lines.append("")
    lines.append(f"- Export format version: {document.format_version}")
    lines.append(f"- Content type: {document.content_type} {document.content_type_version}")
    lines.append(f"- Workflow: {document.workflow_id} {document.workflow_version}")
    lines.append(f"- Units: {len(document.units)}")
    lines.append(f"- Words: {document.word_count}")
    lines.append("")

    lines.append("### Requirement coverage")
    lines.append("")
    rows = list(document.coverage_rows())
    if not rows:
        lines.append("No requirements were recorded for this project.")
        lines.append("")
    else:
        lines.append("| Requirement | Class | Satisfied | Decided by | Checked by | Grounded in |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for row in rows:
            lines.append(
                f"| {row.key} — {_cell(row.text)} "
                f"| {'blocking' if row.blocking else 'advisory'} "
                f"| {'yes' if row.satisfied else 'no'} "
                f"| {row.satisfied_by} "
                f"| {_cell(row.checks)} "
                f"| {_cell(_grounding(row))} |"
            )
        lines.append("")
        lines.append(
            "A requirement decided by `audit` was not settled by a deterministic check: the "
            "guarantee there is model-assisted, and this table says so rather than implying "
            "otherwise. The *grounded in* column is the verbatim span of the author material the "
            "requirement was compiled from — the claim and its evidence side by side, so a "
            "requirement the material does not support is visible as exactly that."
        )
        lines.append("")

    lines.append("### Units")
    lines.append("")
    for unit in document.units:
        lines.append(f"#### {unit.key} — {unit.title}")
        lines.append("")
        lines.append(f"- Version: {unit.version}")
        lines.append(f"- Committed: {unit.committed_at}")
        lines.append(f"- Content hash: {unit.content_hash}")
        lines.append(f"- Words: {unit.word_count}")
        for attempt in unit.provenance:
            lines.append(
                f"- {attempt.stage} attempt {attempt.attempt} (round {attempt.round}): "
                f"{attempt.outcome} via {attempt.backend}, "
                f"model {attempt.model_canonical_id or 'not disclosed'}, "
                f"prompt {attempt.prompt_id or '—'} {attempt.prompt_version or ''}"
            )
            if attempt.degradations:
                for degradation in sorted(attempt.degradations):
                    lines.append(f"  - degradation: {degradation}")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


def _cell(text: str) -> str:
    """Make text safe for a Markdown table cell without changing what it says."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _grounding(row: RequirementCoverage) -> str:
    """The requirement's grounding evidence, as ``document: “verbatim quote”``."""
    if not row.source_quote:
        return "—"
    return f"{row.source_label}: “{row.source_quote}”"
