"""The runtime/security key registry has exactly one copy, read by `PUT /settings` and the CLI.

The registry used to live inside `ideapress.web.routes.settings`; `config schema` needed it too,
so it moved to `ideapress.services.settings_registry` (row WS3). Since row WI1 the route reaches it
only through `ideapress.services.settings`, which owns no key list of its own either.
"""

from __future__ import annotations

from ideapress.services import settings_registry


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
