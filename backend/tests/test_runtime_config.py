"""Runtime billing intent: schema, SDK shapes, new-resource defaults and republish."""

from copy import deepcopy

import pytest
from botocore.session import Session
from botocore.validate import validate_parameters
from pydantic import ValidationError

import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.deployer.pipeline import create_deployment
from app.deployer.runtime_config import runtime_config_kwargs
from app.models.ledger import Agent, Deployment, Job, SpecSnapshot
from app.schemas.agent import AgentSpec
from app.services.agentcore import runtime as rt

CONSUMPTION = {
    "platform_version": "V2", "billing_model": "consumption", "minimum_memory_mib": None,
}
BASELINE = {
    "platform_version": "V2", "billing_model": "committed_baseline", "minimum_memory_mib": 4096,
}
MODEL = Session().get_service_model("bedrock-agentcore-control")


def spec_input(method="zip_runtime", **extra):
    spec = {
        "name": "billing-agent", "method": method, "system_prompt": "Be brief.",
        "memory": {"short_term": False, "long_term": False},
    }
    if method == "byoc":
        spec["byoc"] = {"artifact_kind": "code_zip", "upload_id": "upload1"}
    return {**spec, **extra}


@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_consumption_is_valid_and_legacy_dumps_remain_omitted(platform):
    legacy = AgentSpec(**spec_input())
    assert legacy.runtime_config is None
    assert "runtime_config" not in legacy.model_dump()
    assert "runtime_config" not in AgentSpec(**spec_input(runtime_config=None)).model_dump()
    explicit = AgentSpec(**spec_input(runtime_config={
        "platform_version": platform, "billing_model": "consumption",
    }))
    assert explicit.model_dump()["runtime_config"] == {**CONSUMPTION, "platform_version": platform}


@pytest.mark.parametrize("size", [1024, 2048, 4096, 8192])
def test_supported_baseline_sizes_roundtrip(size):
    value = {**BASELINE, "minimum_memory_mib": size}
    spec = AgentSpec(**spec_input(runtime_config=value))
    assert AgentSpec.model_validate(spec.model_dump()).runtime_config.model_dump() == value


@pytest.mark.parametrize("config", [
    {}, {"platform_version": "V2"}, {"billing_model": "consumption"},
    {**BASELINE, "platform_version": "V1"}, {**BASELINE, "platform_version": "V3"},
    {**BASELINE, "billing_model": "typo"}, {**BASELINE, "minimum_memory_mib": None},
    {**BASELINE, "minimum_memory_mib": 0}, {**BASELINE, "minimum_memory_mib": 512},
    {**BASELINE, "minimum_memory_mib": 16384}, {**BASELINE, "minimum_memory_mib": "4096"},
    {**BASELINE, "minimum_memory_mib": 4096.0}, {**BASELINE, "minimum_memory_mib": True},
    {**CONSUMPTION, "minimum_memory_mib": 1024},
    {**CONSUMPTION, "minimum_memory_mib": False}, {**CONSUMPTION, "minimum_memory_mib": "0"},
    {**CONSUMPTION, "minimum_memory_mib": 0.0}, {**CONSUMPTION, "minimumMemoryInMiB": 4096},
])
def test_runtime_config_refuses_invalid_and_coerced_values(config):
    with pytest.raises(ValidationError):
        AgentSpec(**spec_input(runtime_config=config))


def test_harness_refuses_explicit_runtime_config():
    with pytest.raises(ValidationError, match="Runtime-backed"):
        AgentSpec(**spec_input("harness", runtime_config=CONSUMPTION))
    assert "runtime_config" not in AgentSpec(**spec_input("harness")).model_dump()


class ShapeControl:
    """Validate exact SDK request shapes without creating clients or calling AWS."""

    def __init__(self):
        self.created = None
        self.updated = None

    def create_agent_runtime(self, **params):
        validate_parameters(params, MODEL.operation_model("CreateAgentRuntime").input_shape)
        self.created = params
        return {"agentRuntimeId": "billing_rt-abcdefghij", "agentRuntimeVersion": "1"}

    def update_agent_runtime(self, **params):
        validate_parameters(params, MODEL.operation_model("UpdateAgentRuntime").input_shape)
        self.updated = params
        return {"agentRuntimeVersion": "2"}


def call_wrapper(control, wrapper, **config):
    params = {"role_arn": "arn:aws:iam::111122223333:role/agent-exec", **config}
    if "code" in wrapper:
        params.update(s3_bucket="billing-artifacts", s3_key="agent/package.zip")
    else:
        params["container_uri"] = "111122223333.dkr.ecr.us-west-2.amazonaws.com/agents:latest"
    if wrapper.startswith("create"):
        params["runtime_name"] = "billing_rt"
    else:
        params["runtime_id"] = "billing_rt-abcdefghij"
    getattr(rt, wrapper)(control, **params)
    return control.created if wrapper.startswith("create") else control.updated


WRAPPERS = ["create_code_runtime", "update_code_runtime",
            "create_container_runtime", "update_container_runtime"]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("size", [1024, 2048, 4096, 8192])
def test_all_four_runtime_wrappers_pass_valid_sdk_baseline_shapes(wrapper, size):
    params = call_wrapper(ShapeControl(), wrapper, platform_version="V2",
                          resource_configurations=[{"minimumMemoryInMiB": size}])
    assert params["platformVersion"] == "V2"
    assert params["resourceConfigurations"] == [{"minimumMemoryInMiB": size}]


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("platform", ["V1", "V2"])
def test_explicit_consumption_omits_resources_and_workers_keep_old_defaults(wrapper, platform):
    explicit = call_wrapper(ShapeControl(), wrapper, platform_version=platform)
    assert explicit["platformVersion"] == platform
    assert "resourceConfigurations" not in explicit
    worker = call_wrapper(ShapeControl(), wrapper)
    assert "platformVersion" not in worker and "resourceConfigurations" not in worker


def test_legacy_resolver_requires_live_update_and_never_installs_a_create_default():
    spec = AgentSpec(**spec_input())
    assert runtime_config_kwargs(spec) == {}
    with pytest.raises(RuntimeError, match="preserve legacy billing"):
        runtime_config_kwargs(spec, updating=True)
    live = {"platformVersion": "V2", "resourceConfigurations": [{"minimumMemoryInMiB": 2048}]}
    kwargs = runtime_config_kwargs(spec, updating=True, live=live)
    assert kwargs == {"resource_configurations": live["resourceConfigurations"]}
    kwargs["resource_configurations"][0]["minimumMemoryInMiB"] = 8192
    assert live["resourceConfigurations"] == [{"minimumMemoryInMiB": 2048}]
    assert runtime_config_kwargs(spec, updating=True, live={"platformVersion": "V1"}) == {}


@pytest.mark.parametrize("method", ["zip_runtime", "studio", "container", "byoc", "harness"])
@pytest.mark.parametrize("commit", [True, False])
def test_default_is_written_before_the_single_snapshot_only_for_new_runtime_agents(method, commit):
    with SessionLocal() as db:
        spec = AgentSpec(**spec_input(method))
        agent = Agent(workspace_id="default", name=spec.name, method=method, spec=spec.model_dump())
        db.add(agent)
        db.flush()
        dep, _ = create_deployment(db, agent, commit=commit)
        snapshot = db.query(SpecSnapshot).filter_by(deployment_id=dep.id).one()
        if method == "harness":
            assert "runtime_config" not in agent.spec and "runtime_config" not in snapshot.spec
        else:
            assert agent.spec["runtime_config"] == snapshot.spec["runtime_config"] == CONSUMPTION
        assert db.query(SpecSnapshot).count() == 1
        db.commit()


@pytest.mark.parametrize("state", ["update", "resource", "arn", "prior_failed", "explicit"])
def test_existing_and_failed_deploys_never_acquire_the_new_agent_default(state):
    with SessionLocal() as db:
        raw = spec_input(**({"runtime_config": BASELINE} if state == "explicit" else {}))
        agent = Agent(workspace_id="default", name=raw["name"], method=raw["method"], spec=raw)
        if state == "resource":
            agent.resource_id = "billing_rt-abcdefghij"
        if state == "arn":
            agent.arn = "arn:rt"
        db.add(agent)
        db.flush()
        if state == "prior_failed":
            db.add(Deployment(workspace_id="default", agent_id=agent.id, status="failed"))
            db.flush()
        before = deepcopy(agent.spec)
        create_deployment(db, agent, mode="update" if state == "update" else "create")
        assert agent.spec == before
        assert db.query(SpecSnapshot).one().spec == before


@pytest.fixture
def no_deploy(monkeypatch):
    launched = []
    monkeypatch.setattr(agents_router, "start_deploy_async", launched.append)
    return launched


def seed_runtime(config=None):
    with SessionLocal() as db:
        raw = spec_input(**({"runtime_config": config} if config is not None else {}))
        row = Agent(workspace_id="default", name=raw["name"], method=raw["method"], spec=raw,
                    status="active", resource_id="billing_rt-abcdefghij", arn="arn:rt", version="1")
        db.add(row)
        db.commit()
        return row.id


def test_api_create_default_and_invalid_config_refused_before_jobs(client, no_deploy):
    for config in [{**BASELINE, "minimum_memory_mib": "4096"}, {**CONSUMPTION, "typo": True}]:
        assert client.post("/api/agents", json=spec_input(runtime_config=config)).status_code == 422
    response = client.post("/api/agents", json=spec_input("harness", runtime_config=BASELINE))
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(Job).count() == 0
        assert db.query(Agent).count() == 0
    response = client.post("/api/agents", json=spec_input())
    assert response.status_code == 202, response.text
    assert response.json()["agent"]["spec"]["runtime_config"] == CONSUMPTION
    assert len(no_deploy) == 1


@pytest.mark.parametrize("incoming", ["missing", "null", "consumption"])
def test_republish_distinguishes_omission_from_consumption(client, no_deploy, incoming):
    agent_id = seed_runtime(BASELINE)
    extra = {} if incoming == "missing" else {
        "runtime_config": None if incoming == "null" else CONSUMPTION,
    }
    response = client.post(f"/api/agents/{agent_id}/redeploy",
                           json=spec_input(system_prompt="Edited.", **extra))
    assert response.status_code == 202, response.text
    expected = CONSUMPTION if incoming == "consumption" else BASELINE
    assert response.json()["agent"]["spec"]["runtime_config"] == expected
    with SessionLocal() as db:
        assert db.query(SpecSnapshot).one().spec["runtime_config"] == expected
        assert db.query(Job).one().payload["mode"] == "update"


def test_legacy_republish_keeps_stored_spec_unconfigured(client, no_deploy):
    agent_id = seed_runtime()
    response = client.post(f"/api/agents/{agent_id}/redeploy", json=spec_input())
    assert response.status_code == 202, response.text
    assert "runtime_config" not in response.json()["agent"]["spec"]
    with SessionLocal() as db:
        assert "runtime_config" not in db.query(SpecSnapshot).one().spec


@pytest.mark.parametrize("snapshot_config", [BASELINE, CONSUMPTION, None])
@pytest.mark.parametrize("current_config", [BASELINE, CONSUMPTION, None])
def test_snapshot_rollback_restores_explicit_choice_but_never_guesses_legacy_settings(
    client, no_deploy, snapshot_config, current_config
):
    from app.services.snapshots import record_snapshot

    agent_id = seed_runtime(current_config)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        saved = deepcopy(agent.spec)
        old = spec_input(**({"runtime_config": snapshot_config} if snapshot_config else {}))
        agent.spec = old
        snap = record_snapshot(db, agent)
        snap.aws_version = "1"
        agent.spec = saved
        db.commit()
    response = client.post(f"/api/agents/{agent_id}/snapshots/1/rollback")
    assert response.status_code == 202, response.text
    expected = snapshot_config or current_config
    actual = response.json()["agent"]["spec"]
    if expected is None:
        assert "runtime_config" not in actual
    else:
        assert actual["runtime_config"] == expected
    with SessionLocal() as db:
        assert db.query(SpecSnapshot).filter_by(seq=2).one().spec == actual
        assert db.query(Job).one().payload["mode"] == "update"


@pytest.mark.parametrize("config", [BASELINE, None])
def test_inbound_auth_switch_keeps_explicit_or_legacy_runtime_settings(client, no_deploy, config):
    agent_id = seed_runtime(config)
    response = client.post(f"/api/agents/{agent_id}/inbound-auth",
                           json={"inbound_auth": {"mode": "iam"}})
    assert response.status_code == 202, response.text
    actual = response.json()["agent"]["spec"]
    assert actual["inbound_auth"] == {"mode": "iam", "jwt": None}
    if config is None:
        assert "runtime_config" not in actual
    else:
        assert actual["runtime_config"] == config
    with SessionLocal() as db:
        assert db.query(SpecSnapshot).one().spec == actual
