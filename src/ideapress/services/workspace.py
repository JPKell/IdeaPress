"""ideapress.services.workspace — everything one workspace page needs, assembled once.

The workspace answers three questions about a unit without a page change — *what does it say*,
*what is wrong with it*, *what is it still missing* — plus the fourth that M7's verification found
missing: *why did it stop, and what do I do about it*.

This module assembles that view. It reads; it decides nothing. The route renders what it returns
and adds no logic of its own.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ideapress.services.runtime import Runtime

__all__ = ["BUDGET_PAUSE_HINT", "pause_guidance", "workspace_view"]

logger = logging.getLogger(__name__)

BUDGET_PAUSE_HINT = (
    "Raise `workflow.structured_output_tokens` in your config.toml (default 8192, accepted range "
    "1024–131072), then resume this unit. A reasoning model spends output tokens on thinking "
    "before its first word, so a budget that looks generous can be exhausted before the model "
    "reaches one — and this one knob is the lever for every stage that hits it."
)
"""What to do about a unit paused on an exhausted output budget.

Documented behaviour since 0.1.1, demonstrated end to end: pause, raise the knob, `--resume`. The
M7 verification's finding was not that this failed but that a person hit it with **no setting
named anywhere they were looking**, so the instruction lives next to the pause and names the
setting, its default and its range.
"""


def pause_guidance(paused_reason: str | None) -> dict[str, Any]:
    """Turn a pause reason into something a person can act on.

    Args:
        paused_reason: The reason recorded on the unit, or ``None`` when it is not paused.

    Returns:
        ``{"paused": bool, "reason": str, "hint": str, "kind": str}``. ``kind`` is
        ``output_budget`` when the pause was an exhausted output budget — the one pause with a
        specific, documented remedy — and ``other`` for the rest, which get the reason and the
        resume action but no invented advice.

    Guessing a remedy for a pause whose cause is unknown would be worse than silence: it sends a
    person to change a setting that was not the problem.
    """
    if not paused_reason:
        return {"paused": False, "reason": "", "hint": "", "kind": ""}
    lowered = paused_reason.lower()
    budget_shaped = (
        "output token" in lowered or "output budget" in lowered or "no text at all" in lowered
    )
    return {
        "paused": True,
        "reason": paused_reason,
        "kind": "output_budget" if budget_shaped else "other",
        "hint": BUDGET_PAUSE_HINT if budget_shaped else "",
    }


def workspace_view(
    runtime: Runtime,
    *,
    project_id: str,
    unit_key: str | None = None,
    compare_version: int | None = None,
) -> dict[str, Any]:
    """Assemble the workspace page for one project, focused on one unit.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        unit_key: Which unit to focus; the first in plan order when ``None``.
        compare_version: A version to diff the current one against, when the reader asked.

    Returns:
        Template context: the project, the navigator, the focused unit's full detail, its pause
        guidance, its diff when one was asked for, and the routing/egress facts about the backend
        that produced it.

    Raises:
        ProjectNotFound: No such project.

    A project with no plan yet is not an error: the navigator is empty, ``unit`` is ``None`` and the
    template renders its empty state. "Nothing has been planned" is a state the workspace has to
    show, not a failure it should raise on.
    """
    from ideapress.services.unit_reports import unit_detail, unit_list

    project = runtime.projects.get(project_id)
    units = unit_list(runtime, project_id=project_id)

    selected_key = unit_key or (units[0]["unit_key"] if units else None)
    detail: dict[str, Any] | None = None
    if selected_key is not None and any(u["unit_key"] == selected_key for u in units):
        detail = unit_detail(runtime, project_id=project_id, unit_key=selected_key)
    elif selected_key is not None:
        logger.info(
            "workspace.unknown_unit",
            extra={"project_id": project_id, "unit_key": selected_key},
        )
        selected_key = units[0]["unit_key"] if units else None
        if selected_key is not None:
            detail = unit_detail(runtime, project_id=project_id, unit_key=selected_key)

    view: dict[str, Any] = {
        "project": {"id": project.id, "title": project.title, "status": project.status},
        "units": units,
        "selected_unit_key": selected_key,
        "unit": detail,
        "pause": pause_guidance(detail.get("paused_reason") if detail else None),
        "backend": _backend_facts(runtime),
        "coverage_summary": _coverage_summary(detail),
        "diff": None,
        # The live view attaches to this when a stage is running, and the page says "reload to see
        # progress" when it is not — both correct with JavaScript disabled (ADR-0020).
        "running_task_id": _running_task_id(runtime, project_id),
    }
    if detail is not None and compare_version is not None:
        view["diff"] = _diff(
            runtime, project_id=project_id, unit_key=selected_key, compare=compare_version
        )
    return view


def _running_task_id(runtime: Runtime, project_id: str) -> str | None:
    """The id of this project's running stage, or ``None``.

    Args:
        runtime: The process's handles.
        project_id: The project.

    Returns:
        The active run's id, or ``None`` when nothing is running or no runner exists yet. Never
        raises: a workspace that would not render because the stage runner was unavailable would
        be a page failing over something it only decorates.
    """
    try:
        task = runtime.runner.active_task(project_id)
    except Exception:  # noqa: BLE001 — a live-view decoration never fails the page
        return None
    return task.run_id if task is not None else None


def _diff(
    runtime: Runtime, *, project_id: str, unit_key: str | None, compare: int
) -> dict[str, Any] | None:
    """The requested diff, or a refusal rendered as data rather than raised.

    A reader asking to compare against a version that no longer exists gets told so on the page
    they are looking at; it is not an error condition for the whole workspace.
    """
    from baseaicore import SuiteError

    from ideapress.services.diff import diff_context, unit_diff

    if unit_key is None:
        return None
    try:
        summary = unit_diff(runtime, project_id=project_id, unit_key=unit_key, old_version=compare)
    except SuiteError as exc:
        return {"unavailable": str(exc)}
    return {"unavailable": "", **diff_context(summary)}


def _coverage_summary(detail: dict[str, Any] | None) -> dict[str, Any]:
    """Counts for the coverage panel, including how many rest on a model's review.

    ADR-0039's labelling must survive every redesign: a reader has to be able to tell a mechanical
    guarantee from a model-attested one at a glance, on every surface coverage appears, and a
    count is what makes that visible before any row is read.
    """
    if not detail:
        return {"total": 0, "satisfied": 0, "model_guaranteed": 0, "unsatisfied": []}
    coverage = detail.get("coverage") or []
    model_guaranteed = [
        entry for entry in coverage if entry.get("satisfied_by") not in {"deterministic_check", ""}
    ]
    return {
        "total": len(coverage),
        "satisfied": sum(1 for entry in coverage if entry.get("satisfied")),
        "model_guaranteed": len(model_guaranteed),
        "model_guaranteed_keys": [entry.get("requirement_key") for entry in model_guaranteed],
        "unsatisfied": [
            entry.get("requirement_key") for entry in coverage if not entry.get("satisfied")
        ],
    }


def _backend_facts(runtime: Runtime) -> dict[str, Any]:
    """Which backend is configured, and whether work routed through it leaves the machine.

    Risk S4: the user is told plainly, per backend, where their content would go. The badge is on
    the workspace and not only on the backends page, because the workspace is where somebody is
    about to press a button that sends their draft somewhere.
    """
    backend = runtime.backend
    if backend is None:
        return {"mode": "none", "egress": False, "routes_internally": False, "detail": ""}
    capabilities = backend.capabilities()
    return {
        "mode": backend.name,
        "egress": runtime.settings.inference.mode != "ollama" and _is_remote(runtime),
        "routes_internally": capabilities.routes_internally,
        "structured_output": capabilities.structured_output,
        "detail": "",
    }


def _is_remote(runtime: Runtime) -> bool:
    """Whether the configured backend's endpoint is off this machine.

    Read from configuration rather than by contacting the backend: the workspace renders under a
    300 ms budget (spec §15) and must not wait on a network round trip to draw a badge.
    """
    inference = runtime.settings.inference
    url = {
        "ollama": inference.ollama.base_url,
        "loadcoach": inference.loadcoach.base_url,
        "openai_compatible": inference.openai_compatible.base_url,
    }.get(inference.mode, "")
    if not url:
        return False
    from urllib.parse import urlparse

    host = (urlparse(url).hostname or "").lower()
    return host not in {"127.0.0.1", "localhost", "::1", ""}
