"""Shared fixtures.

Two properties every test in this suite depends on:

* **No network, ever.** The default suite must pass with no backend reachable and no network
  (spec §20 AC11), so the ``no_network`` autouse fixture makes an accidental socket a loud failure
  rather than a slow one. A test that genuinely needs a socket is marked ``live``.
* **No ambient state.** Configuration reads ``IDEAPRESS_*`` and the XDG variables from the real
  environment, so every test gets its own data directory and a cleared prefix. Without this a
  developer's own ``~/.config/ideapress/config.toml`` would change the result of a test run.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REAL_SOCKET_CONNECT = socket.socket.connect


@pytest.fixture(autouse=True)
def no_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse any outbound socket connection that is not to loopback.

    Tests marked ``live`` are exempt: those are the ones that need a real provider.
    """
    if request.node.get_closest_marker("live"):
        return

    def _guard(self: socket.socket, address: Any) -> None:
        host = address[0] if isinstance(address, tuple) else str(address)
        if host in {"127.0.0.1", "::1", "localhost"}:
            _REAL_SOCKET_CONNECT(self, address)
            return
        message = f"network access refused in a default test: {address!r}"
        raise RuntimeError(message)

    monkeypatch.setattr(socket.socket, "connect", _guard)


@pytest.fixture(scope="session", autouse=True)
def isolated_session(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """The same redirection as :func:`isolated_environment`, for the whole session.

    A module-scoped fixture is built before any function-scoped one, so without this it saw the
    real environment: three of them built applications over the operator's real
    ``~/.local/share/ideapress/`` on every run from 2026-08-31 until row WI1 found 1186 test
    projects there. Each test still gets its own directories from :func:`isolated_environment`.
    """
    root = tmp_path_factory.mktemp("session-isolation")
    with pytest.MonkeyPatch.context() as patch:
        for key in list(os.environ):
            if key.startswith("IDEAPRESS_"):
                patch.delenv(key, raising=False)
        for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME"):
            patch.setenv(name, str(root / name.lower()))
        patch.setenv("IDEAPRESS_DATA_DIR", str(root / "data"))
        yield root


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point every XDG path at a temporary directory and clear the ``IDEAPRESS_`` prefix."""
    for key in list(os.environ):
        if key.startswith("IDEAPRESS_"):
            monkeypatch.delenv(key, raising=False)
    data = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("IDEAPRESS_DATA_DIR", str(data))
    monkeypatch.chdir(tmp_path)
    data.mkdir(parents=True, exist_ok=True)
    yield data
