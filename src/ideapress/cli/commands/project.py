"""ideapress.cli.commands.project — create, list, show and delete projects.

``delete`` shows exactly what it would remove and asks, which is api.md §2's preview-then-confirm
in the shape a terminal has. ``--yes`` skips the question; nothing skips the preview.
"""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated, Any

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.domain.project import Project
    from ideapress.services.projects import ProjectService

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Create, list, open and delete projects.")


def _services() -> Iterator[ProjectService]:
    """Yield a project service over a database opened for this one command."""
    from pathlib import Path

    from ideapress.config import load_settings
    from ideapress.services.database import Database, ensure_ready
    from ideapress.services.projects import ProjectService

    settings = load_settings().settings
    assert settings.storage.database_url is not None  # noqa: S101 — Settings always fills this in
    assert settings.storage.project_dir is not None  # noqa: S101 — likewise
    with Database.from_url(
        settings.storage.database_url, statement_timeout_ms=settings.storage.statement_timeout_ms
    ) as database:
        ensure_ready(database, auto_migrate=settings.storage.auto_migrate)
        yield ProjectService(database, project_dir=Path(settings.storage.project_dir))


def _payload(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "title": project.title,
        "slug": project.slug,
        "content_type": project.content_type,
        "status": project.status,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


@app.command(name="create")
def create(
    title: Annotated[str, typer.Argument(help="Human title. The slug is derived, never taken.")],
    brief: Annotated[str, typer.Option("--brief", help="The brief text.")] = "",
    brief_file: Annotated[
        str | None, typer.Option("--brief-file", help="Read the brief from a file.")
    ] = None,
    content_type: Annotated[str, typer.Option("--content-type")] = "article",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Create a project. Mode: local."""
    from pathlib import Path

    text = Path(brief_file).read_text(encoding="utf-8") if brief_file else brief
    for service in _services():
        project = service.create(title=title, brief=text, content_type=content_type)
        if json_output:
            typer.echo(json_module.dumps(_payload(project), indent=2, sort_keys=True))
        else:
            typer.echo(f"{project.id}  {project.slug}  {project.title}")


@app.command(name="list")
def list_projects(
    include_archived: Annotated[bool, typer.Option("--archived")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List projects, most recently updated first. Mode: local."""
    for service in _services():
        projects = service.list(include_archived=include_archived)
        if json_output:
            typer.echo(json_module.dumps([_payload(p) for p in projects], indent=2, sort_keys=True))
            return
        if not projects:
            typer.echo("No projects yet. `ideapress project create <title>` starts one.")
            return
        for project in projects:
            typer.echo(f"{project.id}  {project.status:<11} {project.title}")


@app.command(name="show")
def show(
    project_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one project. Mode: local."""
    from ideapress.errors import ProjectNotFound

    for service in _services():
        try:
            project = service.get(project_id)
        except ProjectNotFound as exc:
            typer.secho(exc.message, err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        if json_output:
            typer.echo(json_module.dumps(_payload(project), indent=2, sort_keys=True))
            return
        typer.echo(f"title        {project.title}")
        typer.echo(f"id           {project.id}")
        typer.echo(f"slug         {project.slug}")
        typer.echo(f"status       {project.status}")
        typer.echo(f"content type {project.content_type} {project.content_type_version}")
        typer.echo(f"workflow     {project.workflow_id} {project.workflow_version}")
        typer.echo(f"created      {project.created_at.isoformat()}")
        if project.brief_text:
            typer.echo("\nbrief:")
            typer.echo(project.brief_text)


@app.command(name="archive")
def archive(project_id: Annotated[str, typer.Argument()]) -> None:
    """Archive a project: hidden from the list, nothing removed, reversible. Mode: local."""
    from ideapress.errors import ProjectNotFound

    for service in _services():
        try:
            project = service.archive(project_id)
        except ProjectNotFound as exc:
            typer.secho(exc.message, err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        typer.echo(f"Archived {project.title}. `ideapress project list --archived` still shows it.")


@app.command(name="delete")
def delete(
    project_id: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Delete a project, its rows and its artifact directory. Mode: local.

    Always prints what will be removed first. Without ``--yes`` it asks; answering anything but
    yes removes nothing. This is irreversible and the thing being destroyed is the user's writing.
    """
    from ideapress.errors import ProjectNotFound

    for service in _services():
        try:
            preview = service.preview_delete(project_id)
        except ProjectNotFound as exc:
            typer.secho(exc.message, err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        typer.echo(f"This will permanently remove:\n  project   {preview.project.title}")
        typer.echo(f"  sources   {preview.source_count}")
        if preview.directory is not None:
            typer.echo(f"  directory {preview.directory} ({preview.directory_bytes} bytes)")
        if not yes and not typer.confirm("Delete it?"):
            typer.echo("Nothing was removed.")
            raise typer.Exit(0)
        service.delete(project_id, confirm=True)
        typer.echo(f"Deleted {preview.project.title}.")


@app.command(name="export")
def export_archive(
    project_id: Annotated[str, typer.Argument(help="The project to export.")],
    destination: Annotated[
        str, typer.Option("--to", help="File to write, or a directory to write into.")
    ] = ".",
) -> None:
    """Write a project's whole record to a portable archive (spec §7.2).

    The archive carries the brief, the compiled requirements, the plan, every committed version and
    the provenance of every attempt — everything needed to open the project on another machine, or
    to keep as a backup that outlives this installation.
    """
    from pathlib import Path

    from ideapress.config import load_settings
    from ideapress.services.project_archive import export_project_archive
    from ideapress.services.runtime import build_runtime

    runtime = build_runtime(load_settings().settings)
    try:
        written = export_project_archive(
            runtime, project_id=project_id, destination=Path(destination)
        )
    finally:
        runtime.close()
    typer.echo(f"Wrote {written}")


@app.command(name="import")
def import_archive(
    path: Annotated[str, typer.Argument(help="The archive to read.")],
    title: Annotated[
        str, typer.Option("--title", help="Use this title instead of the archive's.")
    ] = "",
    inspect_only: Annotated[
        bool,
        typer.Option(
            "--inspect", help="Report what the archive contains and stop, writing nothing."
        ),
    ] = False,
) -> None:
    """Create a project from an archive, after checking every byte of its structure.

    Nothing is written until the archive has passed every check: containment, symlinks, entry
    counts, sizes and the compression ratio. A refused archive leaves no directory and no row —
    and `--inspect` reports what it found without importing at all, which is the safe thing to run
    first on an archive somebody sent you.
    """
    from pathlib import Path

    from baseaicore import SuiteError

    from ideapress.config import load_settings
    from ideapress.services.project_archive import (
        describe_report,
        import_project_archive,
        inspect_archive,
    )
    from ideapress.services.runtime import build_runtime

    archive_path = Path(path)
    try:
        report = inspect_archive(archive_path)
    except SuiteError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2) from exc

    for line in describe_report(report):
        typer.echo(line)
    if inspect_only:
        raise typer.Exit(code=0 if report.safe else 1)
    if not report.safe:
        typer.echo("Refused. Nothing was written.", err=True)
        raise typer.Exit(code=1)

    runtime = build_runtime(load_settings().settings)
    try:
        result = import_project_archive(runtime, path=archive_path, title=title or None)
    except SuiteError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        runtime.close()
    typer.echo(f"Imported {result['title']} as {result['project_id']} ({result['units']} unit(s))")
