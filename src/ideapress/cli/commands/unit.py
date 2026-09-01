"""ideapress.cli.commands.unit — list, show and inspect the history of units."""

from __future__ import annotations

import json as json_module
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Inspect units, their coverage and their provenance.")


@app.command(name="list")
def list_units(
    project_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List a project's units with state and coverage. Mode: local."""
    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.unit_reports import unit_list

    for runtime in runtime_for():
        units = unit_list(runtime, project_id=project_id)
        if json_output:
            typer.echo(json_module.dumps(units, indent=2, sort_keys=True, default=str))
            return
        if not units:
            typer.echo("No units. Build a plan first.")
            return
        for unit in units:
            version = f"v{unit['version']}" if unit["version"] else "—"
            typer.echo(f"{unit['unit_key']}  {unit['state']:<10} {version:<5} {unit['title']}")
            if unit["paused_reason"]:
                typer.echo(f"      paused: {unit['paused_reason']}")


@app.command(name="show")
def show(
    project_id: Annotated[str, typer.Argument()],
    unit_key: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
    provenance: Annotated[
        bool, typer.Option("--provenance", help="Print the full provenance record.")
    ] = False,
) -> None:
    """Show a unit's content, coverage and provenance. Mode: local."""
    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.unit_reports import unit_detail

    for runtime in runtime_for():
        detail = unit_detail(runtime, project_id=project_id, unit_key=unit_key)
        if json_output:
            typer.echo(json_module.dumps(detail, indent=2, sort_keys=True, default=str))
            return
        typer.echo(f"{detail['unit_key']}  {detail['title']}")
        typer.echo(f"state    {detail['state']}")
        typer.echo(f"version  {detail['version'] or '(none)'}")
        typer.echo(f"words    {detail['word_count'] or '—'}")
        typer.echo(f"hash     {detail['content_hash'] or '—'}")
        if detail["paused_reason"]:
            typer.echo(f"paused   {detail['paused_reason']}")
        if not provenance:
            typer.echo("\ncontent:\n")
            typer.echo(detail["content"] or "(none)")
            return
        typer.echo("\nCOVERAGE")
        for entry in detail["coverage"]:
            mark = "yes" if entry["satisfied"] else "NO "
            decided = entry["satisfied_by"]
            if decided == "audit":
                decided += " — guaranteed by model review, not a deterministic check"
            typer.echo(f"  {entry['requirement_key']}  {mark}  by {decided}")
            typer.echo(f"      {entry['detail']}")
        typer.echo("\nVALIDATION")
        for check in detail["validation"]:
            state = "pass" if check["passed"] else ("BLOCK" if check["blocking"] else "advise")
            typer.echo(f"  {state:<6} {check['kind']}/{check['key']}: {check['detail']}")
        typer.echo("\nATTEMPTS")
        for attempt in detail["attempts"]:
            typer.echo(
                f"  {attempt['stage']:<10} #{attempt['attempt']} {attempt['outcome']:<16} "
                f"{attempt['backend']}  {attempt['model_canonical_id'] or '—'}"
            )
            typer.echo(
                f"      prompt {attempt['prompt_id']} {attempt['prompt_version']} "
                f"{attempt['prompt_sha256']}"
            )
            typer.echo(
                f"      tokens in {attempt['input_tokens']} out {attempt['output_tokens']}, "
                f"{attempt['provider_ms']} ms"
            )
            if attempt["degradations"]:
                typer.echo(f"      degradations: {'; '.join(attempt['degradations'])}")


@app.command(name="history")
def history(
    project_id: Annotated[str, typer.Argument()],
    unit_key: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show every version of a unit with its coverage. Mode: local."""
    from ideapress.cli.commands.plan import runtime_for
    from ideapress.services.units import unit_history

    for runtime in runtime_for():
        with runtime.storage.read() as session:
            versions = unit_history(session, project_id, unit_key)
        if json_output:
            typer.echo(json_module.dumps(versions, indent=2, sort_keys=True, default=str))
            return
        if not versions:
            typer.echo("No versions yet.")
            return
        for entry in versions:
            marker = "committed" if entry["committed"] else "draft"
            typer.echo(
                f"v{entry['version']}  {marker:<10} {entry['word_count']} words  "
                f"{entry['content_hash']}"
            )


@app.command(name="revise")
def revise(
    project_id: Annotated[str, typer.Argument()],
    unit_key: Annotated[str, typer.Argument()],
    instructions: Annotated[
        str, typer.Option("--instructions", help="What to change, in your own words.")
    ] = "",
) -> None:
    """Revise a committed unit, creating a new version. Mode: local.

    A committed version is immutable, so this makes a new one and keeps the old. The same bounds
    apply: your instructions inform the revision, they do not raise the round limit.
    """
    from ideapress.cli.commands.plan import _wait, runtime_for
    from ideapress.errors import StagePreconditionFailed
    from ideapress.services.stage_bodies import start_stage
    from ideapress.services.unit_reports import unit_detail

    for runtime in runtime_for():
        detail = unit_detail(runtime, project_id=project_id, unit_key=unit_key)
        if detail["version"] is None:
            typer.secho(
                f"Unit {unit_key} has no committed version to revise; draft it first.",
                err=True,
                fg=typer.colors.RED,
            )
            raise typer.Exit(1)
        try:
            task = start_stage(
                runtime,
                project_id=project_id,
                stage="draft",
                units=[unit_key],
                overrides={"instructions": instructions} if instructions else {},
            )
        except StagePreconditionFailed as exc:
            typer.secho(exc.message, err=True, fg=typer.colors.RED)
            raise typer.Exit(1) from exc
        typer.echo(f"task {task.run_id}")
        if _wait(runtime, task.run_id) != "completed":
            raise typer.Exit(1)
