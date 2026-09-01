"""ideapress.cli.commands.config — show, validate, init and path.

``config show`` reports the effective value of every leaf *and the layer that produced it*, which
is the whole reason :mod:`ideapress.config` merges the layers itself rather than delegating to
pydantic-settings' source priority.
"""

from __future__ import annotations

import json as json_module
from typing import Annotated, Any

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Configuration inspection and management.")


def _flatten(payload: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    """Flatten a nested settings dump into dotted ``path, value`` pairs."""
    rows: list[tuple[str, Any]] = []
    for key, value in payload.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            rows.extend(_flatten(value, f"{path}."))
        else:
            rows.append((path, value))
    return rows


@app.command(name="show")
def show(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    config: Annotated[str | None, typer.Option("--config")] = None,
) -> None:
    """Print the effective configuration and where each value came from. Mode: local.

    An invalid configuration exits 2 with the refusal's own message — the same handling as
    ``config validate`` and ``serve``, because a person who mistyped a key is owed the key's
    name, not a traceback (M7 finding 4).
    """
    from ideapress.config import ConfigurationError, load_settings

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.secho(exc.message, err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    dumped = loaded.settings.model_dump(mode="json")
    if json_output:
        typer.echo(
            json_module.dumps(
                {
                    "config_path": str(loaded.config_path),
                    "config_file_used": loaded.config_file_used,
                    "settings": dumped,
                    "sources": loaded.sources,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    typer.echo(f"# {loaded.config_path} ({'read' if loaded.config_file_used else 'not present'})")
    for path, value in _flatten(dumped):
        source = loaded.sources.get(path, "default")
        typer.echo(f"{path:<48} {value!r:<40} [{source}]")


@app.command(name="validate")
def validate(
    config: Annotated[str | None, typer.Option("--config")] = None,
) -> None:
    """Validate configuration without starting anything. Mode: local.

    Exits 0 when the configuration loads and every refusal passes, 2 when it does not — naming the
    key and, for an unrecognized key, the nearest name it recognizes.
    """
    from baseaicore import ConfigurationError

    from ideapress.config import load_settings

    try:
        loaded = load_settings(config_path=config)
    except ConfigurationError as exc:
        typer.secho(exc.message, err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc
    typer.secho(f"Configuration valid ({loaded.config_path}).", fg=typer.colors.GREEN)


@app.command(name="init")
def init(
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented example configuration file. Mode: local.

    Refuses to overwrite an existing file unless ``--force`` is given: the file holds the user's
    own settings and this command must never be the reason they are lost.
    """
    from ideapress.config import EXAMPLE_CONFIG_TOML, resolve_config_path

    path = resolve_config_path()
    if path.exists() and not force:
        typer.secho(
            f"{path} already exists. Pass --force to overwrite it.", err=True, fg=typer.colors.RED
        )
        raise typer.Exit(1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG_TOML, encoding="utf-8")
    typer.echo(f"Wrote {path}")


@app.command(name="path")
def path_command() -> None:
    """Print the configuration, data and state directories in use. Mode: local."""
    from ideapress.config import config_dir, data_dir, resolve_config_path, state_dir

    typer.echo(f"config file  {resolve_config_path()}")
    typer.echo(f"config dir   {config_dir()}")
    typer.echo(f"data dir     {data_dir()}")
    typer.echo(f"state dir    {state_dir()}")
