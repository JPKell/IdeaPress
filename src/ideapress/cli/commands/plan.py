"""ideapress.cli.commands.plan — build and show a project's plan."""

from __future__ import annotations

import json as json_module
from typing import TYPE_CHECKING, Annotated

import typer

if TYPE_CHECKING:
    from collections.abc import Iterator

    from ideapress.services.runtime import Runtime

__all__ = ["app", "runtime_for"]

app = typer.Typer(no_args_is_help=True, help="Compile requirements and plan units.")


def runtime_for() -> Iterator[Runtime]:
    """Yield a runtime opened for this one command, closed on the way out."""
    from ideapress.config import load_settings
    from ideapress.services.runtime import build_runtime

    runtime = build_runtime(load_settings().settings)
    try:
        yield runtime
    finally:
        runtime.close()


def _wait(runtime: Runtime, task_id: str) -> str:
    """Block until a stage finishes, printing its events as they are persisted."""
    import time

    seen = 0
    while True:
        source = runtime.events.source(runtime.storage, task_id)
        for record in source.records(after=seen):
            seen = record.sequence
            if record.event_type != "token":
                typer.echo(f"  [{record.sequence:>3}] {record.event_type}: {record.message}")
        if runtime.runner.is_finished(task_id):
            break
        time.sleep(0.1)
    return runtime.runner.run_state(task_id) or "unknown"


@app.command(name="build")
def build(
    project_id: Annotated[str, typer.Argument()],
) -> None:
    """Compile requirements and build the unit plan. Mode: local.

    Exits 1 when a gate refuses the plan — which names every blocking requirement left unassigned,
    because a gate that fails without saying which requirement failed is one nobody can act on.
    """
    from ideapress.services.stage_bodies import start_plan

    for runtime in runtime_for():
        task = start_plan(runtime, project_id=project_id)
        typer.echo(f"task {task.run_id}")
        state = _wait(runtime, task.run_id)
        if state != "completed":
            typer.secho(f"Plan stage {state}.", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)
        typer.secho("Plan built.", fg=typer.colors.GREEN)


@app.command(name="show")
def show(
    project_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the compiled requirements and the unit plan. Mode: local.

    Every requirement is printed with the quotation that supports it: the claim and its evidence
    belong side by side, in the terminal as much as in the UI.
    """
    from ideapress.services.stage_reports import plan_report

    for runtime in runtime_for():
        report = plan_report(runtime, project_id=project_id)
        if json_output:
            payload = {
                "requirements": report["requirements"],
                "units": report["units"],
            }
            typer.echo(json_module.dumps(payload, indent=2, sort_keys=True, default=str))
            return
        typer.echo("REQUIREMENTS")
        if not report["requirements"]:
            typer.echo("  (none compiled)")
        for requirement in report["requirements"]:
            marker = "BLOCKING" if requirement["blocking"] else "advisory"
            typer.echo(f"  {requirement['key']} [{marker}] {requirement['text']}")
            typer.echo(f"      checks: {requirement['checks']}")
            typer.echo(f'      source: {requirement["source"]} — "{requirement["quote"]}"')
            typer.echo(f"      units:  {', '.join(requirement['units']) or '—'}")
        typer.echo("\nUNITS")
        if not report["units"]:
            typer.echo("  (no plan)")
        for unit in report["units"]:
            typer.echo(f"  {unit['key']}  {unit['state']:<10} {unit['title']}")
            typer.echo(f"      goal:         {unit['goal']}")
            typer.echo(f"      requirements: {unit['requirements'] or '—'}")
