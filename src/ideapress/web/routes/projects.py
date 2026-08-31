"""ideapress.web.routes.projects — the project API and the project pages.

Route handlers contain no business logic: each calls one service method and renders. Model-produced
and user-supplied text reaches the templates as plain strings and is escaped once by the shared
macros — never ``| safe`` (risk S1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, Form, Query, Request, status
from mirrorwall import json_response, paginated_response
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import RedirectResponse

from ideapress.web.csrf import render_form_page

if TYPE_CHECKING:
    from starlette.responses import HTMLResponse, JSONResponse, Response

    from ideapress.domain.project import Project
    from ideapress.services.projects import ProjectService

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["projects"])
ui_router = APIRouter(include_in_schema=False)


class CreateProjectRequest(BaseModel):
    """``POST /projects`` body."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    brief: str = ""
    content_type: str = "article"
    workflow_id: str = "standard"
    author_material: dict[str, Any] = Field(default_factory=dict)


class UpdateProjectRequest(BaseModel):
    """``PUT /projects/{id}`` body. Every field optional; omitted fields are untouched."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=200)
    brief: str | None = None
    author_material: dict[str, Any] | None = None
    status: str | None = None


def _service(request: Request) -> ProjectService:
    """Return the project service this process built, from the runtime the lifespan opened."""
    return request.app.state.runtime.projects  # type: ignore[no-any-return]  # set by build_runtime


def _as_payload(project: Project) -> dict[str, Any]:
    """Render a project as the API's JSON shape."""
    return {
        "id": project.id,
        "title": project.title,
        "slug": project.slug,
        "content_type": project.content_type,
        "content_type_version": project.content_type_version,
        "workflow_id": project.workflow_id,
        "workflow_version": project.workflow_version,
        "status": project.status,
        "brief": project.brief_text,
        "author_material": project.author_material,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "completed_at": project.completed_at.isoformat() if project.completed_at else None,
        "archived_at": project.archived_at.isoformat() if project.archived_at else None,
    }


@router.post("/projects", status_code=status.HTTP_201_CREATED)
def create_project(request: Request, body: CreateProjectRequest) -> JSONResponse:
    """Create a project. The slug is derived from the title, never supplied by the caller."""
    project = _service(request).create(
        title=body.title,
        brief=body.brief,
        content_type=body.content_type,
        workflow_id=body.workflow_id,
        author_material=body.author_material,
    )
    return json_response(_as_payload(project), status=status.HTTP_201_CREATED)


@router.get("/projects")
def list_projects(
    request: Request,
    project_status: Annotated[str | None, Query(alias="status")] = None,
    content_type: Annotated[str | None, Query()] = None,
    include_archived: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> JSONResponse:
    """List projects, most recently updated first. Archived projects are hidden by default."""
    projects = _service(request).list(
        status=project_status,
        content_type=content_type,
        include_archived=include_archived,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(projects) > limit
    page = [_as_payload(project) for project in projects[:limit]]
    return paginated_response(page, limit=limit, has_more=has_more)


@router.get("/projects/{project_id}")
def get_project(request: Request, project_id: str) -> JSONResponse:
    """Return one project.

    Raises:
        ProjectNotFound: Rendered as 404 by the shared handler.
    """
    return json_response(_as_payload(_service(request).get(project_id)))


@router.put("/projects/{project_id}")
def update_project(request: Request, project_id: str, body: UpdateProjectRequest) -> JSONResponse:
    """Update the brief, author material, title or status. Never recompiles requirements."""
    project = _service(request).update(
        project_id,
        title=body.title,
        brief=body.brief,
        author_material=body.author_material,
        status=body.status,
    )
    return json_response(_as_payload(project))


@router.delete("/projects/{project_id}")
def delete_project(
    request: Request, project_id: str, confirm: Annotated[bool, Query()] = False
) -> JSONResponse:
    """Preview or perform a delete.

    Without ``?confirm=true`` this removes nothing and returns what *would* be removed. That is
    api.md §2's preview-then-confirm, and it is the difference between a mis-click and the loss of
    the user's own writing.
    """
    preview = _service(request).delete(project_id, confirm=confirm)
    return json_response(
        {
            "deleted": confirm,
            "project": _as_payload(preview.project),
            "source_count": preview.source_count,
            "directory": str(preview.directory) if preview.directory else None,
            "directory_bytes": preview.directory_bytes,
        }
    )


@ui_router.get("/")
def projects_page(
    request: Request, show_archived: Annotated[bool, Query()] = False
) -> HTMLResponse:
    """Render the project list."""
    projects = _service(request).list(include_archived=show_archived)
    return render_form_page(
        request,
        "projects/index.html",
        page="projects",
        page_title="Projects",
        projects=projects,
        show_archived=show_archived,
    )


@ui_router.get("/projects/{project_id}")
def project_page(request: Request, project_id: str) -> HTMLResponse:
    """Render one project's detail shell."""
    project = _service(request).get(project_id)
    return render_form_page(
        request,
        "projects/detail.html",
        page="projects",
        page_title=project.title,
        project=project,
    )


@ui_router.post("/projects")
def create_project_form(
    request: Request,
    title: Annotated[str, Form()],
    brief: Annotated[str, Form()] = "",
    content_type: Annotated[str, Form()] = "article",
) -> Response:
    """Create a project from the list page's form, then redirect to it."""
    project = _service(request).create(title=title, brief=brief, content_type=content_type)
    return RedirectResponse(f"/projects/{project.id}", status_code=status.HTTP_303_SEE_OTHER)
