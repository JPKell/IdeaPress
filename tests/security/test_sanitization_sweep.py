"""Model output is inert in **every** view and **every** export format — P9's sanitization sweep.

The plan names the failure mode precisely: *a sanitizer gap in one export format but not another*.
A test that listed the surfaces by hand would be a test that goes stale the day a format is added,
so the surfaces here are **enumerated mechanically** — the export formats from
`services.export.FORMATS`, the templates by walking the template directory, and the UI pages from
the routers. Add a format or a page and it is swept the day it appears.

What "inert" means is format-specific, and saying so is the point:

* **HTML** (the web views and the HTML export) — escaped, so a browser renders the payload as text
  and executes nothing. This is the one that matters and the one asserted hardest.
* **JSON** — the payload is a *string value*; it cannot terminate its own string or add a key.
* **Markdown** — a source-text format with no renderer in this product. What is asserted is that
  the payload cannot break the document's own structure (its provenance tables in particular), and
  that no Markdown-to-HTML path exists anywhere in `src/` — because the day one is added, spec
  §14's allowlist stops being a sentence about nothing and this test starts failing.

The payloads are the ones Security Standards §14 names, plus the ones this application's own
templating makes relevant.
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
from ideapress.services.export import FORMATS
from ideapress.web.app import create_app

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

LOOPBACK = "http://127.0.0.1:8767"
SRC = Path(__file__).resolve().parents[2] / "src" / "ideapress"
TEMPLATES = SRC / "web" / "templates"

# Security Standards §14's list, each with the reason it is here.
PAYLOADS: dict[str, str] = {
    "script": "<script>alert('xss')</script>",
    "img_onerror": "<img src=x onerror=alert(1)>",
    "jinja": "{{ 7*7 }}",
    "jinja_statement": "{% raw %}{% endraw %}",
    "traversal": "../../etc/passwd",
    "sql": "'; DROP TABLE units; --",
    "svg_onload": "<svg/onload=alert(1)>",
    "attribute_break": '" onmouseover="alert(1)',
    "markdown_table_break": "| broken | table |",
    "json_break": '", "injected": "yes',
    "null_byte": "null\x00byte",
    "entity": "&lt;already escaped&gt;",
}
# Real prose around the payloads, and enough of it: a draft that fails the length band goes to
# `repair`, and what this file needs to observe is a *committed* unit carrying hostile text through
# every rendering surface — not the repair loop.
# `../../etc/passwd` is deliberately **not** in the committed draft: `no_path_traversal` is a
# *blocking* validator, so a unit containing one correctly never commits — which is asserted on its
# own below rather than being allowed to stop this file from reaching the export surfaces at all.
RENDERED_PAYLOADS = {name: value for name, value in PAYLOADS.items() if name != "traversal"}
HOSTILE_TEXT = (
    "Everything happens on your own machine. The model is loaded from local storage and inference "
    "runs on hardware you already own, which is the whole point of the arrangement. "
    + " ".join(RENDERED_PAYLOADS.values())
    + " Nothing you write is uploaded anywhere at all, there is no account to create, and no "
    "network connection is required once the model has been downloaded to your own machine. "
    "The trade is that you supply the hardware, and that trade is worth stating plainly rather "
    "than burying it in a footnote where nobody reading in a hurry would ever find it."
)

# The payload is in the *brief* as well as the model's answers, because a requirement's grounding
# quote must be a verbatim span of the author's material — so this is the only way a hostile
# payload legitimately reaches the quote column that the plan page and both exports render.
GROUNDING_QUOTE = f"own machine {PAYLOADS['jinja']} {PAYLOADS['script']}"
BRIEF = (
    "The article must state that inference runs entirely on the reader's "
    f"{GROUNDING_QUOTE} and nowhere else."
)
REQUIREMENTS = {
    "requirements": [
        {
            "text": f"The unit must state where inference runs. {PAYLOADS['script']}",
            "blocking": True,
            "source_document": "brief",
            "source_quote": GROUNDING_QUOTE,
            "checks": [{"kind": "must_contain_any", "values": ["own machine"]}],
        }
    ]
}
PLAN = {
    "units": [
        {
            "title": f"Where the work happens {PAYLOADS['img_onerror']}",
            "goal_text": f"Say plainly where inference runs. {PAYLOADS['sql']}",
            "requirement_keys": ["R-001"],
            # Matched to HOSTILE_TEXT's real length: the band is half to nearly twice the target,
            # and a draft outside it goes to `repair`, which is not what this file is observing.
            "target_words": 120,
        }
    ]
}


def _scripted_runtime(settings: Settings) -> Runtime:
    """A runtime whose every model answer carries every payload."""
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
            FakeGeneration(text=HOSTILE_TEXT),
            FakeGeneration(
                text=json.dumps(
                    {
                        "findings": [
                            {
                                "category": PAYLOADS["script"],
                                "severity": "minor",
                                "problem_text": PAYLOADS["img_onerror"],
                                "evidence_text": PAYLOADS["jinja"],
                                "required_fix_text": PAYLOADS["svg_onload"],
                            }
                        ],
                        "requirements_assessment": [],
                    }
                )
            ),
            FakeGeneration(
                text=json.dumps({"verdict": "acceptable", "rationale": PAYLOADS["attribute_break"]})
            ),
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
def hostile() -> Iterator[tuple[TestClient, str]]:
    """A committed project every one of whose fields carries a hostile payload."""
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url=LOOPBACK) as client:
        project_id = client.post(
            "/api/v1/projects",
            json={"title": f"Local inference {PAYLOADS['script']}", "brief": BRIEF},
        ).json()["id"]

        def run(response: object) -> None:
            task_id = response.json()["task_id"]  # type: ignore[attr-defined]  # httpx.Response
            for _ in range(600):
                state = client.get(f"/api/v1/projects/{project_id}/tasks/{task_id}").json()["state"]
                if state in {"completed", "failed", "cancelled", "interrupted"}:
                    return
                time.sleep(0.02)
            message = "the stage never finished"
            raise AssertionError(message)

        run(client.post(f"/api/v1/projects/{project_id}/plan"))
        run(client.post(f"/api/v1/projects/{project_id}/stages/draft/run", json={}))
        yield client, project_id


# ------------------------------------------------------------------ it really is stored


def test_the_payloads_are_stored_verbatim_rather_than_stripped(
    hostile: tuple[TestClient, str],
) -> None:
    """Spec §20 AC10: model output is **stored** and rendered inert — not silently mangled.

    Stripping on the way in would lose the author's real content along with the payload, and would
    make every downstream inertness claim untestable: there would be nothing left to render.
    """
    client, project_id = hostile
    unit = client.get(f"/api/v1/projects/{project_id}/units/U-01").json()
    assert PAYLOADS["script"] in unit["content"]
    assert PAYLOADS["jinja"] in unit["content"]


# ------------------------------------------------------------------ every view


def _ui_pages(client: TestClient, project_id: str) -> dict[str, str]:
    """Every UI page, enumerated from the routers rather than listed here."""
    from ideapress.web.routes import backends, plan, projects, system, units, workspace

    paths: list[str] = []
    for module in (projects, plan, workspace, units, backends, system):
        router = getattr(module, "ui_router", None)
        if router is None:
            continue
        for route in router.routes:
            if "GET" not in getattr(route, "methods", set()):
                continue
            path = str(getattr(route, "path", ""))
            paths.append(path.replace("{project_id}", project_id).replace("{unit_key}", "U-01"))
    return {path: client.get(path).text for path in sorted(set(paths))}


def test_no_view_renders_an_executable_script_tag(hostile: tuple[TestClient, str]) -> None:
    """The sweep. Every page, enumerated mechanically, must escape the payload.

    Asserted as the *absence of the executable form* rather than the presence of the escaped one:
    a page could contain both, and only the executable one is the defect.
    """
    client, project_id = hostile
    offenders = []
    dangerous = ("script", "img_onerror", "svg_onload", "attribute_break")
    for path, page in _ui_pages(client, project_id).items():
        body = page.split("</head>", 1)[-1]
        for name in dangerous:
            # The *literal* payload in the body is the defect. Matching a fragment like
            # `onerror=alert` instead would flag escaped text as an offence: HTML escaping touches
            # `< > & " \'` and not `=`, so `&lt;img src=x onerror=alert(1)&gt;` — inert, and
            # correct — still contains that fragment.
            if PAYLOADS[name] in body:
                offenders.append(f"{path}: {name}")
    assert offenders == [], f"unescaped model output rendered on: {offenders}"


def test_every_view_escapes_the_payload_it_shows(hostile: tuple[TestClient, str]) -> None:
    """The other half: where the payload appears at all, it appears escaped."""
    client, project_id = hostile
    seen_anywhere = False
    for path, page in _ui_pages(client, project_id).items():
        if "alert(" not in page:
            continue
        seen_anywhere = True
        assert "&lt;script&gt;" in page or "&lt;img" in page or "&lt;svg" in page, path
    assert seen_anywhere, "no page showed the payload at all; the sweep proved nothing"


def test_template_syntax_in_model_output_is_never_evaluated(
    hostile: tuple[TestClient, str],
) -> None:
    """`{{ 7*7 }}` reaching a template is the injection an autoescaping engine does not stop —
    only never passing model output through `render_template_string` does."""
    client, project_id = hostile
    # Proved by the *survival of the literal*, not by the absence of `49`: `49` occurs innocently
    # in any page showing a character count, while `{{ 7*7 }}` surviving intact is possible only if
    # nothing evaluated it. Jinja escapes `{` and `}` to themselves, so the literal is what shows.
    pages = _ui_pages(client, project_id)
    showing = [path for path, page in pages.items() if "{{ 7*7 }}" in page]
    assert showing, "no page rendered the template payload at all; this proved nothing"
    # Statement syntax is checked independently: the two payloads reach different fields, so they
    # do not land on the same pages, and requiring both on one page would assert the fixture's
    # shape rather than the property.
    statements = [path for path, page in pages.items() if "{% raw %}" in page]
    assert statements, "no page rendered the statement payload; that half proved nothing"


def test_no_template_applies_the_safe_filter(hostile: tuple[TestClient, str]) -> None:
    """Walked mechanically over the template tree, so a new template is covered on day one."""
    offenders = []
    for template in sorted(TEMPLATES.rglob("*.html")):
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"\|\s*safe\s*(\}\}|\||$)", line) or "autoescape false" in line:
                offenders.append(f"{template.relative_to(TEMPLATES)}:{number}")
    assert offenders == [], f"model output marked safe: {offenders}"


def test_no_module_renders_a_template_from_a_string() -> None:
    """The one way autoescaping is bypassed without saying `| safe`.

    `Template(...)` or `from_string(...)` over anything a model produced would evaluate `{{ }}` in
    it. Nothing in this application constructs a template from a runtime string, and this is what
    keeps that true.
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        for marker in ("from_string(", "Template(", "render_template_string"):
            if marker in body:
                offenders.append(f"{path.relative_to(SRC)}: {marker}")
    assert offenders == [], f"a template built from a string: {offenders}"


# ------------------------------------------------------------------ every export format


def test_the_sweep_covers_every_format_the_application_ships() -> None:
    """The guard on the guard: this file's per-format assertions must cover `FORMATS` exactly.

    If a fourth format is added and no case is written for it, this fails — which is the whole
    difference between a sweep and a list somebody once wrote.
    """
    covered = {"markdown", "html", "json"}
    assert set(FORMATS) == covered, (
        f"an export format with no inertness case: {sorted(set(FORMATS) - covered)}"
    )


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_every_export_format_carries_the_payload_without_executing_it(
    fmt: str, hostile: tuple[TestClient, str]
) -> None:
    """Every format, parametrised from `FORMATS` so a new one is swept automatically."""
    client, project_id = hostile
    body = client.get(f"/api/v1/projects/{project_id}/export?format={fmt}")
    assert body.status_code == 200, body.text
    text = body.text
    assert "own machine" in text, f"{fmt}: the real content did not survive"
    if fmt == "html":
        # The literal payload, not a fragment of it: escaping touches `< > & " \'` and not `=`, so
        # a correctly escaped `&lt;img src=x onerror=alert(1)&gt;` still contains `onerror=alert`
        # and is nonetheless inert.
        for name in ("script", "img_onerror", "svg_onload"):
            assert PAYLOADS[name] not in text, f"{name} is unescaped in the HTML export"
        assert "&lt;script&gt;" in text, "the payload is not in the export at all"
    elif fmt == "json":
        parsed = json.loads(text)
        # The payload appears as *text inside a string value*, which is correct and expected. What
        # must not happen is it becoming structure: a key of its own on the unit object.
        assert "injected" not in parsed["units"][0], "the payload terminated its own string"
        assert PAYLOADS["json_break"] in parsed["units"][0]["content"]
    else:
        # Markdown is source text with no renderer in this product; see the module docstring.
        assert "| Requirement |" in text, "the provenance table is missing"


def test_the_html_export_opens_with_no_network_and_no_script(
    hostile: tuple[TestClient, str],
) -> None:
    """Self-contained and inert: the two properties that let the file open from a USB stick."""
    client, project_id = hostile
    text = client.get(f"/api/v1/projects/{project_id}/export?format=html").text
    assert "<script" not in text
    assert "<link" not in text
    assert "src=" not in text.replace(
        "src=x onerror", "ESCAPED"
    )  # the payload's own `src=`, escaped
    for scheme in ("http://", "https://"):
        assert scheme not in text


def test_the_json_export_is_well_formed_and_the_payload_is_a_string_value(
    hostile: tuple[TestClient, str],
) -> None:
    """A payload that terminated its own string would add keys to the document."""
    client, project_id = hostile
    parsed = json.loads(client.get(f"/api/v1/projects/{project_id}/export?format=json").text)
    content = parsed["units"][0]["content"]
    assert isinstance(content, str)
    assert PAYLOADS["json_break"] in content
    assert "injected" not in parsed["units"][0]


def test_the_markdown_export_cannot_break_its_own_provenance_table(
    hostile: tuple[TestClient, str],
) -> None:
    """A `|` in a requirement's text would add columns to the coverage table and misalign it."""
    client, project_id = hostile
    text = client.get(f"/api/v1/projects/{project_id}/export?format=markdown").text
    rows = [line for line in text.splitlines() if line.startswith("| R-")]
    assert rows, "no coverage rows to check"
    for row in rows:
        assert row.count("|") == 7, f"a payload added columns: {row}"


def test_no_markdown_is_ever_rendered_to_html_anywhere_in_the_application() -> None:
    """Spec §14 says Markdown is sanitized with an allowlist. No allowlist exists — because
    nothing in this application renders Markdown: unit content goes into `<pre>` as escaped text.

    That is a correct state and a fragile one. The day somebody adds a Markdown renderer, the
    sentence in §14 becomes a requirement rather than a description, and this fails so it is
    implemented rather than assumed (M8-03).
    """
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        for marker in (
            "import markdown",
            "markdown.markdown",
            "import mistune",
            "import commonmark",
        ):
            if marker in body:
                offenders.append(f"{path.relative_to(SRC)}: {marker}")
    assert offenders == [], (
        f"a Markdown renderer appeared: {offenders}. Spec §14's allowlist is now required."
    )


# ------------------------------------------------------------------ nothing is executed


def test_no_model_output_reaches_a_subprocess_or_eval() -> None:
    """Workflows §11: a model may never cause code execution. Asserted over the whole source."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        body = path.read_text(encoding="utf-8")
        for marker in ("eval(", "exec(", "subprocess.", "os.system", "__import__("):
            for number, line in enumerate(body.splitlines(), start=1):
                if marker in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.relative_to(SRC)}:{number}: {marker}")
    assert offenders == [], f"a code-execution path exists: {offenders}"


def test_sql_metacharacters_in_model_output_do_not_reach_the_database_as_sql(
    hostile: tuple[TestClient, str],
) -> None:
    """The units table still exists after storing `'; DROP TABLE units; --`."""
    client, project_id = hostile
    units = client.get(f"/api/v1/projects/{project_id}/units").json()["units"]
    assert len(units) == 1
    assert PAYLOADS["sql"] in units[0]["goal"]


def test_a_path_traversal_in_model_output_is_blocking_and_never_commits() -> None:
    """The one payload that must not merely be rendered inert — it must stop the commit.

    Spec §14: model output is never used to build a path. `no_path_traversal` is the only
    *blocking* check in the safety validator, and that asymmetry is deliberate: a `<script>` tag in
    an article about web security is legitimate content that must render harmlessly, while a
    traversal string has no innocent reading in a unit this application will write to disk.
    """
    from ideapress.domain.validation import ValidationContext
    from ideapress.domain.validators.safety import SafetyValidator

    outcomes = SafetyValidator().check(
        ValidationContext(text=f"Read {PAYLOADS['traversal']} for the answer.")
    )
    traversal = [outcome for outcome in outcomes if outcome.check_key == "no_path_traversal"]
    assert traversal, "the safety validator no longer checks for path traversal"
    assert traversal[0].passed is False
    assert traversal[0].blocking is True


def test_a_script_tag_in_model_output_is_flagged_but_not_blocking() -> None:
    """The other side of the asymmetry, asserted so a later change to it is deliberate.

    An article about cross-site scripting legitimately contains `<script>`. Blocking it would be
    risk T4's named trap — validators too strict, blocking legitimate content — and the mitigation
    is that every surface renders it inert, which is what the rest of this file proves.
    """
    from ideapress.domain.validation import ValidationContext
    from ideapress.domain.validators.safety import SafetyValidator

    outcomes = SafetyValidator().check(ValidationContext(text=PAYLOADS["script"]))
    script = [outcome for outcome in outcomes if outcome.check_key == "no_script_tags"]
    assert script, "the safety validator no longer notices a script tag"
    assert script[0].passed is False
    assert script[0].blocking is False, "flagging is right; blocking would refuse legitimate work"
