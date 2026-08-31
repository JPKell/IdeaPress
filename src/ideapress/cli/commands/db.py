"""ideapress.cli.commands.db — upgrade, status, backup and restore.

Every operation goes through WeightsDB; IdeaPress writes no migration runner, no backup routine
and no integrity check of its own.
"""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.database import Database

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Database migration and maintenance.")


def _database() -> Iterator[Database]:
    """Yield a handle opened for this one command, disposed on the way out."""
    from ideapress.config import load_settings
    from ideapress.services.database import Database

    settings = load_settings().settings
    assert settings.storage.database_url is not None  # noqa: S101 — Settings always fills this in
    with Database.from_url(
        settings.storage.database_url, statement_timeout_ms=settings.storage.statement_timeout_ms
    ) as database:
        yield database


@app.command(name="upgrade")
def upgrade_command() -> None:
    """Run every pending migration. Mode: local.

    Takes an automatic backup first on SQLite; on PostgreSQL the backup is the operator's, which
    is why `auto_migrate` defaults off there.
    """
    from ideapress.services.database import upgrade

    for database in _database():
        outcome = upgrade(database)
        if outcome.from_revision == outcome.to_revision:
            typer.echo(f"Already at {outcome.to_revision}.")
            return
        typer.echo(f"Upgraded {outcome.from_revision or '(empty)'} -> {outcome.to_revision}.")
        if outcome.backup_path:
            typer.echo(f"Backup: {outcome.backup_path}")


@app.command(name="status")
def status_command(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report the schema revision and the database size. Mode: local."""
    from ideapress.services.database import get_status

    for database in _database():
        status = get_status(database)
        if json_output:
            typer.echo(
                json_module.dumps(
                    {
                        "dialect": status.dialect,
                        "current_revision": status.current_revision,
                        "head_revision": status.head_revision,
                        "at_head": status.at_head,
                        "size_bytes": status.size_bytes,
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return
        typer.echo(f"dialect  {status.dialect}")
        typer.echo(f"current  {status.current_revision or '(empty)'}")
        typer.echo(f"head     {status.head_revision}")
        typer.echo(f"at head  {status.at_head}")
        typer.echo(f"size     {status.size_bytes} bytes")


@app.command(name="backup")
def backup_command(
    output: Annotated[str | None, typer.Option("--output", help="Destination directory.")] = None,
    keep: Annotated[int, typer.Option("--keep", help="How many backups to retain.")] = 5,
) -> None:
    """Write a consistent backup. Mode: local."""
    from pathlib import Path

    from weightsdb import backup

    from ideapress.config import data_dir

    for database in _database():
        destination = Path(output) if output else data_dir() / "backups"
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = backup(database.engine, destination, keep=keep, prefix="ideapress")
        typer.echo(f"Wrote {result.path} ({result.size_bytes} bytes).")


@app.command(name="restore")
def restore_command(
    source: Annotated[str, typer.Argument(help="Backup file to restore from.")],
    yes: Annotated[bool, typer.Option("--yes", help="Skip the confirmation prompt.")] = False,
) -> None:
    """Replace the current database with a backup. Mode: local.

    Refuses without confirmation: this overwrites the user's work with an older copy.
    """
    from pathlib import Path

    from weightsdb import restore

    for database in _database():
        path = Path(source)
        if not path.is_file():
            typer.secho(f"{path} does not exist.", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
        typer.echo(f"This will replace the current database with {path}.")
        if not yes and not typer.confirm("Restore it?"):
            typer.echo("Nothing was changed.")
            raise typer.Exit(0)
        result = restore(database.engine, path, confirm=True)
        typer.echo(f"Restored from {result.source}.")
