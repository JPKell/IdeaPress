"""HTML export: self-contained, offline, and inert.

Three properties, each of which is a requirement rather than a nicety:

* **Self-contained.** The CSS is inline. There is no ``<link>``, no ``<script src>``, no image and
  no font from anywhere — so the file opens with no network at all (P6's test opens it in an
  unshared network namespace and asserts no request is made).
* **Inert.** Every piece of model output is escaped here, by this module, with no template engine
  in the path. Risk S1's failure mode is a sanitizer gap that exists in one format and not another,
  so this file escapes rather than trusting that something upstream did.
* **Deterministic.** No wall-clock stamp, no unsorted iteration, ``\\n`` endings.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ideapress.domain.exporters.model import ExportDocument, ExportUnit

__all__ = ["render_html"]

# Inline, deliberately. A stylesheet link is a network request, and this file must open from a USB
# stick on a machine that has never heard of this application.
_STYLE = """
:root { color-scheme: light dark; --fg: #1a1a1a; --bg: #fdfdfc; --muted: #5a5a56;
        --rule: #d8d8d4; --accent: #24506e; }
@media (prefers-color-scheme: dark) {
  :root { --fg: #e8e8e4; --bg: #16181a; --muted: #a0a09a; --rule: #33363a; --accent: #8fbcdb; }
}
* { box-sizing: border-box; }
body { margin: 0; padding: 2rem 1rem 4rem; background: var(--bg); color: var(--fg);
       font: 16px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue",
       Arial, sans-serif; }
main, header, footer { max-width: 44rem; margin: 0 auto; }
h1 { font-size: 2rem; line-height: 1.2; margin: 0 0 0.25rem; }
h2 { font-size: 1.4rem; margin: 2.5rem 0 0.75rem; padding-top: 1.25rem;
     border-top: 1px solid var(--rule); }
h3 { font-size: 1.1rem; margin: 2rem 0 0.5rem; }
h4 { font-size: 1rem; margin: 1.5rem 0 0.25rem; }
p { margin: 0 0 1rem; }
.subtitle { color: var(--muted); margin: 0 0 2rem; }
.unit-content { white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; font-size: 0.9rem; margin: 0 0 1rem; }
th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid var(--rule);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.table-scroll { overflow-x: auto; }
code, .hash { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.85em;
              color: var(--muted); word-break: break-all; }
.note { color: var(--muted); font-size: 0.9rem; }
.yes { color: var(--accent); }
ul { margin: 0 0 1rem; padding-left: 1.25rem; }
"""


def _e(value: object) -> str:
    """Escape anything for HTML text, including quotes.

    Every model-produced string in this file goes through here. Not "should" — the exporter is the
    last place before bytes reach a browser, and P9's named failure mode is exactly a format that
    escapes where another does not.
    """
    return escape(str(value), quote=True)


def _unit_section(unit: ExportUnit) -> list[str]:
    return [
        f"<h2>{_e(unit.title)}</h2>",
        f'<div class="unit-content">{_e(unit.content.strip())}</div>',
    ]


def _coverage_table(document: ExportDocument) -> list[str]:
    rows = list(document.coverage_rows())
    if not rows:
        return ["<p>No requirements were recorded for this project.</p>"]
    out = [
        '<div class="table-scroll"><table>',
        "<thead><tr><th>Requirement</th><th>Class</th><th>Satisfied</th>"
        "<th>Decided by</th><th>Checked by</th></tr></thead><tbody>",
    ]
    for row in rows:
        out.append(
            "<tr>"
            f"<td>{_e(row.key)} — {_e(row.text)}</td>"
            f"<td>{'blocking' if row.blocking else 'advisory'}</td>"
            f'<td class="{"yes" if row.satisfied else ""}">{"yes" if row.satisfied else "no"}</td>'
            f"<td>{_e(row.satisfied_by)}</td>"
            f"<td>{_e(row.checks)}</td>"
            "</tr>"
        )
    out.append("</tbody></table></div>")
    out.append(
        '<p class="note">A requirement decided by <code>audit</code> was not settled by a '
        "deterministic check: the guarantee there is model-assisted, and this table says so "
        "rather than implying otherwise.</p>"
    )
    return out


def render_html(document: ExportDocument) -> str:
    """Render a committed project as a single, self-contained HTML file.

    Args:
        document: The committed project.

    Returns:
        One file. No external reference of any kind, so it opens with no network; every piece of
        model output escaped here; and byte-identical for the same committed project.
    """
    parts: list[str] = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_e(document.title)}</title>",
        f"<style>{_STYLE}</style>",
        "</head>",
        "<body>",
        "<header>",
        f"<h1>{_e(document.title)}</h1>",
        f'<p class="subtitle">{len(document.units)} units, {document.word_count} words</p>',
        "</header>",
        "<main>",
    ]
    for unit in document.units:
        parts.extend(_unit_section(unit))

    parts.extend(
        [
            "</main>",
            "<footer>",
            "<h2>Provenance</h2>",
            "<ul>",
            f"<li>Export format version: {_e(document.format_version)}</li>",
            f"<li>Content type: {_e(document.content_type)} "
            f"{_e(document.content_type_version)}</li>",
            f"<li>Workflow: {_e(document.workflow_id)} {_e(document.workflow_version)}</li>",
            "</ul>",
            "<h3>Requirement coverage</h3>",
        ]
    )
    parts.extend(_coverage_table(document))
    parts.append("<h3>Units</h3>")
    for unit in document.units:
        parts.append(f"<h4>{_e(unit.key)} — {_e(unit.title)}</h4>")
        parts.append("<ul>")
        parts.append(f"<li>Version {unit.version}, committed {_e(unit.committed_at)}</li>")
        parts.append(f'<li class="hash">{_e(unit.content_hash)}</li>')
        parts.append(f"<li>{unit.word_count} words</li>")
        for attempt in unit.provenance:
            parts.append(
                f"<li>{_e(attempt.stage)} attempt {attempt.attempt} (round {attempt.round}): "
                f"{_e(attempt.outcome)} via {_e(attempt.backend)}, model "
                f"{_e(attempt.model_canonical_id or 'not disclosed')}, prompt "
                f"{_e(attempt.prompt_id or '—')} {_e(attempt.prompt_version or '')}</li>"
            )
            for degradation in sorted(attempt.degradations):
                parts.append(f"<li>degradation: {_e(degradation)}</li>")
        parts.append("</ul>")
    parts.extend(["</footer>", "</body>", "</html>"])
    return "\n".join(parts) + "\n"
