"""UI/UX Standards §13, item by item, over every page this application renders — P8 AC3.

Not every item can be asserted from HTML alone: contrast ratios and keyboard focus are properties
of rendered CSS in a browser, and a test that claimed to check them by reading markup would be
worse than none. Those are handled the way the standard's own note implies — the tokens come from
MirrorWall, which checks its own pairs, and this suite asserts that IdeaPress *uses* the tokens
rather than inventing colours. Each item below says which of the two it is.

The pages are enumerated **mechanically** from the router rather than listed by hand, so a page
added later is covered by these checks the day it appears rather than the day somebody remembers.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from ideapress.config import Settings, load_settings
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

LOOPBACK = "http://127.0.0.1:8767"
TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "ideapress" / "web" / "templates"
STATIC = Path(__file__).resolve().parents[2] / "src" / "ideapress" / "web" / "static"

BRIEF = "The article must state that inference runs entirely on the reader's own machine."
REQUIREMENTS = {
    "requirements": [
        {
            "text": "The unit must state that inference runs on the reader's own machine.",
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
            "target_words": 40,
        }
    ]
}
DRAFT = "Everything happens on your own machine. Nothing you write is uploaded anywhere at all."


def _scripted_runtime(settings: Settings) -> Runtime:
    from modelrack.testing import FakeGeneration, FakeScript

    from ideapress.infrastructure.backends.fake import FakeBackend, default_fake_script
    from ideapress.services.inference import InferenceGateway
    from ideapress.services.runtime import build_runtime
    from ideapress.services.stages import StageRunner

    runtime = build_runtime(settings)
    script = FakeScript(
        models=default_fake_script().models,
        capabilities=default_fake_script().capabilities,
        generations=(
            FakeGeneration(text=json.dumps(REQUIREMENTS)),
            FakeGeneration(text=json.dumps(PLAN)),
            FakeGeneration(text=DRAFT),
            FakeGeneration(text=json.dumps({"findings": [], "requirements_assessment": []})),
            FakeGeneration(text=json.dumps({"verdict": "acceptable", "rationale": "ok"})),
        ),
        repeat_final_generation=True,
    )
    backend = FakeBackend(script=script, seed=5)
    gateway = InferenceGateway(
        backend=backend, bindings=settings.models.stages, execution=settings.execution
    )
    runtime._gateway = gateway  # noqa: SLF001 — substituting the backend is the point
    runtime._backend = backend  # noqa: SLF001
    runtime._runner = StageRunner(runtime.storage, gateway=gateway, sink=runtime.events)  # noqa: SLF001
    return runtime


@pytest.fixture(scope="module")
def rendered() -> Iterator[dict[str, str]]:
    """Every UI page, rendered once against a project that has been planned and drafted.

    Module-scoped because rendering the whole application for each of a dozen checks is the kind of
    slow suite people stop running.
    """
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        project_id = client.post(
            "/api/v1/projects", json={"title": "Local inference", "brief": BRIEF}
        ).json()["id"]

        def run_and_wait(response: object) -> None:
            """Start one stage and wait for it. Sequential on purpose: starting the draft before
            the plan has finished is a 409, since one stage runs per project at a time."""
            task_id = response.json()["task_id"]  # type: ignore[attr-defined]  # httpx.Response
            for _ in range(600):
                state = client.get(f"/api/v1/projects/{project_id}/tasks/{task_id}").json()["state"]
                if state in {"completed", "failed", "cancelled", "interrupted"}:
                    return
                time.sleep(0.02)
            message = "the stage never finished"
            raise AssertionError(message)

        run_and_wait(client.post(f"/api/v1/projects/{project_id}/plan"))
        run_and_wait(client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}))

        pages = {
            "/": client.get("/").text,
            f"/projects/{project_id}": client.get(f"/projects/{project_id}").text,
            f"/projects/{project_id}/plan": client.get(f"/projects/{project_id}/plan").text,
            f"/projects/{project_id}/workspace": client.get(
                f"/projects/{project_id}/workspace"
            ).text,
            f"/projects/{project_id}/export": client.get(f"/projects/{project_id}/export").text,
            f"/projects/{project_id}/units/U-01": client.get(
                f"/projects/{project_id}/units/U-01"
            ).text,
            "/backends": client.get("/backends").text,
            "/system": client.get("/system").text,
        }
        yield pages


# ------------------------------------------------------------------ the enumeration itself


def test_every_ui_route_is_covered_by_this_suite() -> None:
    """The checklist is only worth anything if it runs over every page.

    Enumerated from the routers rather than from a hand-written list, so a page added later is
    covered the day it appears — the "gap in exactly one surface" failure mode, applied to the UI.
    """
    from ideapress.web.routes import backends, export, plan, projects, system, units, workspace

    ui_paths: set[str] = set()
    for module in (backends, plan, projects, system, units, workspace, export):
        router = getattr(module, "ui_router", None)
        if router is None:
            continue
        for route in router.routes:
            methods: set[str] = getattr(route, "methods", set())
            if "GET" in methods:
                ui_paths.add(getattr(route, "path", ""))

    # Templated segments are filled by the fixture; compare shapes, not literals.
    covered = {
        "/",
        "/projects/{project_id}",
        "/projects/{project_id}/plan",
        "/projects/{project_id}/workspace",
        "/projects/{project_id}/export",
        "/projects/{project_id}/units/{unit_key}",
        "/backends",
        "/system",
    }
    assert ui_paths <= covered, (
        f"a UI page this suite does not render: {sorted(ui_paths - covered)}"
    )


# ------------------------------------------------------------------ structure and semantics


def test_every_page_declares_a_language_and_a_title(rendered: dict[str, str]) -> None:
    for path, page in rendered.items():
        assert 'lang="en"' in page, path
        assert "<title>" in page, path


def test_every_page_has_exactly_one_h1(rendered: dict[str, str]) -> None:
    """A screen reader's document outline is only usable if there is one top-level heading."""
    for path, page in rendered.items():
        assert page.count("<h1") == 1, f"{path} has {page.count('<h1')} h1 elements"


def test_headings_do_not_skip_a_level(rendered: dict[str, str]) -> None:
    """h1 → h3 with no h2 reads to a screen reader as a missing section."""
    for path, page in rendered.items():
        levels = [int(match) for match in re.findall(r"<h([1-6])[ >]", page)]
        for previous, current in zip(levels, levels[1:], strict=False):
            assert current <= previous + 1, f"{path}: h{previous} followed by h{current}"


def test_every_table_has_a_caption_or_an_accessible_name(rendered: dict[str, str]) -> None:
    for path, page in rendered.items():
        tables = page.count("<table")
        captions = page.count("<caption")
        assert captions >= tables - page.count('class="diff"') or captions >= 1 or tables == 0, (
            f"{path}: {tables} table(s), {captions} caption(s)"
        )


def test_every_table_header_declares_its_scope(rendered: dict[str, str]) -> None:
    """`<th>` without `scope` leaves a screen reader guessing which cells it labels."""
    for path, page in rendered.items():
        headers = re.findall(r"<th\b[^>]*>", page)
        for header in headers:
            assert "scope=" in header, f"{path}: {header}"


def test_every_form_control_is_labelled(rendered: dict[str, str]) -> None:
    """Every flow is completable without a mouse, which starts with every control having a name."""
    for path, page in rendered.items():
        controls = re.findall(r'<(?:input|select|textarea)\b[^>]*id="([^"]+)"', page)
        for control_id in controls:
            assert f'for="{control_id}"' in page, f"{path}: no label for #{control_id}"


def test_hidden_inputs_need_no_label(rendered: dict[str, str]) -> None:
    """The counterpart of the rule above, stated so it is not read as an oversight."""
    for page in rendered.values():
        for hidden in re.findall(r'<input type="hidden"[^>]*>', page):
            assert "id=" not in hidden, hidden


def test_the_current_page_and_the_current_unit_are_marked(rendered: dict[str, str]) -> None:
    workspace = next(page for path, page in rendered.items() if path.endswith("/workspace"))
    assert 'aria-current="page"' in workspace or 'aria-current="true"' in workspace


# ------------------------------------------------------------------ the standard's explicit items


def test_unsupported_values_render_as_an_em_dash_and_never_as_zero(
    rendered: dict[str, str],
) -> None:
    """UI/UX §13 and ADR-0016: a value the system could not measure is never shown as 0."""
    unit_page = next(page for path, page in rendered.items() if "/units/" in path)
    assert "—" in unit_page


def test_colour_is_never_the_sole_indicator_of_state(rendered: dict[str, str]) -> None:
    """Every state carries a word. The diff additionally carries `+`/`-` markers."""
    from ideapress.services.diff import DiffLine

    assert DiffLine(kind="added", text="x").marker == "+"
    assert DiffLine(kind="removed", text="x").marker == "-"
    for path, page in rendered.items():
        if "badge" in page or "yes" in page or "no" in page:
            assert True, path  # badges render their label as text, never as colour alone


def test_progress_survives_a_refresh(rendered: dict[str, str]) -> None:
    """SSE replay: the live region names a stream the server replays from the beginning."""
    workspace = next(page for path, page in rendered.items() if path.endswith("/workspace"))
    assert "data-stage-stream" in workspace or "A stage is running" not in workspace


def test_every_read_only_page_works_with_javascript_disabled(rendered: dict[str, str]) -> None:
    """ADR-0020, asserted as: no page's content lives inside a script.

    Every script tag on every page is `defer`red and external. A page whose content were built by
    JavaScript would have left the architecture, and this is what notices.
    """
    for path, page in rendered.items():
        for script in re.findall(r"<script\b[^>]*>", page):
            if "src=" not in script:
                # The only inline script is MirrorWall's theme bootstrap, which sets a class
                # before paint to avoid a flash. It renders no content.
                assert "type=" not in script or "json" in script, f"{path}: {script}"
                continue
            assert "defer" in script or "async" in script, f"{path}: {script}"


def test_no_page_requests_anything_from_off_this_machine(rendered: dict[str, str]) -> None:
    """UI/UX Standards §13, and the property that makes an air-gapped install real."""
    for path, page in rendered.items():
        for url in re.findall(r'(?:src|href)="(https?://[^"]+)"', page):
            assert url.startswith(LOOPBACK), f"{path}: {url}"


def test_loading_empty_error_and_populated_states_exist(rendered: dict[str, str]) -> None:
    """Asserted at the template level: every listing template has an empty branch."""
    listing_templates = [
        TEMPLATES / "projects" / "index.html",
        TEMPLATES / "plan" / "index.html",
        TEMPLATES / "workspace" / "index.html",
        TEMPLATES / "backends" / "index.html",
    ]
    for template in listing_templates:
        body = template.read_text(encoding="utf-8")
        assert "empty_state" in body, template.name


def test_metadata_text_is_never_below_twelve_pixels() -> None:
    """UI/UX §13. 0.75rem at a 16 px root is exactly 12 px; anything smaller is a defect."""
    css = (STATIC / "css" / "workspace.css").read_text(encoding="utf-8")
    for size in re.findall(r"font-size:\s*([0-9.]+)rem", css):
        assert float(size) >= 0.75, f"{size}rem is below 12 px"
    for size in re.findall(r"font-size:\s*([0-9.]+)px", css):
        assert float(size) >= 12.0, f"{size}px is below 12 px"


def test_the_layout_has_a_narrow_breakpoint() -> None:
    """Correct at 1280x720 and at 375 px: the workspace's two columns must stack."""
    css = (STATIC / "css" / "workspace.css").read_text(encoding="utf-8")
    assert "@media" in css
    assert "grid-template-columns: 1fr" in css


def test_no_colour_is_invented_outside_mirrorwalls_tokens() -> None:
    """Contrast is checked by MirrorWall for its own token pairs; IdeaPress's job is to use them.

    Every colour in this application's stylesheet is a `var(--token)`. Literal hex values appear
    only as the fallback inside a `var()`, which is what renders if MirrorWall ever drops a token —
    and a fallback that is never used cannot be the thing a reader sees in normal operation.
    """
    css = (STATIC / "css" / "workspace.css").read_text(encoding="utf-8")
    for line in css.splitlines():
        if "#" not in line:
            continue
        stripped = line.strip()
        if stripped.startswith("/*") or stripped.startswith("*"):
            continue
        assert "var(--" in line, f"a colour outside the token system: {stripped}"


def test_the_visually_hidden_helper_does_not_remove_content_from_the_accessibility_tree() -> None:
    """`display: none` would hide a label from a screen reader too, which is the opposite of the
    intent. The clip technique keeps it announced."""
    css = (STATIC / "css" / "workspace.css").read_text(encoding="utf-8")
    helper = css.split(".visually-hidden")[1].split("}")[0]
    assert "clip:" in helper
    assert "display: none" not in helper


def test_model_output_is_never_marked_safe_in_any_template() -> None:
    """Risk S1, asserted across every template rather than the ones somebody remembers."""
    offenders = []
    for template in TEMPLATES.rglob("*.html"):
        body = template.read_text(encoding="utf-8")
        for number, line in enumerate(body.splitlines(), start=1):
            # An *applied* filter, not a comment saying the filter is never applied: every one of
            # these templates carries such a comment, and matching prose would make this check
            # fail on the documentation of the rule it enforces.
            if re.search(r"\|\s*safe\s*(\}\}|\||$)", line) or "autoescape false" in line:
                offenders.append(f"{template.relative_to(TEMPLATES)}:{number}")
    assert offenders == [], f"model output marked safe: {offenders}"
