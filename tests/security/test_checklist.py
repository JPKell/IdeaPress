"""``docs/security.md``, bullet by bullet (M9 audit Group 5, item Q4 — ported from LoadCoach's
``tests/security/test_checklist.py``).

Every claim the guide makes is either held directly here, held by a named test elsewhere in this
repository (the map at the bottom asserts those tests exist and are callable), or is a permanent
design statement the guide itself states as a boundary rather than a property of the running code
(the single-user, no-account model). "No X" is a claim about the surface, so where it is cheap to
assert directly — no login route, no outbound network by default, a reporting address that exists
— it is asserted about the surface rather than merely written down.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from ideapress.config import load_settings
from ideapress.web.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]


# security.md "The bind": loopback by default, no authentication, no accounts


def test_the_default_bind_is_loopback_with_no_allowed_hosts_required() -> None:
    settings = load_settings().settings
    assert settings.server.host in {"127.0.0.1", "localhost", "::1"}


def test_no_route_offers_a_login_or_session() -> None:
    """security.md: "No authentication or user accounts." — asserted about the route table.

    Excludes FastAPI's own interactive-docs routes (``/docs/oauth2-redirect``), which exist for
    every application regardless of whether it has accounts and name no real endpoint of this
    one's.
    """
    app = create_app(load_settings().settings)
    paths = {route.path for route in app.routes if hasattr(route, "path")}
    suspicious = [
        path
        for path in paths
        if not path.startswith("/docs")
        and any(word in path.lower() for word in ("login", "session"))
    ]
    assert suspicious == []


# security.md "Your content" / "What IdeaPress does not do": stored locally, never uploaded, no
# outbound network in the default configuration — the property `tests/conftest.py`'s autouse
# ``no_network`` fixture enforces for the whole suite. Asserted directly here rather than only by
# every other test happening not to trip it.


def test_a_non_loopback_socket_connection_is_refused_by_default() -> None:
    with pytest.raises(RuntimeError, match="network access refused"):
        socket.create_connection(("93.184.216.34", 80), timeout=1)


def test_a_loopback_socket_connection_is_not_refused() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        connecting = socket.create_connection(listener.getsockname(), timeout=1)
        connecting.close()
    finally:
        listener.close()


# security.md "Reporting something": SECURITY.md exists at the repository root


def test_security_md_exists_at_the_repository_root() -> None:
    assert (REPO_ROOT / "SECURITY.md").is_file()


# The map of every remaining bullet to the test that holds it.

CHECKLIST_MAP: dict[str, tuple[str, str]] = {
    "non-loopback bind without allowed_hosts refuses to start": (
        "tests.security.test_lan_exposure",
        "test_a_non_loopback_bind_with_no_allowed_hosts_refuses_to_start",
    ),
    "0.0.0.0 needs a separate exposure acknowledgement": (
        "tests.security.test_lan_exposure",
        "test_binding_to_every_interface_needs_an_acknowledgement_too",
    ),
    "Host header is validated before routing (421, not 404)": (
        "tests.security.test_lan_exposure",
        "test_host_validation_precedes_routing_on_a_lan_bind",
    ),
    "every UI form route is CSRF protected": (
        "tests.security.test_lan_exposure",
        "test_every_ui_form_route_is_csrf_protected",
    ),
    "the CSRF cookie keeps its __Host-/Secure flags on a non-loopback bind": (
        "tests.security.test_lan_exposure",
        "test_the_csrf_cookie_keeps_its_flags_on_a_lan_bind",
    ),
    "a remote backend is labelled as egress": (
        "tests.security.test_lan_exposure",
        "test_a_remote_backend_is_labelled_as_egress",
    ),
    "a remote backend needs allow_remote": (
        "tests.unit.test_config",
        "test_remote_backend_needs_allow_remote",
    ),
    "model output is never executed (no eval, exec or subprocess)": (
        "tests.security.test_sanitization_sweep",
        "test_no_model_output_reaches_a_subprocess_or_eval",
    ),
    "a path-traversal string in model output is blocking and never commits": (
        "tests.security.test_sanitization_sweep",
        "test_a_path_traversal_in_model_output_is_blocking_and_never_commits",
    ),
    "a <script> tag in model output is flagged but not blocking": (
        "tests.security.test_sanitization_sweep",
        "test_a_script_tag_in_model_output_is_flagged_but_not_blocking",
    ),
    "every view escapes the model output it shows": (
        "tests.security.test_sanitization_sweep",
        "test_every_view_escapes_the_payload_it_shows",
    ),
    "no template applies the `safe` filter": (
        "tests.security.test_sanitization_sweep",
        "test_no_template_applies_the_safe_filter",
    ),
    "no module renders a template built from a runtime string": (
        "tests.security.test_sanitization_sweep",
        "test_no_module_renders_a_template_from_a_string",
    ),
    "the sanitization sweep covers every export format and page, by walking the code": (
        "tests.security.test_sanitization_sweep",
        "test_the_sweep_covers_every_format_the_application_ships",
    ),
    "model output is never a control-flow decision: silence never satisfies a requirement": (
        "tests.integration.test_audit_attestation",
        "test_audit_silence_pauses_instead_of_satisfying",
    ),
    "your content is never logged at INFO or above by default": (
        "tests.unit.test_logging",
        "test_content_is_redacted_by_default_at_every_level",
    ),
    "content logging is opt-in and DEBUG-only": (
        "tests.unit.test_logging",
        "test_content_survives_at_debug_when_deliberately_enabled",
    ),
    "an archive entry that escapes the extraction directory is refused, writing nothing": (
        "tests.security.test_project_archives",
        "test_an_entry_that_escapes_the_directory_is_refused",
    ),
    "a symlink or hardlink entry is refused": (
        "tests.security.test_project_archives",
        "test_a_symlink_entry_is_refused",
    ),
    "a high compression-ratio archive is refused": (
        "tests.security.test_project_archives",
        "test_a_high_ratio_archive_is_refused",
    ),
    "the entry-count/size/ratio caps are declared and ordered sensibly": (
        "tests.security.test_project_archives",
        "test_the_caps_are_all_declared_and_ordered_sensibly",
    ),
    "--inspect reports an archive's contents and writes nothing, even on the safe path": (
        "tests.security.test_project_archives",
        "test_inspection_writes_nothing_at_all",
    ),
    "the importer never calls extractall": (
        "tests.security.test_project_archives",
        "test_the_importer_never_calls_extractall",
    ),
}


def test_every_remaining_checklist_item_is_held_by_a_named_test() -> None:
    import importlib

    for item, (module_name, function_name) in CHECKLIST_MAP.items():
        module = importlib.import_module(module_name)
        assert callable(getattr(module, function_name, None)), (item, module_name, function_name)
