"""The runtime/security key registry has exactly one copy, read by the web route and the CLI.

The registry used to live inside `ideapress.web.routes.settings`; `config schema` needed it too,
so it moved to `ideapress.services.settings_registry` and the route now imports it rather than
defining its own copy (row WS3). These tests are the guard against that drifting back apart.
"""

from __future__ import annotations

from ideapress.services import settings_registry
from ideapress.web.routes import settings as settings_route


def test_the_web_route_imports_the_registry_rather_than_owning_a_copy() -> None:
    assert settings_route.RUNTIME_KEYS is settings_registry.RUNTIME_KEYS
    assert settings_route.CONFIG_ONLY_KEYS is settings_registry.CONFIG_ONLY_KEYS


def test_is_runtime_key_accepts_the_registered_keys() -> None:
    for key in settings_registry.RUNTIME_KEYS:
        assert settings_registry.is_runtime_key(key)


def test_is_runtime_key_accepts_every_model_stage_binding() -> None:
    from ideapress.domain.stages import MODEL_STAGES

    for stage in MODEL_STAGES:
        assert settings_registry.is_runtime_key(f"models.stages.{stage}")


def test_is_runtime_key_refuses_a_security_key_and_an_unknown_one() -> None:
    for key in settings_registry.CONFIG_ONLY_KEYS:
        assert not settings_registry.is_runtime_key(key)
    assert not settings_registry.is_runtime_key("models.stages.not_a_stage")
    assert not settings_registry.is_runtime_key("nonsense")
