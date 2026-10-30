"""Usage scoping of owner-bound providers (N17-obs203 §6).

The catalog a user is OFFERED = the workspace-scope providers' models + the user's own personal
providers' models (administrators: everything): `/api/capabilities.models` (every console picker
— the agent wizard, the eval judge / actor selectors — reads it), the architect's protocol
preamble and proposal validation (the conversation OWNER's catalog), the preset bundles (shared
catalog only). Wherever a model is NAMED, another member's personal provider's model is refused:
agent create / redeploy → 422 `agent.model_not_visible` (the owner or an administrator may still
deploy), an evaluation actor → 422 `run.model_not_visible`, a platform role → 422
`models.model_not_allowed` reason `personal_scope` (the model-defaults page lists workspace-scope
models only).
"""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.routers import agents as agents_router
from app.schemas import model_catalog
from app.schemas.agent import (
    MODEL_NOT_VISIBLE_CODE,
    ModelRefusal,
    assert_model_visible,
    model_catalog_entries,
    model_catalog_lines,
)
from app.services import model_registry, model_roles
from tests import test_visibility as tv

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

PREFIX = "/api/settings/model-providers"
URL = "https://api.ppinfra.com/v3/openai"
pytestmark = pytest.mark.siliconflow


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()


@pytest.fixture
def personal(ada):
    """ada's personal provider with one chat model; returns (ada_id, ada_client, model_id)."""
    ada_id, client = ada
    row = client.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    model = client.post(f"{PREFIX}/{row['id']}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False}).json()
    return ada_id, client, model["model_id"], row["id"]


def _offered(client) -> set[str]:
    body = client.get("/api/capabilities").json()
    return {m["model_id"] for m in body["models"]}


# ── /api/capabilities: what each principal is offered ────────────────────────────────
def test_capabilities_offer_shared_models_plus_the_callers_own_personal_ones(
    admin, personal, bob,
):
    _ada_id, ada_client, model_id, _pid = personal
    _bob_id, bob_client = bob
    assert model_id in _offered(ada_client)  # the owner
    assert model_id in _offered(admin)  # an administrator
    assert model_id not in _offered(bob_client)  # another member — never
    assert "global.zai.glm-5.3" in _offered(bob_client)  # the shared catalog stays
    # the provider table follows the same rule
    assert "ppio" in admin.get("/api/capabilities").json()["model_providers"]
    assert "ppio" not in bob_client.get("/api/capabilities").json()["model_providers"]
    assert "ppio" in ada_client.get("/api/capabilities").json()["model_providers"]


def test_sharing_the_provider_offers_its_models_to_everyone(admin, personal, bob):
    _ada_id, _ada_client, model_id, pid = personal
    _bob_id, bob_client = bob
    assert admin.put(f"{PREFIX}/{pid}", json={"scope": "workspace"}).status_code == 200
    assert model_id in _offered(bob_client)


# ── the pure views ───────────────────────────────────────────────────────────────────
def test_the_catalog_views_filter_by_principal(personal, bob):
    ada_id, _client, model_id, _pid = personal
    bob_id, _bob_client = bob
    snapshot = model_catalog.current()
    owner = model_catalog.Principal(ada_id, False)
    other = model_catalog.Principal(bob_id, False)
    administrator = model_catalog.Principal(None, True)
    assert snapshot.model_visible(model_id, owner)
    assert snapshot.model_visible(model_id, administrator)
    assert snapshot.model_visible(model_id, None)  # no principal = every row (workers)
    assert not snapshot.model_visible(model_id, other)
    assert not snapshot.model_visible(model_id, model_catalog.WORKSPACE_ONLY)
    assert snapshot.model_visible("global.zai.glm-5.3", other)  # shared rows: everyone
    assert model_id in snapshot.visible_model_ids(owner)
    assert model_id not in snapshot.visible_model_ids(other)
    ids = {e["model_id"] for e in model_catalog_entries(principal=other)}
    assert model_id not in ids and "global.zai.glm-5.3" in ids
    assert model_id in {e["model_id"] for e in model_catalog_entries(principal=owner)}
    # the architect prose (preamble / preset bundles) follows the same filter
    assert not any(model_id in line for line in model_catalog_lines(principal=other))
    assert not any(model_id in line for line in model_catalog_lines(
        principal=model_catalog.WORKSPACE_ONLY))
    assert any(model_id in line for line in model_catalog_lines(principal=owner))
    with pytest.raises(ModelRefusal) as exc:
        assert_model_visible(model_id, other)
    assert exc.value.code == MODEL_NOT_VISIBLE_CODE and "PPIO" in str(exc.value)
    assert assert_model_visible(model_id, owner) == model_id


# ── deploy / redeploy ────────────────────────────────────────────────────────────────
def test_deploying_on_another_members_personal_model_is_422_model_not_visible(
    admin, personal, bob,
):
    _ada_id, ada_client, model_id, _pid = personal
    _bob_id, bob_client = bob
    spec = {"name": "on-ppio", "method": "zip_runtime", "system_prompt": "Hi.",
            "model_id": model_id}
    res = bob_client.post("/api/agents", json=spec)
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "agent.model_not_visible"
    assert res.json()["detail"] == {"model_id": model_id}
    assert "personal provider" in res.json()["message"]
    # the owner passes the visibility gate — the NEXT gate answers (no key configured yet)
    own = ada_client.post("/api/agents", json=spec)
    assert own.status_code == 422 and own.json()["code"] == "model.provider_unconfigured"
    # an administrator passes it too
    adm = admin.post("/api/agents", json=spec)
    assert adm.status_code == 422 and adm.json()["code"] == "model.provider_unconfigured"
    # nothing was created by any of the three
    assert all(a["name"] != "on-ppio" for a in admin.get("/api/agents").json()["agents"])


def test_redeploying_another_members_agent_onto_their_personal_model_is_refused(
    admin, personal, bob,
):
    _ada_id, _ada_client, model_id, _pid = personal
    _bob_id, bob_client = bob
    # an agent bob may redeploy (his own, on the shared default model)
    created = bob_client.post("/api/agents", json={"name": "bobs", "method": "zip_runtime",
                                                   "system_prompt": "Hi."})
    assert created.status_code == 202, created.text
    agent_id = created.json()["agent"]["id"]
    from tests.conftest import settle_deploy_jobs

    settle_deploy_jobs(agent_id)
    res = bob_client.post(f"/api/agents/{agent_id}/redeploy",
                          json={"name": "bobs", "method": "zip_runtime", "system_prompt": "Hi.",
                                "model_id": model_id})
    assert res.status_code == 422 and res.json()["code"] == "agent.model_not_visible"


# ── evaluation actor ─────────────────────────────────────────────────────────────────
def test_an_evaluation_actor_on_another_members_personal_model_is_refused(personal, bob):
    from app.core.errors import AppError
    from app.evaluation.routers import _check_actor_model

    ada_id, _client, model_id, _pid = personal
    bob_id, _bob_client = bob
    items = [{"actor_profile": "a persona"}]
    with pytest.raises(AppError) as exc:
        _check_actor_model(items, model_id, None, model_catalog.Principal(bob_id, False))
    assert exc.value.code == "run.model_not_visible" and exc.value.status_code == 422
    # the owner gets the next gate (provider not configured), not the visibility one
    with pytest.raises(AppError) as exc:
        _check_actor_model(items, model_id, None, model_catalog.Principal(ada_id, False))
    assert exc.value.code == "model.provider_unconfigured"


# ── platform roles: workspace-scope only ─────────────────────────────────────────────
def test_a_platform_role_never_points_at_a_personal_provider(admin, personal):
    _ada_id, _client, model_id, pid = personal
    res = admin.put("/api/settings/model-roles/judge", json={"model_id": model_id})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "models.model_not_allowed"
    assert res.json()["detail"]["reason"] == "personal_scope"
    role = model_roles.ROLE_BY_NAME["judge"]
    with pytest.raises(Exception) as exc:
        model_roles.validate(role, model_id)
    assert getattr(exc.value, "detail", {}).get("reason") == "personal_scope"
    # once shared the role accepts it (enabled chat row of a shared provider)
    assert admin.put(f"{PREFIX}/{pid}", json={"scope": "workspace"}).status_code == 200
    db = SessionLocal()
    try:
        assert model_roles.validate(role, model_id) == model_id
    finally:
        db.close()


# ── N17-obs227 (rev-r20 Nit 2): the HUB-side USE sites ───────────────────────────────────────
# Where the hub itself spends a provider's key for a member — a judge / derived evaluator's
# model, a plan approval's judges, a recommendation's reflection model, the hub judge builder —
# another member's personal model is refused with 422 `models.model_not_visible` (the owner and
# administrators pass to the next gate).
JUDGE = {"name": "tone_judge", "instructions": "Rate the tone of {context}", "level": "SESSION",
         "rating_scale": [{"value": 1, "label": "bad", "definition": "bad"},
                          {"value": 5, "label": "good", "definition": "good"}]}


def test_a_judge_evaluator_on_another_members_personal_model_is_refused(admin, personal, bob):
    ada_id, ada_client, model_id, _pid = personal
    _bob_id, bob_client = bob
    res = bob_client.post("/api/eval/evaluators", json={**JUDGE, "model_id": model_id})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "models.model_not_visible"
    assert res.json()["detail"] == {"model_id": model_id}
    assert "personal provider" in res.json()["message"]
    # the owner and an administrator pass the visibility gate — the next gate answers
    for client in (ada_client, admin):
        own = client.post("/api/eval/evaluators", json={**JUDGE, "model_id": model_id})
        assert own.status_code == 422 and own.json()["code"] == "model.provider_unconfigured"
    # a derived evaluator is held to the same rule
    derived = bob_client.post("/api/eval/evaluators", json={
        "name": "derived_one", "base_evaluator_id": "Builtin.Helpfulness", "model_id": model_id})
    assert derived.status_code == 422 and derived.json()["code"] == "models.model_not_visible"
    # … and so is an UPDATE that moves an existing judge onto the personal model
    mine = bob_client.post("/api/eval/evaluators", json=JUDGE)
    assert mine.status_code == 201, mine.text
    moved = bob_client.put(f"/api/eval/evaluators/{mine.json()['evaluator_id']}",
                           json={**JUDGE, "model_id": model_id})
    assert moved.status_code == 422 and moved.json()["code"] == "models.model_not_visible"
    assert bob_client.get(f"/api/eval/evaluators/{mine.json()['evaluator_id']}").json()[
        "model_id"] != model_id
    assert all(e.get("name") != "derived_one"  # nothing was created by the refused calls
               for e in admin.get("/api/eval/evaluators").json()["evaluators"])


def test_plan_approval_holds_the_judges_to_the_approvers_visibility(personal, bob):
    from types import SimpleNamespace

    from app.assistant.evaluation_assets import _require_judge_models
    from app.core.errors import AppError

    ada_id, _client, model_id, _pid = personal
    bob_id, _bob_client = bob
    plan = SimpleNamespace(evaluators=[
        SimpleNamespace(kind="judge", key="tone", model_id=model_id),
    ])
    with pytest.raises(AppError) as exc:
        _require_judge_models(plan, {}, model_catalog.Principal(bob_id, False))
    assert exc.value.code == "models.model_not_visible" and exc.value.status_code == 422
    assert exc.value.detail == {"model_id": model_id, "key": "tone"}
    with pytest.raises(AppError) as exc:  # the owner meets the next gate
        _require_judge_models(plan, {}, model_catalog.Principal(ada_id, False))
    assert exc.value.code == "model.provider_unconfigured"
    # the approve route hands the approver's principal over (user id + admin flag)
    import inspect

    from app.routers import assistant as assistant_router

    source = inspect.getsource(assistant_router)
    assert "approver_is_admin=identity.is_admin" in source


def test_a_recommendation_reflection_model_and_the_hub_judge_builder_check_visibility(
    personal, bob,
):
    from app.core.errors import AppError
    from app.evaluation import recommendations as recs
    from app.services import aws_clients

    ada_id, _client, model_id, _pid = personal
    bob_id, _bob_client = bob
    other, owner = model_catalog.Principal(bob_id, False), model_catalog.Principal(ada_id, False)
    prov = recs._provider(None)
    with pytest.raises(AppError) as exc:
        recs._model_for(prov, model_id, None, other)
    assert exc.value.code == "models.model_not_visible" and exc.value.status_code == 422
    with pytest.raises(AppError) as exc:
        recs._model_for(prov, model_id, None, owner)
    assert exc.value.code == "model.provider_unconfigured"
    # the hub judge builder — before any key lookup or client construction
    with pytest.raises(AppError) as exc:
        aws_clients.build_chat_model(model_id, region="us-west-2", principal=other)
    assert exc.value.code == "models.model_not_visible" and exc.value.status_code == 422
    with pytest.raises(AppError) as exc:
        aws_clients.global_bedrock_model(model_id, region="us-west-2", principal=other)
    assert exc.value.code == "models.model_not_visible"
    with pytest.raises(AppError) as exc:  # no principal (worker) / the owner: the next gate
        aws_clients.build_chat_model(model_id, region="us-west-2", principal=owner)
    assert exc.value.code == "model.provider_unconfigured"
    with pytest.raises(AppError) as exc:
        aws_clients.build_chat_model(model_id, region="us-west-2")
    assert exc.value.code == "model.provider_unconfigured"
    # the recommendation route hands the caller's principal over
    import inspect

    from app.evaluation import routers as eval_routers

    assert "principal=principal_of_identity(require_identity(request))" in inspect.getsource(
        eval_routers.create_run_recommendations)
