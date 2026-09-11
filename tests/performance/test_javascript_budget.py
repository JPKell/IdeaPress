"""ADR-0139, as this application inherits it at row WM2: a page loads at most 120 KB of
JavaScript in total, ECharts and mermaid aside. Marked ``performance`` like every budget."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from tests.e2e.test_stage_stream import _project, _scripted_runtime

from ideapress.config import load_settings
from ideapress.web.app import create_app

pytestmark = pytest.mark.performance

_SCRIPT_SRC = re.compile(r'<script[^>]+src="([^"]+)"')
_INLINE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S)


def test_every_page_stays_under_the_total_budget() -> None:
    sizes: dict[str, int] = {}
    totals: dict[str, int] = {}
    app = create_app(load_settings().settings, runtime_builder=_scripted_runtime)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        project_id = _project(client)
        for path in (
            "/",
            f"/projects/{project_id}",
            f"/projects/{project_id}/workspace",
            "/backends",
            "/system",
        ):
            response = client.get(path)
            if response.status_code != 200:
                continue
            html = response.text
            total = sum(len(script) for script in _INLINE.findall(html))
            for src in _SCRIPT_SRC.findall(html):
                if "vendor/echarts" in src or "vendor/mermaid" in src:
                    continue
                name = src.split("?")[0]
                if name not in sizes:
                    asset = client.get(src)
                    assert asset.status_code == 200, (path, src)
                    sizes[name] = len(asset.content)
                total += sizes[name]
            totals[path] = total
    assert max(totals.values()) <= 120 * 1024, totals
