"""`services.research_tools`: the registry, the containment and the egress ceiling (row M1).

Everything here runs through `httpx.MockTransport` and an injected resolver, so the whole fetch
path — allowlist, media types, size caps, the link-local rule — is exercised with no socket opened
(spec §20 AC11, and G9's "no network" now that a network tool ships).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx
import pytest
from toolyard import EgressClass, IsolationTier, PathContainment, ToolCallRequest, ToolStatus

from ideapress.config import ResearchSettings
from ideapress.services.research_tools import ResearchPlant

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path


def _resolver(_host: str) -> Sequence[str]:
    """Every host resolves to one public address, so the link-local rule has something to see."""
    return ["93.184.216.34"]


def _transport(body: str = "hello", *, media_type: str = "text/plain") -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=body, headers={"content-type": media_type})

    return httpx.MockTransport(handler)


def _plant(tmp_path: Path, **overrides: object) -> ResearchPlant:
    settings = ResearchSettings(**overrides)  # type: ignore[arg-type]  # kwargs are field values
    return ResearchPlant.build(
        settings,
        sources_dir=tmp_path / "p" / "sources",
        resolver=_resolver,
        transport=_transport(),
    )


def test_with_no_host_configured_the_fetch_tool_is_not_registered(tmp_path: Path) -> None:
    """A fresh installation reaches nothing — not even loopback, which is ToolYard's own default."""
    plant = _plant(tmp_path)
    assert plant.registry.get("read_file") is not None
    assert plant.registry.get("http_fetch") is None


def test_a_url_on_an_unconfigured_installation_is_refused_as_a_result(tmp_path: Path) -> None:
    plant = _plant(tmp_path)
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "unknown_tool"


def test_a_configured_host_is_fetched(tmp_path: Path) -> None:
    plant = _plant(tmp_path, allowed_hosts=("a.example",))
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.OK
    assert "hello" in result.content


def test_a_host_off_the_allowlist_is_refused(tmp_path: Path) -> None:
    plant = _plant(tmp_path, allowed_hosts=("a.example",))
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://b.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "host_not_allowed"


def test_a_denied_egress_verdict_closes_the_ceiling_and_toolyard_refuses(tmp_path: Path) -> None:
    """The whole enforcement of a Commissioner denial, in one argument (ADR-0054, ADR-0116 §6)."""
    plant = _plant(tmp_path, allowed_hosts=("a.example",))
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=False),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "egress_not_permitted"


def test_a_tool_omitted_from_the_allowlist_is_refused_differently(tmp_path: Path) -> None:
    """`not_allowlisted` and `unknown_tool` are different facts, and the record keeps them apart."""
    plant = _plant(tmp_path, allowed_tools=("read_file",), allowed_hosts=("a.example",))
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "not_allowlisted"


def test_a_file_inside_the_sources_directory_is_read(tmp_path: Path) -> None:
    root = tmp_path / "p" / "sources"
    root.mkdir(parents=True)
    (root / "notes.md").write_text("the note", encoding="utf-8")
    plant = _plant(tmp_path)
    result = plant.executor(None).execute(
        ToolCallRequest(name="read_file", args={"path": "notes.md"}),
        plant.context("inv-1", egress_approved=False),
    )
    assert result.status is ToolStatus.OK
    assert "the note" in result.content


def test_a_path_outside_the_sources_directory_is_refused(tmp_path: Path) -> None:
    """The project's own export sits one level up, and the stage must not ingest it."""
    project = tmp_path / "p"
    (project / "sources").mkdir(parents=True)
    (project / "p.md").write_text("the exported document", encoding="utf-8")
    plant = _plant(tmp_path)
    result = plant.executor(None).execute(
        ToolCallRequest(name="read_file", args={"path": "../p.md"}),
        plant.context("inv-1", egress_approved=False),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "path_escape"


def test_a_file_over_the_cap_is_refused_rather_than_partly_read(tmp_path: Path) -> None:
    root = tmp_path / "p" / "sources"
    root.mkdir(parents=True)
    (root / "big.md").write_text("x" * 4096, encoding="utf-8")
    plant = _plant(tmp_path, max_file_bytes=1024)
    result = plant.executor(None).execute(
        ToolCallRequest(name="read_file", args={"path": "big.md"}),
        plant.context("inv-1", egress_approved=False),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "too_large"


def test_a_media_type_that_is_not_text_is_refused(tmp_path: Path) -> None:
    settings = ResearchSettings(allowed_hosts=("a.example",))
    plant = ResearchPlant.build(
        settings,
        sources_dir=tmp_path / "p" / "sources",
        resolver=_resolver,
        transport=_transport(media_type="application/octet-stream"),
    )
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "content_type_not_allowed"


def test_a_host_resolving_to_a_link_local_address_is_refused(tmp_path: Path) -> None:
    """ADR-0026 §3's cloud-metadata vector, judged after resolution rather than on the name."""

    def link_local(_host: str) -> Sequence[str]:
        return ["169.254.169.254"]

    plant = ResearchPlant.build(
        ResearchSettings(allowed_hosts=("a.example",)),
        sources_dir=tmp_path / "p" / "sources",
        resolver=link_local,
        transport=_transport(),
    )
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.REFUSED
    assert result.reason == "link_local_address"


def test_an_origin_answering_badly_fails_rather_than_refuses(tmp_path: Path) -> None:
    """ToolYard's own line: a rule saying no is REFUSED, the world answering badly is FAILED."""
    plant = ResearchPlant.build(
        ResearchSettings(allowed_hosts=("a.example",)),
        sources_dir=tmp_path / "p" / "sources",
        resolver=_resolver,
        transport=httpx.MockTransport(lambda _r: httpx.Response(503, text="down")),
    )
    result = plant.executor(None).execute(
        ToolCallRequest(name="http_fetch", args={"url": "http://a.example/x"}),
        plant.context("inv-1", egress_approved=True),
    )
    assert result.status is ToolStatus.FAILED
    assert result.reason == "http_status"


def test_the_containment_reports_no_isolation_tier_and_never_pretends_one(tmp_path: Path) -> None:
    """ADR-0116 decision 2: no subprocess launcher reaches this application."""
    plant = _plant(tmp_path)
    assert PathContainment().isolation_tier() is IsolationTier.UNAVAILABLE
    assert plant.workspace.read_roots == ()
    assert plant.workspace.write_root.name == "sources"


def test_the_ceiling_defaults_closed(tmp_path: Path) -> None:
    plant = _plant(tmp_path)
    assert plant.context("i", egress_approved=False).max_egress is EgressClass.NONE
    assert plant.context("i", egress_approved=True).max_egress is EgressClass.NETWORK


def test_a_tool_name_that_ships_nowhere_is_refused_at_startup() -> None:
    """Configuration cannot supply a handler, so an unknown name can only be a typo."""
    with pytest.raises(ValueError, match="run_command"):
        ResearchSettings(allowed_tools=("read_file", "run_command"))
