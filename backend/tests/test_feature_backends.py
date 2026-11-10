"""Memory / Registry / Knowledge feature boundaries (N1 → N4/N6).

No backend is registered in this build, so every boundary raises 503
``feature.unavailable`` and every skeleton router answers the same envelope.
"""

import pytest

from app.core.errors import AppError
from app.services import knowledge_backend, memory_backend, registry_backend


@pytest.mark.parametrize("mod,feature", [
    (memory_backend, "memory"),
    (registry_backend, "registry"),
    (knowledge_backend, "knowledge_bases"),
])
def test_unregistered_backend_raises_503(mod, feature):
    mod.set_backend(None)
    with pytest.raises(AppError) as exc:
        mod.get_backend()
    assert exc.value.code == "feature.unavailable"
    assert exc.value.status_code == 503
    assert exc.value.detail == {"feature": feature}


@pytest.mark.parametrize("path", [
    "/api/memory/overview", "/api/memory/actors", "/api/memory/resources",
    "/api/knowledge-bases",
])
def test_skeleton_routers_answer_503(client, path):
    # `client` registers the real self-hosted Memory backend (N6) at startup, so a
    # memory path needs its backend cleared to still exercise the skeleton 503;
    # Knowledge Bases has no backend yet (N6 Task 3), clearing it is a no-op there.
    memory_backend.set_backend(None)
    knowledge_backend.set_backend(None)
    res = client.get(path)
    assert res.status_code == 503
    assert res.json()["code"] == "feature.unavailable"


def test_create_app_registers_the_local_registry_backend(client):
    from app.services.registry_local import LocalRegistryBackend

    assert isinstance(registry_backend.backend_or_none(), LocalRegistryBackend)


def test_create_app_registers_the_selfhosted_memory_backend(client):
    from app.services.memory_selfhosted import SelfHostedMemoryBackend

    assert isinstance(memory_backend.backend_or_none(), SelfHostedMemoryBackend)


def test_registry_routes_answer_503_when_the_backend_is_cleared(client):
    registry_backend.set_backend(None)
    for path in ("/api/registry/records", "/api/registry/attachables"):
        res = client.get(path)
        assert res.status_code == 503 and res.json()["code"] == "feature.unavailable", path


def test_scoped_actor_round_trip():
    actor = memory_backend.scoped_actor("agent-1", "alice")
    assert actor == "agent-1__alice"
    assert memory_backend.decode_actor(actor)["agent_id"] == "agent-1"
    assert memory_backend.decode_actor(actor)["human_actor"] == "alice"
    assert memory_backend.decode_actor(actor)["actor_id"] == actor


# ── N12: evaluation / optimization are capabilities-driven routers (no backend slot) ──

_EVAL_PATHS = ("/api/eval/datasets", "/api/eval/evaluators", "/api/eval/online",
               "/api/eval/pipelines", "/api/eval/queue", "/api/eval/recommendation-providers")
_OPT_PATHS = ("/api/runtime-canaries",)  # N13-obs2: /api/experiments retired


@pytest.fixture
def settings_cache_cleared():
    """Requested BEFORE `monkeypatch` so its teardown runs AFTER the env is restored:
    requests made with LAUNCHPAD_FEATURES set re-cache settings, and a stale cache
    would leak the reduced feature list into the next test."""
    from app.core import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _app_with_features(monkeypatch, features: list[str]):
    import json

    from fastapi.testclient import TestClient

    from app.core import config
    from app.main import create_app

    monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(features))
    config.get_settings.cache_clear()
    return TestClient(create_app())


def test_evaluation_routes_exist_only_with_the_feature(settings_cache_cleared, monkeypatch):
    on = _app_with_features(monkeypatch, ["agents", "chat", "evaluation"])
    off = _app_with_features(monkeypatch, ["agents", "chat"])
    for path in _EVAL_PATHS:
        assert on.get(path).status_code != 404, path
        assert off.get(path).status_code == 404, path
    # optimization is a separate feature: canaries stay hidden without it
    for path in _OPT_PATHS:
        assert on.get(path).status_code == 404, path


def test_optimization_routes_exist_only_with_the_feature(settings_cache_cleared, monkeypatch):
    on = _app_with_features(monkeypatch, ["agents", "chat", "optimization"])
    off = _app_with_features(monkeypatch, ["agents", "chat"])
    for path in _OPT_PATHS:
        assert on.get(path).status_code != 404, path
        assert off.get(path).status_code == 404, path
