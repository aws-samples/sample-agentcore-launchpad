"""D21 (M1-d): Deployment.catalog_version / model_descriptor and the agent view's hint."""

import types

from app.core.db import SessionLocal
from app.deployer.pipeline import (
    StageResult,
    create_deployment,
    execute_deploy_job,
    register_method,
)
from app.models.ledger import Deployment
from app.routers.agents import catalog_hint, descriptor_changed
from app.schemas import model_catalog
from app.schemas.agent import catalog_version, model_descriptor
from tests.test_pipeline import make_agent

GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"


def _dep(version, descriptor):
    return types.SimpleNamespace(catalog_version=version, model_descriptor=descriptor)


def _agent(model_id):
    return types.SimpleNamespace(spec={"model_id": model_id})


def test_descriptor_changed_matrix():
    now = model_descriptor(GLM)
    assert descriptor_changed(now, now) is False
    # only the version stamp moved (another, unrelated catalog write) → not a change
    assert descriptor_changed({**now, "catalog_version": now["catalog_version"] - 5}, now) is False
    assert descriptor_changed({**now, "streaming": False}, now) is True
    assert descriptor_changed(None, None) is False  # an id outside the catalog, then and now
    assert descriptor_changed(None, now) is True and descriptor_changed(now, None) is True


def test_catalog_hint_same_model_changed_descriptor_and_unrelated_model():
    current = catalog_version()
    now = model_descriptor(GLM)
    assert catalog_hint(_agent(GLM), None) is None
    assert catalog_hint(_agent(GLM), _dep(None, None)) is None  # pre-D21 deployment row
    # same row → versions shown, no change
    assert catalog_hint(_agent(GLM), _dep(current, now)) == {
        "deployed": current, "current": current, "changed": False}
    # a NEW unrelated model bumped the catalog: version differs, this agent's row did not
    older = {**now, "catalog_version": current - 1}
    assert catalog_hint(_agent(GLM), _dep(current - 1, older)) == {
        "deployed": current - 1, "current": current, "changed": False}
    # the agent's own row changed (a capability edit) → the Redeploy hint
    assert catalog_hint(_agent(GLM), _dep(current - 1, {**now, "inference_params": {
        "temperature": False, "top_p": True}, "catalog_version": current - 1}))["changed"] is True
    # an id outside the catalog never raises the hint
    assert catalog_hint(_agent("custom.byoc"), _dep(current, None))["changed"] is False


def test_generate_stage_pins_the_catalog_on_the_deployment_row():
    register_method("fake_pin", {"generate": lambda ctx, agent: StageResult(detail="ok")})
    db = SessionLocal()
    agent = make_agent(db, "fake_pin", name="pin-agent")
    agent.spec = {"name": "pin-agent", "model_id": KIMI}
    db.commit()
    deployment, job = create_deployment(db, agent)
    assert deployment.catalog_version is None and deployment.model_descriptor is None
    db.close()
    execute_deploy_job(job.id)
    db = SessionLocal()
    row = db.get(Deployment, deployment.id)
    assert row.status == "succeeded"
    assert row.catalog_version == catalog_version()
    assert row.model_descriptor == model_descriptor(KIMI)
    db.close()


def test_agent_list_and_detail_expose_the_hint_and_the_deployment_version(client, monkeypatch):
    import app.routers.agents as agents_router

    monkeypatch.setattr(agents_router, "start_deploy_async", lambda job_id: None)
    created = client.post("/api/agents", json={
        "name": "hinted-agent", "method": "zip_runtime", "system_prompt": "p", "model_id": GLM,
    })
    assert created.status_code == 202, created.text
    agent_id = created.json()["agent"]["id"]
    deployment_id = created.json()["deployment_id"]
    db = SessionLocal()
    dep = db.get(Deployment, deployment_id)
    dep.catalog_version = catalog_version() - 1
    dep.model_descriptor = {**model_descriptor(GLM), "streaming": False}
    db.commit()
    db.close()
    detail = client.get(f"/api/agents/{agent_id}").json()
    assert detail["catalog_hint"] == {"deployed": catalog_version() - 1,
                                      "current": catalog_version(), "changed": True}
    assert detail["deployments"][0]["catalog_version"] == catalog_version() - 1
    listed = {a["id"]: a for a in client.get("/api/agents").json()["agents"]}
    assert listed[agent_id]["catalog_hint"]["changed"] is True
    assert listed[agent_id]["deployment"]["catalog_version"] == catalog_version() - 1
    model_catalog.reset()
