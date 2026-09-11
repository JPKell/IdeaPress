"""The suite never reads or writes the operator's real IdeaPress data — module-scoped fixtures too.

`conftest.isolated_environment` redirects `XDG_*` and `IDEAPRESS_DATA_DIR` per test, but a
module-scoped fixture is built before any function-scoped fixture runs. Three of them
(`test_sanitization_sweep.hostile`, `test_ui_checklist.rendered`, `test_budgets.big_project`)
built an application over `~/.local/share/ideapress/` for every run from 2026-08-31 until row WI1
found 1186 test projects in the operator's database.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ideapress.config import config_dir, data_dir, load_settings


@pytest.fixture(scope="module")
def paths_seen_by_a_module_fixture() -> tuple[Path, Path, str]:
    return data_dir(), config_dir(), str(load_settings().settings.storage.database_url)


def test_a_module_scoped_fixture_sees_no_real_path(
    paths_seen_by_a_module_fixture: tuple[Path, Path, str],
) -> None:
    data, config, database_url = paths_seen_by_a_module_fixture
    home = Path.home()
    for real in (home / ".local" / "share" / "ideapress", home / ".config" / "ideapress"):
        assert not data.is_relative_to(real), data
        assert not config.is_relative_to(real), config
        assert str(real) not in database_url, database_url
