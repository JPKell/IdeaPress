"""ideapress.cli.commands.prompts — inspect the prompt pack and regenerate its manifest.

ADR-0012 and ADR-0028: the records are IdeaPress's, the machinery is `setspec.prompts`. `build`
regenerates the manifest, which is the one operation that must exist locally — a record edited
without it fails to load, by design.
"""

from __future__ import annotations

import json as json_module
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Inspect prompt records and their hashes.")


@app.command(name="list")
def list_prompts(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List every prompt record with its version and hash. Mode: local."""
    from ideapress.services.prompts import library

    pack = library()
    records = [
        {"prompt_id": r.prompt_id, "version": r.version, "purpose": r.purpose}
        for r in sorted(pack.all_records(), key=lambda r: r.prompt_id)
    ]
    if json_output:
        typer.echo(json_module.dumps(records, indent=2, sort_keys=True))
        return
    typer.echo(f"{pack.pack_id} {pack.pack_version}")
    for record in records:
        typer.echo(f"  {record['prompt_id']:<38} {record['version']}")


@app.command(name="show")
def show(
    prompt_id: Annotated[str, typer.Argument()],
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show one prompt record in full. Mode: local."""
    from setspec.prompts import PromptNotFound

    from ideapress.services.prompts import library

    try:
        record = library().get(prompt_id)
    except PromptNotFound as exc:
        typer.secho(str(exc), err=True, fg=typer.colors.RED)
        raise typer.Exit(1) from exc
    if json_output:
        typer.echo(json_module.dumps(record.body, indent=2, sort_keys=True))
        return
    typer.echo(f"{record.prompt_id} {record.version}")
    typer.echo(f"purpose: {record.purpose}")
    typer.echo(f"\nsystem:\n{record.system}")
    typer.echo(f"\ntemplate:\n{record.template}")
    typer.echo(f"\nvariables: {', '.join(sorted(record.variables))}")


@app.command(name="build")
def build(
    check: Annotated[
        bool, typer.Option("--check", help="Report drift without writing the manifest.")
    ] = False,
) -> None:
    """Regenerate the pack manifest. Mode: local.

    Exits 1 under ``--check`` when the manifest is out of date, which is what a pre-commit hook
    wants: a record edited without regenerating the manifest fails to load at runtime, and finding
    that at commit time is cheaper than finding it mid-stage.
    """
    from setspec.prompts import build_manifest, write_manifest

    from ideapress.services.prompts import PACK_ROOT

    manifest, drift = build_manifest(PACK_ROOT, generated_at="2026-08-31T00:00:00Z")
    manifest["pack_id"] = "ideapress.stages"
    changed = bool(drift.added or drift.removed or drift.changed)
    for prompt_id, version in drift.added:
        typer.echo(f"added   {prompt_id} {version}")
    for prompt_id, version in drift.removed:
        typer.echo(f"removed {prompt_id} {version}")
    for prompt_id, version in drift.changed:
        typer.echo(f"changed {prompt_id} {version}")
    if check:
        typer.echo("manifest is current" if not changed else "manifest is out of date")
        raise typer.Exit(1 if changed else 0)
    path = write_manifest(manifest, PACK_ROOT)
    typer.echo(f"wrote {path}")
