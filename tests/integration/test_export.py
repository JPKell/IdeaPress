"""Exports over the real service: written to disk, byte-stable, and openable with no network."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ideapress.config import Settings, load_settings
from ideapress.errors import ExportFailed
from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
from ideapress.services.runtime import Runtime, build_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator

BRIEF = (
    "The article must state that inference runs entirely on the reader's own machine and that no "
    "document content is uploaded anywhere."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must be explicit about where inference happens.",
            "blocking": True,
            "source_document": "brief",
            "source_quote": "inference runs entirely on the reader's own machine",
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        }
    ]
}
PLAN = {
    "units": [
        {
            "title": "Where the work happens",
            "goal_text": "Say plainly where inference runs.",
            "requirement_keys": ["R-001"],
            "target_words": 50,
        },
        {
            "title": "What it costs you",
            "goal_text": "Be honest about the trade.",
            "requirement_keys": ["R-001"],
            "target_words": 50,
        },
    ]
}
DRAFT = (
    "Everything happens on your own machine. The model reads what you wrote and answers there, "
    "with nothing uploaded and no account needed. The hardware is yours to provide, which is the "
    "trade you make for keeping the work where you made it."
)
CLEAN_REVIEW = (
    json.dumps({"findings": []}),
    json.dumps({"verdict": "acceptable", "rationale": "ok"}),
)


def _script(*answers: Any) -> FakeBackend:
    from modelrack.testing import FakeGeneration, FakeScript

    return FakeBackend(
        script=FakeScript(
            models=default_fake_script().models,
            capabilities=default_fake_script().capabilities,
            generations=tuple(
                FakeGeneration(text=a if isinstance(a, str) else json.dumps(a)) for a in answers
            ),
            repeat_final_generation=True,
        ),
        seed=5,
    )


@pytest.fixture
def settings() -> Settings:
    return load_settings().settings


@pytest.fixture
def runtime(settings: Settings) -> Iterator[Runtime]:
    built = build_runtime(settings)
    yield built
    built.close()


def _with(runtime: Runtime, backend: FakeBackend) -> Runtime:
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.stages import StageRunner

    gateway = InferenceGateway(
        backend=backend,
        bindings=runtime.settings.models.stages,
        execution=runtime.settings.execution,
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(  # noqa: SLF001
        runtime.storage, gateway=gateway, sink=runtime.events
    )
    return runtime


def _wait(runtime: Runtime, task_id: str, *, timeout: float = 30.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.runner.is_finished(task_id):
            return runtime.runner.run_state(task_id) or "unknown"
        time.sleep(0.02)
    message = "the stage did not finish"
    raise AssertionError(message)


@pytest.fixture
def committed_project(runtime: Runtime) -> str:
    """A project with two committed units, ready to export."""
    from ideapress.services.stage_bodies import start_plan, start_stage

    _with(runtime, _script(REQUIREMENTS, PLAN))
    project_id = runtime.projects.create(title="Local inference for writers", brief=BRIEF).id
    plan = start_plan(runtime, project_id=project_id)
    assert _wait(runtime, plan.run_id) == "completed"

    # One full cycle per unit: draft, a clean audit, an accepting critique. `repeat_final` would
    # otherwise feed the second unit the first unit's critique JSON as its draft.
    _with(runtime, _script(DRAFT, *CLEAN_REVIEW, DRAFT, *CLEAN_REVIEW))
    draft = start_stage(runtime, project_id=project_id, stage="draft")
    assert _wait(runtime, draft.run_id) == "completed"
    return project_id


@pytest.mark.parametrize("fmt", ["markdown", "html", "json"])
def test_exporting_twice_produces_identical_bytes(
    runtime: Runtime, committed_project: str, fmt: str
) -> None:
    """Spec §11 contract 4, over the real service and the real filesystem."""
    from ideapress.services.export import export_project

    first = export_project(runtime, project_id=committed_project, fmt=fmt)
    first_bytes = Path(str(first["path"])).read_bytes()
    second = export_project(runtime, project_id=committed_project, fmt=fmt)
    second_bytes = Path(str(second["path"])).read_bytes()

    assert first_bytes == second_bytes
    assert first["sha256"] == second["sha256"]
    assert hashlib.sha256(first_bytes).hexdigest() == hashlib.sha256(second_bytes).hexdigest()


@pytest.mark.parametrize("fmt", ["markdown", "html", "json"])
def test_the_export_record_names_what_it_covered(
    runtime: Runtime, committed_project: str, fmt: str
) -> None:
    from sqlalchemy import select

    from ideapress.infrastructure.db.models import Export as ExportRow
    from ideapress.services.export import export_project

    written = export_project(runtime, project_id=committed_project, fmt=fmt)
    with runtime.storage.read() as session:
        row = session.scalars(select(ExportRow).where(ExportRow.format == fmt)).one()
    assert row.sha256 == written["sha256"]
    assert row.export_format_version == "1.0"
    assert len(row.unit_version_ids_json) == 2
    assert all(h.startswith("sha256:") for h in row.unit_version_ids_json)


def test_the_export_path_comes_from_the_slug_not_the_title(
    runtime: Runtime, committed_project: str
) -> None:
    """Risk S2: the path is built from an identifier IdeaPress generated and validated."""
    from ideapress.services.export import export_project

    written = export_project(runtime, project_id=committed_project, fmt="markdown")
    assert Path(str(written["path"])).name == "local-inference-for-writers.md"


def test_a_project_with_nothing_committed_refuses_to_export(runtime: Runtime) -> None:
    """An export is of work that passed its gates; a paused draft is deliberately not in one."""
    from ideapress.services.export import export_project

    _with(runtime, _script(REQUIREMENTS, PLAN))
    project_id = runtime.projects.create(title="Nothing yet", brief=BRIEF).id
    with pytest.raises(ExportFailed) as caught:
        export_project(runtime, project_id=project_id, fmt="markdown")
    assert "no committed units" in caught.value.message


def test_an_unknown_format_is_refused_by_name(runtime: Runtime, committed_project: str) -> None:
    from ideapress.services.export import export_project

    with pytest.raises(ExportFailed) as caught:
        export_project(runtime, project_id=committed_project, fmt="pdf")
    assert "pdf" in caught.value.message
    assert "markdown" in caught.value.message


def test_the_exported_document_contains_every_committed_unit(
    runtime: Runtime, committed_project: str
) -> None:
    from ideapress.services.export import build_document, render

    document = build_document(runtime, project_id=committed_project)
    assert [unit.key for unit in document.units] == ["U-01", "U-02"]
    for fmt in ("markdown", "html", "json"):
        rendered = render(document, fmt)
        assert "Where the work happens" in rendered
        assert "What it costs you" in rendered
        assert "own machine" in rendered


def test_the_html_export_opens_with_no_network_at_all(
    runtime: Runtime, committed_project: str
) -> None:
    """The offline proof, run inside an unshared network namespace.

    What this establishes: a process with **no network interfaces at all** reads the file, parses
    it, finds every unit's content present, and finds no reference to any URL it could fetch. It
    does not drive a browser — there is none here — so what it cannot prove is that a browser would
    make no request for some reason other than a reference in the markup. The complementary
    evidence is the markup assertion itself: no `<link>`, no `<script src>`, no `@import`, no
    absolute URL anywhere.
    """
    from ideapress.services.export import export_project

    written = export_project(runtime, project_id=committed_project, fmt="html")
    path = Path(str(written["path"]))

    program = textwrap.dedent(f"""
        import re, socket, sys
        text = open({str(path)!r}, encoding="utf-8").read()

        # Any attempt to open a socket in here is a failure, not a slow test.
        def refuse(*args, **kwargs):
            raise SystemExit("the export tried to reach the network")
        socket.socket.connect = refuse
        socket.create_connection = refuse

        problems = []
        for url in re.findall(r'(?:src|href)\\s*=\\s*["\\']([^"\\']+)', text):
            if url.startswith(("http://", "https://", "//", "ftp:")):
                problems.append(url)
        if "@import" in text:
            problems.append("@import")
        if "<link" in text:
            problems.append("<link")
        for needed in ("Where the work happens", "What it costs you", "own machine"):
            if needed not in text:
                problems.append("missing: " + needed)
        sys.stdout.write("OK" if not problems else "PROBLEMS: " + "; ".join(problems))
    """)
    result = subprocess.run(  # noqa: S603 — fixed argv, no shell
        ["unshare", "-rn", sys.executable, "-c", program],  # noqa: S607 — on PATH by design
        capture_output=True,
        text=True,
        check=False,
        # The repository root, always: pytest-cov starts coverage in a subprocess through a `.pth`
        # hook that reads `pyproject.toml` relative to the working directory, and from anywhere
        # else it measures without `branch = true` and writes a data file the parent's cannot
        # combine with — aborting the whole coverage run rather than failing a test (M7-9).
        cwd=Path(__file__).resolve().parents[2],
    )
    if result.returncode != 0 and "unshare" in result.stderr.lower():
        pytest.skip(f"unshare is unavailable here: {result.stderr.strip()[:120]}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "OK", result.stdout


def test_the_html_export_is_a_single_file_with_no_companions(
    runtime: Runtime, committed_project: str
) -> None:
    """Self-contained means one file: no sidecar CSS, no assets directory."""
    from ideapress.services.export import export_project

    written = export_project(runtime, project_id=committed_project, fmt="html")
    directory = Path(str(written["path"])).parent
    assert sorted(p.name for p in directory.iterdir()) == ["local-inference-for-writers.html"]


def test_the_exports_agree_with_each_other(runtime: Runtime, committed_project: str) -> None:
    """Risk G4: three serializers, one rendering model, no drift between them."""
    from ideapress.services.export import build_document, render

    document = build_document(runtime, project_id=committed_project)
    payload = json.loads(render(document, "json"))
    markdown = render(document, "markdown")
    html = render(document, "html")

    for unit in payload["units"]:
        assert unit["title"] in markdown
        assert unit["title"] in html
        assert unit["content_hash"] in markdown
        assert unit["content_hash"] in html
    assert str(payload["project"]["word_count"]) in html


def test_export_over_http(runtime: Runtime, committed_project: str) -> None:
    from fastapi.testclient import TestClient

    from ideapress.web.app import create_app

    app = create_app(runtime.settings, runtime_builder=lambda settings: runtime)
    with TestClient(app, base_url="http://127.0.0.1:8767") as client:
        rendered = client.get(f"/api/v1/projects/{committed_project}/export?format=markdown")
        assert rendered.status_code == 200
        assert "Where the work happens" in rendered.text

        written = client.post(f"/api/v1/projects/{committed_project}/export?format=json").json()
        assert written["sha256"].startswith("sha256:")
        assert written["export_format_version"] == "1.0"

        formats = client.get("/api/v1/export/formats").json()["formats"]
        assert [f["format"] for f in formats] == ["html", "json", "markdown"]

        bad = client.get(f"/api/v1/projects/{committed_project}/export?format=pdf")
        assert bad.status_code == 500
        assert bad.json()["error"]["code"] == "EXPORT_FAILED"


def test_a_regex_free_check_that_no_absolute_url_appears(
    runtime: Runtime, committed_project: str
) -> None:
    from ideapress.services.export import build_document, render

    html = render(build_document(runtime, project_id=committed_project), "html")
    assert not re.search(r"https?://", html)


@pytest.mark.parametrize("fmt", ["markdown", "html", "json"])
def test_the_recorded_hash_is_one_sha256sum_reproduces(
    runtime: Runtime, committed_project: str, fmt: str
) -> None:
    """A hash a reader cannot reproduce is a hash they cannot use.

    The first version recorded BaseAiCore's `sha256_of`, which hashes a value's *canonical JSON* —
    right for a structure, wrong for a file. `sha256sum` on the export disagreed with the number
    the export record and the CLI both printed, so the one check anybody would actually run to
    verify determinism said the file was wrong.
    """
    from ideapress.services.export import export_project

    written = export_project(runtime, project_id=committed_project, fmt=fmt)
    on_disk = hashlib.sha256(Path(str(written["path"])).read_bytes()).hexdigest()
    assert written["sha256"] == f"sha256:{on_disk}"
    assert written["size_bytes"] == Path(str(written["path"])).stat().st_size


@pytest.mark.parametrize("fmt", ["markdown", "html", "json"])
def test_the_grounding_quote_reaches_the_exported_file(
    runtime: Runtime, committed_project: str, fmt: str
) -> None:
    """M7 finding 2, over the real service: the verbatim quote survives the whole pipeline.

    The compiled requirement's quote is risk T6's fabrication-detection evidence, and
    `ground_requirement` promises it travels into every export. This walks the real path —
    compilation, storage, commit, `build_document`, renderer, file on disk — and reads the quote
    back out of the artefact.
    """
    from ideapress.services.export import export_project

    quote = "inference runs entirely on the reader"
    written = export_project(runtime, project_id=committed_project, fmt=fmt)
    rendered = Path(str(written["path"])).read_text(encoding="utf-8")
    assert quote in rendered, "the verbatim grounding quote must appear in the exported artefact"
    if fmt == "json":
        entries = [
            entry
            for unit in json.loads(rendered)["units"]
            for entry in unit["coverage"]
            if entry["requirement_key"] == "R-001"
        ]
        assert entries, "every committed unit carries the requirement"
        for entry in entries:
            assert entry["source"]["document"] == "brief"
            assert entry["source"]["quote"] == (
                "inference runs entirely on the reader's own machine"
            )
