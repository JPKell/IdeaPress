"""Risk T8: export non-determinism, falsified rather than asserted.

The prompt for this run is specific about where non-determinism hides — unsorted collections, dict
iteration, locale, timezone, newline handling and embedded timestamps — so each is attacked
directly rather than covered by one "export twice" test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from ideapress.domain.exporters.html import render_html
from ideapress.domain.exporters.json import render_json
from ideapress.domain.exporters.markdown import render_markdown
from ideapress.domain.exporters.model import (
    EXPORT_FORMAT_VERSION,
    ExportDocument,
    ExportUnit,
    RequirementCoverage,
    UnitProvenance,
)

if TYPE_CHECKING:
    from collections.abc import Callable

RENDERERS: dict[str, Callable[[ExportDocument], str]] = {
    "markdown": render_markdown,
    "html": render_html,
    "json": render_json,
}


def _document(*, hostile: bool = False) -> ExportDocument:
    content = (
        "Everything happens on your own machine. <script>alert(1)</script> {{ 7*7 }} "
        "and nothing is uploaded."
        if hostile
        else "Everything happens on your own machine, and nothing is uploaded anywhere."
    )
    coverage = (
        RequirementCoverage(
            key="R-002",
            text="Nothing is uploaded.",
            blocking=True,
            satisfied=True,
            satisfied_by="deterministic_check",
            detail="found 'uploaded'",
            checks="contains any of: 'uploaded'",
        ),
        RequirementCoverage(
            key="R-001",
            text="Inference runs on the reader's own machine.",
            blocking=True,
            satisfied=True,
            satisfied_by="audit",
            detail="an audit reported this satisfied",
            checks="no deterministic check — evaluated by audit only",
        ),
    )
    provenance = (
        UnitProvenance(
            stage="draft",
            attempt=1,
            round=0,
            outcome="completed",
            backend="ollama",
            model_canonical_id="ollama/gemma4:12b@sha256:abc",
            prompt_id="stages.draft.write",
            prompt_version="1.0.0",
            prompt_sha256="sha256:def",
            response_hash="sha256:ghi",
            input_tokens=120,
            output_tokens=340,
            provider_ms=1234.5,
            degradations=("model_switch: b", "model_switch: a"),
        ),
    )
    return ExportDocument(
        project_id="01PROJECT",
        title="Local inference for writers",
        slug="local-inference-for-writers",
        content_type="article",
        content_type_version="1.0",
        workflow_id="standard",
        workflow_version="1.0",
        brief="A brief.",
        units=(
            ExportUnit(
                key="U-01",
                ordinal=1,
                title="Where the work happens",
                goal="Say where it runs.",
                content=content,
                version=1,
                content_hash="sha256:unit1",
                word_count=11,
                committed_at="2026-08-31T12:00:00+00:00",
                coverage=coverage,
                provenance=provenance,
            ),
        ),
    )


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_rendering_twice_is_byte_identical(fmt: str) -> None:
    render = RENDERERS[fmt]
    assert render(_document()) == render(_document())


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_no_wall_clock_stamp_appears_anywhere(fmt: str) -> None:
    """The single most common cause of an export that differs from itself."""
    import datetime

    rendered = RENDERERS[fmt](_document())
    today = datetime.datetime.now(tz=datetime.UTC).date().isoformat()
    assert today not in rendered or today == "2026-08-31", (
        "a date that is not in the data appeared in the output"
    )
    assert "generated_at" not in rendered
    assert "Generated" not in rendered


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_collections_are_sorted_not_iterated_in_arrival_order(fmt: str) -> None:
    """The coverage rows arrive as R-002 then R-001 and must render sorted."""
    rendered = RENDERERS[fmt](_document())
    assert rendered.index("R-001") < rendered.index("R-002")


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_degradations_are_sorted_too(fmt: str) -> None:
    rendered = RENDERERS[fmt](_document())
    assert rendered.index("model_switch: a") < rendered.index("model_switch: b")


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_only_unix_line_endings(fmt: str) -> None:
    """Risk P2: a file that differs by line ending between two machines fails on the second."""
    assert "\r" not in RENDERERS[fmt](_document())


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_every_render_ends_with_exactly_one_newline(fmt: str) -> None:
    rendered = RENDERERS[fmt](_document())
    assert rendered.endswith("\n")
    assert not rendered.endswith("\n\n")


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_the_format_version_is_embedded(fmt: str) -> None:
    """Spec §19: a re-export of an old project is checkable against the version that made it."""
    assert EXPORT_FORMAT_VERSION in RENDERERS[fmt](_document())


def test_the_json_export_carries_the_full_structure_and_provenance() -> None:
    payload = json.loads(render_json(_document()))
    assert payload["export_format_version"] == EXPORT_FORMAT_VERSION
    assert payload["project"]["slug"] == "local-inference-for-writers"
    unit = payload["units"][0]
    assert unit["content_hash"] == "sha256:unit1"
    assert [entry["requirement_key"] for entry in unit["coverage"]] == ["R-001", "R-002"]
    assert unit["coverage"][0]["mechanical"] is False, "the audit-decided one is flagged"
    assert unit["coverage"][1]["mechanical"] is True
    attempt = unit["provenance"][0]
    assert attempt["model_canonical_id"] == "ollama/gemma4:12b@sha256:abc"
    assert attempt["prompt_sha256"] == "sha256:def"
    assert attempt["output_tokens"] == 340


def test_the_json_export_keeps_the_users_own_characters_readable() -> None:
    document = _document()
    unit = document.units[0]
    with_unicode = ExportDocument(
        **{
            **{f.name: getattr(document, f.name) for f in document.__dataclass_fields__.values()},
            "units": (
                ExportUnit(
                    **{
                        **{
                            f.name: getattr(unit, f.name)
                            for f in unit.__dataclass_fields__.values()
                        },
                        "content": "Le modèle s'exécute sur votre propre machine — 日本語 too.",
                    }
                ),
            ),
        }
    )
    rendered = render_json(with_unicode)
    assert "modèle" in rendered
    assert "日本語" in rendered
    assert "\\u" not in rendered


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_hostile_model_output_is_inert_in_every_format(fmt: str) -> None:
    """P9's named failure mode is a sanitizer gap in one format and not another."""
    rendered = RENDERERS[fmt](_document(hostile=True))
    if fmt == "html":
        assert "<script>alert(1)</script>" not in rendered
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in rendered
        assert "{{ 7*7 }}" in rendered
        assert ">49<" not in rendered
    else:
        # Markdown and JSON are not executed by a renderer, so the text is kept verbatim — but it
        # must still never be *evaluated*, and JSON must still parse.
        assert "<script>alert(1)</script>" in rendered
        assert "49" not in rendered.replace("2026", "").replace("sha256", "")
    if fmt == "json":
        json.loads(rendered)


def test_the_html_export_references_nothing_external() -> None:
    """It must open from a machine that has never heard of this application."""
    import re

    rendered = render_html(_document())
    fetched = re.findall(r'(?:src|href|url\()\s*=?\s*["\']?([^"\')\s>]+)', rendered)
    external = [url for url in fetched if url.startswith(("http://", "https://", "//"))]
    assert external == [], external
    assert "<link" not in rendered
    assert "<script" not in rendered.replace("&lt;script", "")
    assert "@import" not in rendered
    assert "<style>" in rendered, "the CSS is inline, which is why there is no link"


def test_the_markdown_export_escapes_table_pipes_without_changing_meaning() -> None:
    document = _document()
    unit = document.units[0]
    piped = ExportDocument(
        **{
            **{f.name: getattr(document, f.name) for f in document.__dataclass_fields__.values()},
            "units": (
                ExportUnit(
                    **{
                        **{
                            f.name: getattr(unit, f.name)
                            for f in unit.__dataclass_fields__.values()
                        },
                        "coverage": (
                            RequirementCoverage(
                                key="R-001",
                                text="Use a | pipe in the text",
                                blocking=True,
                                satisfied=True,
                                satisfied_by="deterministic_check",
                                detail="",
                                checks="",
                            ),
                        ),
                    }
                ),
            ),
        }
    )
    rendered = render_markdown(piped)
    assert "a \\| pipe" in rendered


@pytest.mark.parametrize("fmt", sorted(RENDERERS))
def test_identical_under_a_different_locale_timezone_and_hash_seed(fmt: str) -> None:
    """The three environment knobs the prompt names, each varied in a real subprocess.

    Not `monkeypatch`: `PYTHONHASHSEED` is read by the interpreter at startup and cannot be changed
    from inside a running one, so a test that set it in-process would prove nothing.
    """
    # The repository root, not the working directory: the test suite chdirs into a temporary
    # directory, and a subprocess started there cannot import this module.
    root = Path(__file__).resolve().parents[2]
    program = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(root)!r})
        sys.path.insert(0, {str(root / "tests" / "unit")!r})
        from test_exporters import RENDERERS, _document
        sys.stdout.write(RENDERERS[{fmt!r}](_document()))
    """)
    environments = [
        {"PYTHONHASHSEED": "0", "LC_ALL": "C", "TZ": "UTC"},
        {"PYTHONHASHSEED": "1", "LC_ALL": "C", "TZ": "UTC"},
        {"PYTHONHASHSEED": "12345", "LC_ALL": "de_DE.UTF-8", "TZ": "Asia/Tokyo"},
        {"PYTHONHASHSEED": "99999", "LC_ALL": "en_US.UTF-8", "TZ": "America/Sao_Paulo"},
    ]
    outputs: list[str] = []
    for extra in environments:
        environment = {**os.environ, **extra}
        result = subprocess.run(  # noqa: S603 — fixed argv, no shell
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            env=environment,
            cwd=root,
        )
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1, (
        f"{fmt} rendered differently across locale, timezone and hash seed"
    )
    assert outputs[0] == RENDERERS[fmt](_document())
