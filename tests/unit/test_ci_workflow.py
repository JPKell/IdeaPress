"""The CI workflow has to parse, and it has to run the jobs it claims to.

A workflow GitHub cannot parse produces a run with **zero jobs** and an immediate `failure`, whose
name falls back to the file path — and no local tool reports it, because PyYAML accepts duplicate
mapping keys (last one wins) while GitHub's parser refuses them. That is how this repository
answered its first real push: an edit left a stale `env:` block on a step, so the whole workflow was
invalid and not one job ran.

These tests are cheap and they close a gap the documented gate cannot see at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


class _StrictLoader(yaml.SafeLoader):
    """A loader that refuses a duplicate mapping key, the way GitHub Actions does."""


def _no_duplicate_keys(loader: yaml.Loader, node: yaml.MappingNode, deep: bool = False) -> Any:  # noqa: FBT001, FBT002
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            message = f"duplicate key {key!r} at line {key_node.start_mark.line + 1}"
            raise yaml.constructor.ConstructorError(None, None, message, key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys)


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506
    assert isinstance(parsed, dict)
    return parsed


def test_the_workflow_parses_with_no_duplicate_keys(workflow: dict[str, Any]) -> None:
    assert workflow["name"] == "CI"
    assert workflow["jobs"]


def test_every_job_is_named_and_has_steps(workflow: dict[str, Any]) -> None:
    for name, job in workflow["jobs"].items():
        assert job.get("steps"), f"{name} has no steps"
        assert job.get("runs-on"), f"{name} does not say where to run"


def test_no_job_selects_a_marker_this_repository_does_not_declare(workflow: dict[str, Any]) -> None:
    """HR2's second defect: `-m integration` collects nothing and exits 5, silently, forever."""
    import tomllib

    pyproject = tomllib.loads((WORKFLOW.parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    declared = {
        entry.split(":", 1)[0] for entry in pyproject["tool"]["pytest"]["ini_options"]["markers"]
    }
    for name, job in workflow["jobs"].items():
        for step in job["steps"]:
            command = str(step.get("run", ""))
            if "pytest" not in command or " -m " not in command:
                continue
            expression = command.split(" -m ", 1)[1].split(" ")[0].strip("\"'")
            for token in expression.replace("(", " ").replace(")", " ").split():
                if token in {"not", "and", "or"}:
                    continue
                assert token in declared, f"{name} selects undeclared marker {token!r}"


def test_the_postgresql_job_names_the_driver_this_project_installs(
    workflow: dict[str, Any],
) -> None:
    """`postgresql://` resolves to psycopg2; this project installs psycopg3."""
    steps = workflow["jobs"]["db-matrix"]["steps"]
    urls = [
        value
        for step in steps
        for key, value in (step.get("env") or {}).items()
        if "URL" in key and isinstance(value, str)
    ]
    assert urls, "the PostgreSQL job sets no database URL"
    for url in urls:
        assert url.startswith("postgresql+psycopg://"), url


def test_the_postgresql_job_uses_the_variable_the_shared_helper_reads(
    workflow: dict[str, Any],
) -> None:
    environment: dict[str, str] = {}
    for step in workflow["jobs"]["db-matrix"]["steps"]:
        environment.update(step.get("env") or {})
    assert "WEIGHTSDB_POSTGRES_URL" in environment
    assert environment.get("WEIGHTSDB_REQUIRE_POSTGRES") == "1", "a skipped dialect is untested"


def test_the_postgres_service_matches_the_url_the_job_sets(workflow: dict[str, Any]) -> None:
    """A service nothing connects to is a job that has never run a query."""
    job = workflow["jobs"]["db-matrix"]
    service = job["services"]["postgres"]["env"]
    environment: dict[str, str] = {}
    for step in job["steps"]:
        environment.update(step.get("env") or {})
    url = environment["WEIGHTSDB_POSTGRES_URL"]
    assert f"//{service['POSTGRES_USER']}:{service['POSTGRES_PASSWORD']}@" in url
    assert url.endswith(f"/{service['POSTGRES_DB']}")
