"""ideapress.cli.commands.export — render a committed project to a file or to stdout."""

from __future__ import annotations

from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Export a committed project.")


@app.command(name="run")
def run(
    project_id: Annotated[str, typer.Argument()],
    fmt: Annotated[str, typer.Option("--format", help="markdown | html | json")] = "markdown",
    stdout: Annotated[
        bool, typer.Option("--stdout", help="Write to stdout instead of the project directory.")
    ] = False,
) -> None:
    """Export a committed project. Mode: local.

    Exports are byte-identical for the same committed project (spec §11 contract 4), so writing
    twice and comparing hashes is a check anyone can run.
    """
    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.export import build_document, export_project
    from ideapress.services.export import render as render_export

    for runtime in runtime_for():
        if stdout:
            document = build_document(runtime, project_id=project_id)
            typer.echo(render_export(document, fmt), nl=False)
            return
        written = export_project(runtime, project_id=project_id, fmt=fmt)
        typer.echo(f"{written['path']}")
        typer.echo(f"  sha256 {written['sha256']}")
        typer.echo(f"  {written['size_bytes']} bytes, {written['units']} unit(s)")
        typer.echo(f"  export format version {written['export_format_version']}")


@app.command(name="formats")
def formats() -> None:
    """List the export formats this build ships. Mode: local."""
    from ideapress.services.export import FORMATS

    for name, extension in sorted(FORMATS.items()):
        typer.echo(f"{name:<10} .{extension}")
