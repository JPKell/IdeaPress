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
SCAFFOLD_REMAINING = 13


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
