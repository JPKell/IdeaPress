"""ideapress.cli.main — the Typer root app.

Registers the top-level ``serve``/``health``/``version``/``doctor`` commands and the subgroups.
Only ``typer`` and the lightweight command modules load at import time; every heavier dependency
stays behind a lazy import inside a command body (CLI standards §12), so ``--help`` is fast and
imports neither the web layer nor a database driver.
"""

from __future__ import annotations

from typing import Annotated

import typer

from ideapress.cli.commands import backend as backend_commands
from ideapress.cli.commands import config as config_commands
from ideapress.cli.commands import db as db_commands
from ideapress.cli.commands import project as project_commands
from ideapress.cli.commands import system as system_commands

__all__ = ["app"]

app = typer.Typer(
    name="ideapress",
    help="Turn an idea into finished content through workflows Python controls.",
    no_args_is_help=False,
    add_completion=True,
)


def _eager_version(show: bool) -> None:
    if not show:
        return
    system_commands.print_version(json_output=False)
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version", is_eager=True, callback=_eager_version, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """ideapress — turns an idea into finished content, with Python owning the control flow."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(system_commands.serve)


app.command(name="serve", help="Start the web server (also the default with no subcommand).")(
    system_commands.serve
)
app.command(name="health", help="Report component health.")(system_commands.health)
app.command(name="version", help="Print the application and API versions.")(system_commands.version)
app.command(name="doctor", help="Diagnose a broken installation.")(system_commands.doctor)

app.add_typer(config_commands.app, name="config")
app.add_typer(db_commands.app, name="db")
app.add_typer(project_commands.app, name="project")
app.add_typer(backend_commands.app, name="backend")
