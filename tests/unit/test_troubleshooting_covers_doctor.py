"""``ideapress doctor`` diagnoses every check the troubleshooting guide lists (M9 audit Group 5,
item D6 — ported from FreeWeight's `test_troubleshooting_covers_doctor.py`).

Unlike FreeWeight's component-based health report, `ideapress doctor` reports named
:class:`~ideapress.services.diagnostics.Diagnosis` findings (``configuration``, ``data
directory``, ``stage model bindings``, ...) — a mix of the three health components
(``database``, ``backend``, ``prompts``) and configuration/backend-shaped checks that have no
component of their own. This test holds the guide to what FreeWeight's does: every check name
``doctor`` can print is named, verbatim, in ``docs/troubleshooting.md``'s "What `doctor` checks"
table, so a check added to :func:`~ideapress.services.diagnostics.diagnose` with no line in the
guide fails here rather than staying undocumented.
"""

from __future__ import annotations

from pathlib import Path

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "troubleshooting.md"

# The names `diagnose()` can produce, read from the source rather than a live run: a live run
# depends on configuration (loadcoach vs. ollama mode) and would never emit both
# "stage model bindings" branches or "loadcoach task profiles" without a reachable LoadCoach.
# The three health-checker names (`database`, `backend`, `prompts`) are ``ideapress health``'s and
# are asserted the same way, since `doctor` folds them in verbatim (services/diagnostics.py).
DIAGNOSIS_NAMES: frozenset[str] = frozenset(
    {
        "configuration",
        "data directory",
        "database",
        "backend",
        "prompts",
        "stage model bindings",
        "output budget",
        "bind",
        "telemetry",
        "loadcoach task profiles",
    }
)


def test_every_diagnosis_name_the_doctor_can_print_is_named_in_the_guide() -> None:
    text = GUIDE.read_text(encoding="utf-8").lower()
    missing = {name for name in DIAGNOSIS_NAMES if name not in text}
    assert missing == set(), f"doctor check names the guide does not document: {missing}"


def test_diagnosis_names_match_the_source() -> None:
    """The name list above stays honest against the module it describes."""
    import ideapress.services.diagnostics as diagnostics_module

    source = Path(diagnostics_module.__file__).read_text(encoding="utf-8")
    for name in DIAGNOSIS_NAMES - {"database", "backend", "prompts"}:
        assert f'name="{name}"' in source, f"{name!r} is no longer a name diagnose() assigns"
