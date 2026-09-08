"""Degradation: disk full while exporting (graceful-degradation.md, row "Disk full").

The matrix's wording — "Draft written to a temp file and reported" — does not match what
``export_project`` (``services/export.py``) actually does: it renders the whole document in memory
and writes it directly to the project's own export path with ``Path.open("w", ...)``, already
wrapped ``except OSError as exc: raise ExportFailed(...)``. There is no temp-file staging anywhere
in this codebase (confirmed by its absence). This test proves the real behaviour instead —
``ExportFailed`` names the path and the original error, and nothing partial is left claiming to be
the export — and ``graceful-degradation.md``'s IdeaPress cell was corrected in the same change to
say so.

``export_project`` takes no injectable writer, so the narrowest seam available is a scoped
monkeypatch of ``Path.open`` that raises only for *this* export's own path — every other file the
test touches (fixtures, the database, prior renders) opens normally through the real method.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import pytest
from tests.integration.test_export import (  # noqa: F401 — fixtures, not called directly
    committed_project,
    runtime,
    settings,
)

from ideapress.errors import ExportFailed
from ideapress.services.export import export_project

if TYPE_CHECKING:
    from ideapress.services.runtime import Runtime


def test_a_disk_full_export_write_is_refused_cleanly_and_writes_nothing_partial(
    runtime: Runtime,  # noqa: F811 — the fixture, per the import above
    committed_project: str,  # noqa: F811 — the fixture, per the import above
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = runtime.projects.get(committed_project)
    target = runtime.projects.directory(project) / f"{project.slug}.md"
    real_open = Path.open

    def _open_that_fails_for_the_export_target(
        self: Path, mode: str = "r", *args: Any, **kwargs: Any
    ) -> IO[Any]:
        if self == target and "w" in mode:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open_that_fails_for_the_export_target)

    with pytest.raises(ExportFailed) as excinfo:
        export_project(runtime, project_id=committed_project, fmt="markdown")
    assert str(target) in str(excinfo.value)
    assert excinfo.value.details["path"] == str(target)
    assert not target.exists()

    # The disk is "full" only for that one write: undo the fault and prove the export the
    # matrix promises still works — this was a refusal, not a wound.
    monkeypatch.setattr(Path, "open", real_open)
    written = export_project(runtime, project_id=committed_project, fmt="markdown")
    assert Path(str(written["path"])) == target
    assert target.exists()
