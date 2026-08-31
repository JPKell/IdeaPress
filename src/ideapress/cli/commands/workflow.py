"""ideapress.cli.commands.workflow — list and inspect workflow definitions."""

from __future__ import annotations

import json as json_module
from typing import Annotated, Any

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Inspect workflow definitions and their gates.")


def _definition() -> dict[str, Any]:
    from ideapress.domain.stages import STAGES

    return {
        "id": "standard",
        "version": "1.0",
        "stages": [
            {
                "stage": d.stage,
                "ordinal": d.ordinal,
                "uses_model": d.uses_model,
                "gate": d.gate,
            }
            for d in STAGES.values()
        ],
    }


@app.command(name="list")
def list_workflows(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List the workflows this build ships. Mode: local."""
    definitions = [_definition()]
    if json_output:
        typer.echo(json_module.dumps(definitions, indent=2))
        return
    for definition in definitions:
        typer.echo(
            f"{definition['id']} {definition['version']}  {len(definition['stages'])} stages"
        )


@app.command(name="show")
def show(
    workflow_id: Annotated[str, typer.Argument()] = "standard",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show a workflow's stage order and the gate on each. Mode: local.

    Exits 1 for a workflow this build does not ship, rather than showing the default one.
    """
    if workflow_id != "standard":
        typer.secho(
            f"{workflow_id!r} is not a workflow. This build ships: standard.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(1)
    definition = _definition()
    if json_output:
        typer.echo(json_module.dumps(definition, indent=2))
        return
    typer.echo(f"{definition['id']} {definition['version']}")
    for stage in definition["stages"]:
        marker = "model" if stage["uses_model"] else " --  "
        typer.echo(f"  {stage['ordinal']:>2}. {stage['stage']:<20} {marker}  {stage['gate']}")
