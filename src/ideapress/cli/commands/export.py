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
    allow_partial: Annotated[
        bool,
        typer.Option(
            "--allow-partial",
            help="Export a plan that is not fully committed. The file says so, and lists the "
            "requirements nothing answers.",
        ),
    ] = False,
) -> None:
    """Export a committed project. Mode: local.

    Exports are byte-identical for the same committed project (spec §11 contract 4), so writing
    twice and comparing hashes is a check anyone can run.
    """
    from baseaicore import SuiteError

    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.export import (
        build_document,
        export_project,
        refuse_partial_export,
    )
    from ideapress.services.export import render as render_export

    for runtime in runtime_for():
        try:
            if stdout:
                document = build_document(runtime, project_id=project_id)
                # `--stdout` renders without writing, but it is still an export a person keeps —
                # the usual use is `> file`. It refuses on the same terms as the written path, or
                # a partial export would be one redirect away from the check.
                refuse_partial_export(document, allow_partial=allow_partial)
                typer.echo(render_export(document, fmt), nl=False)
                return
            written = export_project(
                runtime, project_id=project_id, fmt=fmt, allow_partial=allow_partial
            )
        except SuiteError as exc:
            # A refusal a person can read and act on, not a traceback. The message already names
            # the remedy; a stack trace buries it.
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
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
