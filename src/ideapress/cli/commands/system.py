"""ideapress.cli.commands.system — serve, health, version and doctor.

Every heavier dependency stays behind a lazy import inside a command body (CLI standards §12), so
building ``--help`` never imports FastAPI, SQLAlchemy or uvicorn.
"""

from __future__ import annotations

import json as json_module
from typing import Annotated

import typer

__all__ = ["doctor", "health", "print_version", "serve", "version"]


def serve(
    host: Annotated[
        str | None, typer.Option(help="Bind host. Overrides configuration for this run.")
    ] = None,
    port: Annotated[
        int | None, typer.Option(help="Bind port. Overrides configuration for this run.")
    ] = None,
    config: Annotated[
        str | None, typer.Option("--config", help="Path to a config.toml file.")
    ] = None,
) -> None:
    """Start the web server. Mode: local.

    This is also what runs when ``ideapress`` is invoked with no subcommand at all. Starting
    requires nothing: no backend, no network, no configuration file (spec §20 AC1).
    """
    import os

    import uvicorn
    from baseaicore import ConfigurationError

    from ideapress.config import load_settings

    if config is not None:
        os.environ["IDEAPRESS_CONFIG"] = config
    if host is not None:
        os.environ["IDEAPRESS_SERVER__HOST"] = host
    if port is not None:
        os.environ["IDEAPRESS_SERVER__PORT"] = str(port)

    try:
        loaded = load_settings()
    except ConfigurationError as exc:
        typer.secho(f"Configuration error: {exc.message}", err=True, fg=typer.colors.RED)
        raise typer.Exit(2) from exc

    # uvicorn imports the factory by dotted string. `ideapress.cli` may not reach `ideapress.web`
    # — the web-cli-independence contract forbids the import chain — and a string literal is
    # invisible to import-linter's static analysis, so the two surfaces stay decoupled at the
    # source level while running one application in one process.
    uvicorn.run(
        "ideapress.bootstrap:create_app_from_environment",
        host=loaded.settings.server.host,
        port=loaded.settings.server.port,
        factory=True,
        log_level=loaded.settings.logging.level.lower(),
    )


def print_version(*, json_output: bool) -> None:
    """Print the application, API and schema versions."""
    from ideapress.__about__ import __version__

    payload = {
        "application": "ideapress",
        "version": __version__,
        "api_version": "v1",
        "schema_version": "1",
    }
    if json_output:
        typer.echo(json_module.dumps(payload, indent=2, sort_keys=True))
        return
    typer.echo(f"ideapress {__version__} (API v1, schema 1)")


def version(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print the application and API versions. Mode: local."""
    print_version(json_output=json_output)


def health(
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Report component health. Mode: local.

    Exits 0 when every component is ``ok`` or ``degraded`` and 1 when any is ``unavailable``. A
    backend that is not running is *degraded*, not an outage: opening projects and exporting
    committed content need no model.
    """
    from mirrorwall import ComponentStatus

    from ideapress.services.diagnostics import health_report

    report = health_report()
    if json_output:
        typer.echo(json_module.dumps(report, indent=2, sort_keys=True))
    else:
        for component in report["components"]:
            typer.echo(f"{component['name']:<12} {component['status']:<16} {component['detail']}")
    unavailable = any(
        component["status"] == ComponentStatus.UNAVAILABLE.value
        for component in report["components"]
    )
    raise typer.Exit(1 if unavailable else 0)


def doctor() -> None:
    """Diagnose a broken installation. Mode: local.

    Names every documented failure mode it can check without a network: configuration that will not
    load, a data directory that cannot be written, a database that will not open, a prompt pack
    that does not match its manifest, and a backend that is not reachable — the last of which is a
    finding, not a failure.
    """
    from ideapress.services.diagnostics import diagnose

    findings = diagnose()
    for finding in findings:
        colour = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW}.get(
            finding.level, typer.colors.RED
        )
        typer.secho(f"[{finding.level:>4}] {finding.name}: {finding.detail}", fg=colour)
        if finding.remedy:
            typer.echo(f"         → {finding.remedy}")
    failed = [f for f in findings if f.level == "fail"]
    raise typer.Exit(1 if failed else 0)
