"""The stage vocabulary is one set, spelled once — and the document is the arbiter.

Workflows §2 is the only stage list. These tests read it back out of the repository's own mirrored
copy and compare it with :mod:`ideapress.domain.stages`, in both directions, so a stage added to
the documentation without the code (or the reverse) fails here. `fact_check` existed in
configuration and in the LoadCoach task map while appearing in no stage list; that is what this
test exists to make impossible.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel

from ideapress.config import ConfigurationError, StageBindings, check_stage_vocabulary
from ideapress.domain.stages import (
    GATE_STAGES,
    MODEL_STAGES,
    NO_MODEL_STAGES,
    STAGES,
    StageId,
    is_stage,
    stage_definition,
)

WORKFLOWS_DOC = Path(__file__).resolve().parents[2] / "docs" / "apps" / "ideapress" / "workflows.md"
SPEC_DOC = Path(__file__).resolve().parents[2] / "docs" / "apps" / "ideapress" / "spec.md"


def _documented_stages() -> dict[str, bool]:
    """Parse workflows §2's table into ``stage -> uses a model``."""
    text = WORKFLOWS_DOC.read_text(encoding="utf-8")
    section = text.split("## 2. Stages", 1)[1].split("## 3.", 1)[0]
    rows: dict[str, bool] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in line.split("|")[1:-1]]
        if len(cells) != 6 or not cells[0].isdigit():
            continue
        stage = cells[1].strip("`")
        # "**No**" is no model. "Optional" is the *stage*, not the model — workflows §2's own
        # paragraph names `research` as the fifth of the five stages that involve no model.
        model_cell = cells[5]
        rows[stage] = not (model_cell.startswith("**No**") or model_cell == "Optional")
    return rows


def _documented_bindings() -> set[str]:
    """Parse spec §12's `[models.stages]` keys."""
    text = SPEC_DOC.read_text(encoding="utf-8")
    section = text.split("[models.stages]", 1)[1].split("# One key per", 1)[0]
    return set(re.findall(r"^(\w+)\s*=", section, flags=re.MULTILINE))


def test_stage_table_matches_the_document_in_both_directions() -> None:
    documented = _documented_stages()
    assert set(documented) == set(STAGES), "workflows §2 and STAGES name different stages"
    for stage, uses_model in documented.items():
        assert stage_definition(stage).uses_model is uses_model, f"{stage}: model column differs"


def test_five_stages_involve_no_model_as_the_document_says() -> None:
    assert len(NO_MODEL_STAGES) == 5
    assert GATE_STAGES == {"validate", "coverage", "commit", "export"}
    assert GATE_STAGES <= NO_MODEL_STAGES


def test_model_stages_are_exactly_the_specs_bindings() -> None:
    assert _documented_bindings() == MODEL_STAGES
    assert set(StageBindings.model_fields) == MODEL_STAGES


def test_binding_for_a_stage_that_does_not_exist_is_refused_by_name() -> None:
    class Extra(StageBindings):
        edit: str = "ollama/whatever"  # `edit` was in an earlier config and is not a stage

    with pytest.raises(ConfigurationError) as caught:
        check_stage_vocabulary(Extra())
    assert "edit" in caught.value.message


def test_model_using_stage_with_no_binding_is_refused_by_name() -> None:
    # A section binding ten of the eleven model stages is what a hand-edited config amounts to.
    class Partial(BaseModel):
        requirements: str = "ollama/qwen3.5:9b-q8_0"
        research_synthesis: str = "ollama/qwen3.5:9b-q8_0"
        outline: str = "ollama/qwen3.5:9b-q8_0"
        repair: str = "ollama/qwen3.5:9b-q8_0"
        audit_fast: str = "ollama/qwen3.5:9b-q8_0"
        audit_deep: str = "ollama/qwen3.5:9b-q8_0"
        fact_check: str = "ollama/qwen3.5:9b-q8_0"
        critique: str = "ollama/qwen3.5:9b-q8_0"
        revise: str = "ollama/qwen3.5:9b-q8_0"
        project_review: str = "ollama/qwen3.5:9b-q8_0"

    with pytest.raises(ConfigurationError) as caught:
        check_stage_vocabulary(cast("StageBindings", Partial()))
    assert "draft" in caught.value.message


def test_is_stage_refuses_the_near_misses_the_audit_found() -> None:
    assert not is_stage("audit")
    assert not is_stage("edit")
    assert is_stage("audit_fast")
    assert is_stage("fact_check")


def test_stage_definition_refuses_an_unknown_stage() -> None:
    with pytest.raises(KeyError):
        stage_definition("audit")


def test_stage_id_literal_is_the_table() -> None:
    from typing import get_args

    assert set(get_args(StageId)) == set(STAGES)
