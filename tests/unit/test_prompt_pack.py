"""The prompt pack: ADR-0012 and ADR-0028.

Prompts are versioned JSON records, never Python string literals, and the loader, renderer and
hashing come from `setspec.prompts` — IdeaPress supplies only its own pack. Two of these tests are
the ones that keep it true as the pack grows: one greps the source for inline prompt strings, and
one rebuilds the manifest and asserts nothing drifted.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from setspec.prompts import PromptNotFound, PromptVariableError, build_manifest, load_pack

from ideapress.services.prompts import PACK_ROOT, library, prompts_health_component, render

SRC = Path(__file__).resolve().parents[2] / "src" / "ideapress"


def test_the_pack_parses_and_names_itself() -> None:
    pack = library()
    assert pack.pack_id == "ideapress.stages"
    assert pack.pack_version
    assert list(pack.ids())


def test_the_manifest_is_current() -> None:
    """A prompt edited without regenerating the manifest silently changes what a model was asked."""
    _, drift = build_manifest(PACK_ROOT, generated_at="2026-08-31T00:00:00Z")
    assert drift.added == ()
    assert drift.removed == ()
    assert drift.changed == ()


def test_every_record_declares_every_variable_its_template_uses() -> None:
    for record in library().all_records():
        declared = set(record.variables)
        used = {
            token.split("|")[0].strip()
            for token in record.template.split("{{")[1:]
            for token in [token.split("}}")[0]]
        }
        assert used <= declared, f"{record.prompt_id} uses undeclared: {used - declared}"


def test_every_record_states_a_change_reason() -> None:
    """Risk G3, prompt sprawl: a version with no stated reason is a version nobody can review."""
    for path in sorted(PACK_ROOT.rglob("*.json")):
        if path.name == "manifest.json":
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        assert record["metadata"]["change_reason"].strip(), path.name
        assert record["metadata"]["owner"] == "ideapress"


def test_rendering_carries_the_provenance_fields() -> None:
    rendered = render("stages.hello", {"title": "A study"})
    assert rendered.prompt_id == "stages.hello"
    assert rendered.version == "1.0.0"
    assert rendered.sha256.startswith("sha256:")
    assert "A study" in rendered.user
    assert rendered.system


def test_a_missing_required_variable_is_refused() -> None:
    with pytest.raises(PromptVariableError):
        render("stages.hello", {})


def test_an_unknown_prompt_is_refused() -> None:
    with pytest.raises(PromptNotFound):
        render("stages.no_such_prompt", {})


def test_no_inline_prompt_strings_in_python() -> None:
    """ADR-0012. A long imperative string literal outside the pack is a prompt in hiding.

    The heuristic: a string constant over 200 characters, in a module that is not a test and not
    the configuration example, containing a second-person instruction or an output-format
    imperative. It is deliberately narrow — a docstring is not a prompt, and a length-only
    threshold would flag the example config and the exporter's inline CSS — but M7 noted the
    original five markers let a marker-free imperative prompt slip through, so the set now covers
    the phrasings prompts actually use. It is checked against the AST rather than the text so
    that a comment describing a prompt does not trip it.
    """
    markers = (
        "you are ",
        "you must",
        "you will",
        "your task",
        "your job",
        "your role",
        "respond with",
        "respond only",
        "respond in",
        "return only",
        "return json",
        "return a json",
        "output only",
        "output json",
        "answer with",
        "answer only",
        "answer in json",
        "do not explain",
        "do not include",
        "do not add",
        "step by step",
        "as a json object",
        "in the following format",
    )
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or len(node.value) <= 200:
                continue
            lowered = node.value.lower()
            if any(marker in lowered for marker in markers):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == [], f"inline prompt text found: {offenders}"


def test_the_pack_hashes_identically_under_the_installed_setspec() -> None:
    """Integration I9: one record, one hash, whatever component loads it.

    The figure is recorded in the handoff. If this ever disagrees with FreeWeight's or LoadCoach's
    installed `setspec`, the packs are no longer comparable and every prompt-version provenance
    claim across the suite is suspect.
    """
    pack = load_pack(PACK_ROOT)
    (reference,) = pack.references([("stages.hello", None)])
    assert reference.sha256 == (
        "sha256:b9f17cf04bb076bcd7a660e169a73238e41e63987fead814d52bb36d6d7e1cb5"
    )


def test_prompts_health_is_ok_when_the_pack_loads() -> None:
    health = prompts_health_component()
    assert health.status.value == "ok"
    assert "ideapress.stages" in (health.detail or "")
