"""ideapress.web.routes.workspace — the page a person actually works in.

P8's goal in one sentence: *a workspace where a person reads, judges and directs, rather than
watching logs*. That means one page carrying the unit navigator, the unit's content, its findings,
its requirement coverage and the actions available on it — so the three questions a person asks
between stages (what does it say, what is wrong with it, what is it still missing) are answered
without leaving the page.

Everything here is server-rendered and works with JavaScript disabled (ADR-0020). The navigator is
links, the actions are forms, and the live view degrades to a "reload to see progress" notice. The
progressive enhancement is that `workspace.js` follows the SSE stream and updates in place; nothing
depends on it.

Route handlers contain no business logic: each calls one service function and renders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Form, Query, Request, status
from mirrorwall import json_response

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import JSONResponse, RedirectResponse, Response

from ideapress.web.csrf import render_form_page

if TYPE_CHECKING:
    from starlette.responses import HTMLResponse

    from ideapress.services.runtime import Runtime

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["workspace"])
ui_router = APIRouter(include_in_schema=False)


def _runtime(request: Request) -> Runtime:
    runtime: Runtime = request.app.state.runtime
    return runtime


@router.get("/projects/{project_id}/workspace")
def get_workspace(
    request: Request,
    project_id: str,
    unit: Annotated[str, Query()] = "",
    compare: Annotated[int | None, Query()] = None,
) -> JSONResponse:
    """The workspace page's own view of one unit, over the API (row WP5).

    Assembled by the same ``workspace_view`` the page renders: the navigator, the unit's content
    and provenance, its pause reason with IdeaPress's remedy, its coverage summary, the diff
    against ``compare`` when asked, the backend's recorded egress facts, the project's cost, the
    running stage and where research may fetch.

    Args:
        request: The request.
        project_id: The project.
        unit: Which unit; the first one when empty or unknown.
        compare: A version to diff the current one against.

    Raises:
        ProjectNotFound: No such project.
    """
    from ideapress.services.workspace import workspace_view

    view = workspace_view(
        _runtime(request), project_id=project_id, unit_key=unit or None, compare_version=compare
    )
    return json_response(view)


@ui_router.get("/projects/{project_id}/workspace")
def workspace_page(
    request: Request,
    project_id: str,
    unit: Annotated[str, Query()] = "",
    compare: Annotated[int | None, Query()] = None,
) -> HTMLResponse:
    """Render the workspace, focused on one unit.

    Args:
        request: The request.
        project_id: The project.
        unit: Which unit to show; the first one when empty.
        compare: A version to diff the selected version against, when the reader asked for one.

    Returns:
        The workspace page.

    Raises:
        ProjectNotFound: No such project.
    """
    from ideapress.services.workspace import workspace_view

    view = workspace_view(
        _runtime(request), project_id=project_id, unit_key=unit or None, compare_version=compare
    )
    return render_form_page(
        request,
        "workspace/index.html",
        page="projects",
        page_title=f"{view['project']['title']} — workspace",
        # htmx only while a stage runs (ADR-0128): the log pane's SSE region is the one swap.
        mirrorwall={"htmx": bool(view.get("running_task_id"))},
        **view,
    )


@ui_router.post("/projects/{project_id}/units/{unit_key}/resume")
def resume_unit_form(request: Request, project_id: str, unit_key: str) -> Response:
    """Resume one paused unit from the workspace, then return to it.

    The action sits next to the pause reason on purpose: a person reading *why* a unit stopped is
    the person who should be able to restart it, and making them find the CLI to do so is what M7's
    verification called out.
    """
    from ideapress.services.stage_bodies import start_stage

    start_stage(
        _runtime(request),
        project_id=project_id,
        stage="draft",
        units=[unit_key],
        resume=True,
    )
    return RedirectResponse(
        f"/projects/{project_id}/workspace?unit={unit_key}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@ui_router.post("/projects/{project_id}/research")
def research_form(request: Request, project_id: str) -> Response:
    """Start the ``research`` stage from the workspace, then return to it.

    The one stage that runs before a plan exists (workflows §2, ADR-0116), and the one whose click
    may reach the network: a fetch goes only to a host named in ``[research] allowed_hosts``, and
    the page states that list beside the button. CSRF-protected like every form here
    (ADR-0026 §2).

    Raises:
        ProjectNotFound: No such project.
        StageAlreadyRunning: A stage is already running for this project.
    """
    from ideapress.services.stage_bodies import start_stage

    start_stage(_runtime(request), project_id=project_id, stage="research")
    return RedirectResponse(
        f"/projects/{project_id}/workspace", status_code=status.HTTP_303_SEE_OTHER
    )


@ui_router.post("/projects/{project_id}/plan/edit")
def edit_plan_form(
    request: Request,
    project_id: str,
    operation: Annotated[str, Form()],
    unit_keys: Annotated[str, Form()] = "",
    requirement_keys: Annotated[str, Form()] = "",
    text: Annotated[str, Form()] = "",
    position: Annotated[int | None, Form()] = None,
) -> Response:
    """Apply one plan edit, or re-render the plan with the refusal.

    Args:
        request: The request.
        project_id: The project whose plan to edit.
        operation: ``reorder``, ``split``, ``merge``, ``reassign`` or ``goal``.
        unit_keys: Comma-separated unit keys the operation applies to.
        requirement_keys: Comma-separated requirement keys, for ``reassign`` and ``split``.
        text: The new goal or the new unit's title.
        position: The target position, for ``reorder``.

    Returns:
        A redirect back to the plan on success; the plan page carrying the refusal otherwise.

    An edit that would orphan a blocking requirement comes back as the page the person was already
    looking at, with the refusal naming the requirement — not as a raw error, and not as a silent
    no-op. The refusal is the product working, so it is rendered like one.
    """
    from baseaicore import SuiteError
    from baseaicore import ValidationError as SuiteValidationError

    from ideapress.services.plan_editing import PlanEdit, apply_edit
    from ideapress.services.stage_reports import plan_report

    edit = PlanEdit(
        operation=operation,
        unit_keys=_split(unit_keys),
        requirement_keys=_split(requirement_keys),
        text=text,
        position=position,
    )
    try:
        apply_edit(_runtime(request), project_id=project_id, edit=edit)
    except (SuiteValidationError, SuiteError) as exc:
        report: dict[str, Any] = plan_report(_runtime(request), project_id=project_id)
        return render_form_page(
            request,
            "plan/index.html",
            page="projects",
            page_title="Plan",
            edit_refusal=str(exc),
            edit_refusal_details=getattr(exc, "details", {}) or {},
            **report,
        )
    return RedirectResponse(f"/projects/{project_id}/plan", status_code=status.HTTP_303_SEE_OTHER)


def _split(value: str) -> tuple[str, ...]:
    """Split a comma-separated form field, dropping blanks.

    A blank entry would become a lookup for the unit named ``""``, which fails with a confusing
    message rather than being ignored as the person plainly intended.
    """
    return tuple(part.strip() for part in value.split(",") if part.strip())


@ui_router.get("/projects/{project_id}/export")
def export_dialog(
    request: Request, project_id: str, written: Annotated[str, Query()] = ""
) -> HTMLResponse:
    """Render the export dialog: the formats, what each includes, and where the file lands.

    "Stated plainly" is the plan's wording and the point. A person choosing an export format is
    deciding what leaves this application and in what shape, so the page says what each format
    contains rather than offering three unexplained buttons.
    """
    from ideapress.services.export import FORMAT_DESCRIPTIONS, FORMATS
    from ideapress.services.unit_reports import unit_list

    units = unit_list(_runtime(request), project_id=project_id)
    project = _runtime(request).projects.get(project_id)
    committed = [unit for unit in units if unit["state"] == "committed"]
    return render_form_page(
        request,
        "export/index.html",
        page="projects",
        page_title=f"{project.title} — export",
        project={"id": project.id, "title": project.title},
        formats=[
            {
                "format": name,
                "extension": extension,
                "describes": FORMAT_DESCRIPTIONS.get(name, ""),
            }
            for name, extension in sorted(FORMATS.items())
        ],
        unit_count=len(units),
        committed_count=len(committed),
        uncommitted=[unit["unit_key"] for unit in units if unit["state"] != "committed"],
        written=written,
    )


@ui_router.post("/projects/{project_id}/export")
def export_form(
    request: Request,
    project_id: str,
    fmt: Annotated[str, Form(alias="format")] = "markdown",
) -> Response:
    """Write the export and return to the dialog with the artifact named.

    Raises:
        ExportFailed: The format is not one of the three shipped at 1.0.
    """
    from ideapress.services.export import export_project

    artifact = export_project(_runtime(request), project_id=project_id, fmt=fmt)
    return RedirectResponse(
        f"/projects/{project_id}/export?written={artifact.get('path', '')}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
