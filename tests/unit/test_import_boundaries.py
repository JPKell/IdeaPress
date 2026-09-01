"""Risk T9 and P2 AC2: provider specifics never reach workflow code.

``.importlinter`` enforces the layer and application boundaries; it cannot express "``modelrack``
may be imported here and nowhere else", because the permitted place is a subpackage of the
forbidden one. This walks the AST instead, which also catches an import inside a function body —
where a leak would otherwise hide from a module-level scan.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "ideapress"

# The adapters are the only place a provider library belongs. `services.inference` is on the list
# for `modelrack` alone: it is the single choke point every stage reaches a model through, so it
# holds the provider handle — but it never speaks HTTP itself.
ADAPTER_ONLY = SRC / "infrastructure" / "backends"

# How many generated modules are still a docstring and a TODO. Ratcheted down by each unit.
SCAFFOLD_REMAINING = 2


def _python_files() -> Iterator[Path]:
    yield from sorted(SRC.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("library", ["modelrack", "httpx"])
def test_provider_libraries_appear_only_in_adapters(library: str) -> None:
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if library in _imported_roots(path) and not path.is_relative_to(ADAPTER_ONLY)
    ]
    assert offenders == [], f"{library} imported outside infrastructure/backends: {offenders}"


def test_no_module_imports_another_application() -> None:
    """Spec §20 AC8, restated at the AST level so a lazy import inside a function is caught too."""
    offenders = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if _imported_roots(path) & {"loadcoach", "freeweight"}
    ]
    assert offenders == []


def test_domain_imports_no_framework() -> None:
    frameworks = {"fastapi", "starlette", "sqlalchemy", "typer", "jinja2", "httpx", "alembic"}
    offenders = {
        path.relative_to(SRC).as_posix(): sorted(_imported_roots(path) & frameworks)
        for path in (SRC / "domain").rglob("*.py")
        if _imported_roots(path) & frameworks
    }
    assert offenders == {}


def _is_bare_scaffold(path: Path) -> bool:
    """Whether this module is still a docstring and nothing else.

    A module with no statements has no annotations to postpone, so requiring the future import
    there would be noise. The exemption is self-closing: as each scaffold module gains a body it
    joins the assertion below automatically, and :func:`test_scaffold_modules_are_disappearing`
    keeps count so the exemption cannot quietly become permanent.
    """
    body = ast.parse(path.read_text(encoding="utf-8"), filename=str(path)).body
    return len(body) <= 1 and all(
        isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) for node in body
    )


def test_every_implemented_module_uses_postponed_annotations() -> None:
    """Coding standards: `from __future__ import annotations` at the top of every module."""
    missing = [
        path.relative_to(SRC).as_posix()
        for path in _python_files()
        if path.name != "__about__.py"
        and not _is_bare_scaffold(path)
        and "from __future__ import annotations" not in path.read_text(encoding="utf-8")
    ]
    assert missing == []


def test_scaffold_modules_are_disappearing() -> None:
    """The generated scaffold started at 70 docstring-only modules; the count only goes down.

    This is a ratchet, not a target: it fails if a *new* empty module appears, and it fails when
    the number falls so the constant gets corrected rather than forgotten.
    """
    remaining = sorted(
        path.relative_to(SRC).as_posix() for path in _python_files() if _is_bare_scaffold(path)
    )
    assert len(remaining) <= SCAFFOLD_REMAINING, f"new empty module(s): {remaining}"
    assert len(remaining) == SCAFFOLD_REMAINING, (
        f"{SCAFFOLD_REMAINING - len(remaining)} scaffold module(s) implemented since this was "
        f"last updated; set SCAFFOLD_REMAINING to {len(remaining)}."
    )


def test_every_subprocess_in_the_suite_names_its_working_directory() -> None:
    """M7-9's trap, closed by a ratchet rather than by remembering.

    pytest-cov starts coverage inside a subprocess through a `.pth` hook that reads
    `pyproject.toml` **relative to the working directory**. The autouse fixtures `chdir` into a
    temporary directory, so a subprocess spawned without an explicit `cwd` measures coverage
    without `branch = true` and writes a data file the parent's cannot combine with. That aborts
    the whole `--cov` run inside pytest-cov — an INTERNALERROR, not a test failure — and only the
    coverage job sees it. It has now happened twice.
    """
    tests_root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in sorted(tests_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = node.func
            name = (
                f"{target.value.id}.{target.attr}"
                if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)
                else ""
            )
            if name not in {"subprocess.run", "subprocess.Popen", "subprocess.check_output"}:
                continue
            if not any(keyword.arg == "cwd" for keyword in node.keywords):
                offenders.append(f"{path.relative_to(tests_root)}:{node.lineno} {name}")
    assert offenders == [], f"subprocess call(s) with no explicit cwd: {offenders}"


def test_only_the_gateway_calls_a_backend_to_generate() -> None:
    """ADR-0038's first obligation, asserted rather than asserted-in-a-docstring.

    `InferenceGateway.run` says "this is the only function in IdeaPress that calls a backend's
    `generate`" and that a test walks the source to prove it. Until M8 no test did — the claim was
    load-bearing and unchecked, which is the exact shape of the M5 lesson it cites (LoadCoach's
    synchronous path bypassed the circuit breaker its queue honoured, and nothing noticed until a
    verification looked for a second entry point).

    The adapters are exempt: `generate` on a *provider* is how an adapter does its job. What is
    forbidden is a second module reaching a `backend` object and asking it to generate.
    """
    offenders = []
    for path in _python_files():
        relative = path.relative_to(SRC).as_posix()
        if relative == "services/inference.py" or relative.startswith("infrastructure/backends/"):
            continue
        source = path.read_text(encoding="utf-8")
        for number, line in enumerate(source.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "backend.generate(" in stripped or "_backend.generate(" in stripped:
                offenders.append(f"{relative}:{number}")
    assert offenders == [], f"a second door to a model: {offenders}"
