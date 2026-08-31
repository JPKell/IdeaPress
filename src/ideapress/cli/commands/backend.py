"""ideapress.cli.commands.backend — list, test and switch backends."""

from __future__ import annotations

import json as json_module
from typing import Annotated

import typer

__all__ = ["app"]

app = typer.Typer(no_args_is_help=True, help="Inspect, test and switch inference backends.")


@app.command(name="list")
def list_backends(
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """List the configured backends, with reachability and egress. Mode: local."""
    from ideapress.config import load_settings
    from ideapress.services.backends import describe_backends

    described = describe_backends(load_settings().settings)
    if json_output:
        typer.echo(json_module.dumps(described, indent=2, sort_keys=True, default=str))
        return
    for backend in described:
        marks = []
        if backend.get("selected"):
            marks.append("selected")
        if backend.get("fallback"):
            marks.append("fallback")
        if backend.get("egress"):
            marks.append("EGRESS: sends your content off this machine")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        state = "reachable" if backend.get("available") else "not reachable"
        typer.echo(f"{backend['mode']:<20} {state:<14} {backend.get('base_url') or ''}{suffix}")


@app.command(name="test")
def test(
    mode: Annotated[str | None, typer.Option("--mode", help="Which backend to test.")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Round-trip a backend and report latency and its model list. Mode: local.

    Exits 1 when the backend does not answer, so a script can branch on it.
    """
    from ideapress.config import load_settings
    from ideapress.services.backends import test_backend

    report = test_backend(load_settings().settings, mode=mode)
    if json_output:
        typer.echo(json_module.dumps(report, indent=2, sort_keys=True, default=str))
    else:
        typer.echo(f"mode      {report['mode']}")
        typer.echo(f"status    {report['status']}")
        typer.echo(f"endpoint  {report.get('base_url') or 'not configured'}")
        typer.echo(f"latency   {report['latency_ms']} ms")
        typer.echo(f"models    {report['model_count']}")
        if report.get("detail"):
            typer.echo(f"detail    {report['detail']}")
    raise typer.Exit(0 if report["status"] == "ok" else 1)


@app.command(name="switch")
def switch(
    mode: Annotated[str, typer.Argument(help="ollama | openai_compatible | loadcoach")],
) -> None:
    """Write `inference.mode` to the configuration file. Mode: local.

    Refuses a mode that is not one of the three, and refuses to write a file that would then fail
    validation — switching to a remote backend with `providers.allow_remote = false` is refused
    here rather than at the next startup.
    """
    import tomllib

    from baseaicore import ConfigurationError

    from ideapress.config import load_settings, resolve_config_path

    if mode not in {"ollama", "openai_compatible", "loadcoach"}:
        typer.secho(
            f"{mode!r} is not an inference mode. Use ollama, openai_compatible or loadcoach.",
            err=True,
            fg=typer.colors.RED,
        )
        raise typer.Exit(2)

    path = resolve_config_path()
    existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    existing.setdefault("inference", {})["mode"] = mode

    # Validate before writing: a configuration file that will not load is worse than a refusal.
    try:
        load_settings(config_path=path, cli_overrides={"inference": {"mode": mode}})
    except ConfigurationError as exc:
        typer.secho(f"Refusing to switch: {exc.message}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    written = _with_mode(lines, mode)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(written) + "\n", encoding="utf-8")
    typer.echo(f"inference.mode = {mode!r} written to {path}")


def _with_mode(lines: list[str], mode: str) -> list[str]:
    """Return ``lines`` with `inference.mode` set, preserving every other line and comment."""
    output: list[str] = []
    in_inference = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("["):
            if in_inference and not replaced:
                output.append(f'mode = "{mode}"')
                replaced = True
            in_inference = stripped == "[inference]"
        if in_inference and stripped.startswith("mode"):
            output.append(f'mode = "{mode}"')
            replaced = True
            continue
        output.append(line)
    if not replaced:
        output.extend(
            ["", "[inference]", f'mode = "{mode}"'] if not in_inference else [f'mode = "{mode}"']
        )
    return output
