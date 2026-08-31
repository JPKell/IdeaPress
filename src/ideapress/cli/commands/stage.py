"""ideapress.cli.commands.stage — run, list, inspect and cancel stages."""

from __future__ import annotations

import json as json_module
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Run, inspect and cancel workflow stages.")


@app.command(name="run")
def run(
    project_id: Annotated[str, typer.Argument()],
    stage: Annotated[str, typer.Argument(help="A stage identifier from workflows §2.")],
    units: Annotated[
        str | None, typer.Option("--units", help="Comma-separated unit keys; all when omitted.")
    ] = None,
    resume: Annotated[
        bool, typer.Option("--resume", help="Continue from the first incomplete unit.")
    ] = False,
) -> None:
    """Run one stage over the project's units. Mode: local."""
    from ideapress.cli.commands.plan import _wait, runtime_for
    from ideapress.services.stage_bodies import start_stage

    selected = [key.strip() for key in units.split(",")] if units else None
    for runtime in runtime_for():
        task = start_stage(
            runtime,
            project_id=project_id,
            stage=stage,  # type: ignore[arg-type]  # validated by start_stage against the registry
            units=selected,
            resume=resume,
        )
        typer.echo(f"task {task.run_id}")
        state = _wait(runtime, task.run_id)
        if state != "completed":
            typer.secho(f"Stage {state}.", err=True, fg=typer.colors.RED)
            raise typer.Exit(1)


@app.command(name="list")
def list_stages(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the stages, their order, their gates and which use a model. Mode: local."""
    from ideapress.domain.stages import STAGES

    rows = [
        {
            "stage": definition.stage,
            "ordinal": definition.ordinal,
            "uses_model": definition.uses_model,
            "gate": definition.gate,
        }
        for definition in STAGES.values()
    ]
    if json_output:
        typer.echo(json_module.dumps(rows, indent=2))
        return
    for row in rows:
        marker = "model" if row["uses_model"] else " --  "
        typer.echo(f"{row['ordinal']:>2}. {row['stage']:<20} {marker}  {row['gate']}")


@app.command(name="status")
def status(
    project_id: Annotated[str, typer.Argument()],
    task_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Report a stage task's state, progress and attempts. Mode: local."""
    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.stage_reports import task_report

    for runtime in runtime_for():
        report = task_report(runtime, project_id=project_id, run_id=task_id, stage=None)
        if json_output:
            typer.echo(json_module.dumps(report, indent=2, sort_keys=True, default=str))
            return
        typer.echo(f"task     {report['task_id']}")
        typer.echo(f"stage    {report['stage']}")
        typer.echo(f"state    {report['state']}")
        typer.echo(f"units    {report['units_completed']}/{report['units_total']}")
        if report["error_code"]:
            typer.echo(f"error    {report['error_code']}: {report['error_text']}")
        for attempt in report["attempts"]:
            typer.echo(
                f"  {attempt['stage']:<18} {attempt['unit_key'] or '—':<8} "
                f"{attempt['outcome']:<12} {attempt['model_canonical_id'] or ''}"
            )


@app.command(name="cancel")
def cancel(
    project_id: Annotated[str, typer.Argument()],
    task_id: Annotated[str, typer.Argument()],
) -> None:
    """Cancel a running stage. Mode: local.

    Honoured at the next model-call boundary. Cancelling a finished task is not an error — it is a
    race the user cannot avoid.
    """
    from ideapress.cli.commands.plan import runtime_for

    for runtime in runtime_for():
        cancelled = runtime.runner.cancel(task_id)
        typer.echo("Cancelling." if cancelled else "That task is not running here.")
