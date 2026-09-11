"""Row WI1 follow-up: `ideapress doctor` checks the bindings a stage would actually use.

A binding stored through `PUT /settings` decides the model the next stage asks for (api.md §6), so
a doctor reading only `config.toml` would pass a binding no stage uses and miss the one that is.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ideapress.config import load_settings
from ideapress.services.diagnostics import diagnose
from ideapress.services.runtime import build_runtime
from ideapress.services.settings import write_runtime_settings


def test_doctor_checks_a_stored_stage_binding() -> None:
    runtime = build_runtime(load_settings().settings)
    try:
        write_runtime_settings(
            runtime.storage,
            {"models.stages.draft": ""},
            settings=runtime.configured,
            now=datetime.now(UTC),
        )
    finally:
        runtime.close()

    bindings = next(finding for finding in diagnose() if finding.name == "stage model bindings")

    assert bindings.level == "fail"
    assert "draft" in bindings.detail
