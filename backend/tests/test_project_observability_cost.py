"""Observability and cost per project (projects design §10.8, R10-obs): span resource
attributes and `launchpad:*` tags on an isolated project's runtimes and harnesses,
application inference profiles per (project, model), dashboards filtered by the span
attribute, and cost attributed by span / profile / agent. AWS is stubbed throughout;
a shared project (and so a single-project workspace) keeps today's requests."""

from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.project_scope import project_context
from app.deployer import harness as harness_deployer
from app.deployer.environment import runtime_environment
from app.models.ledger import Agent
from app.models.project_isolation import ProjectResource
from app.models.projects import Project
from app.schemas.agent import AgentSpec
from app.services import costs as cost_service
from app.services import inference_profiles as ip
from app.services import observability, project_telemetry
from app.services.agentcore import runtime as rt
from app.services.observability import ProjectAgents
from app.services.workspace import WorkspaceContext
from tests.conftest import TEST_DEPARTMENT, TEST_PROJECT
from tests.test_project_scope import _project  # noqa: F401

WS = DEFAULT_WORKSPACE_ID
PRICES = {"claude-sonnet": {"input": 3.0, "output": 15.0}}
MODEL = "us.anthropic.claude-sonnet-4-v1:0"
PROFILE_ARN = "arn:aws:bedrock:us-west-2:111122223333:application-inference-profile/abc123xyz"


@pytest.fixture(autouse=True)
def _fresh_profiles():
    ip.invalidate()
    observability.reset_cache()
    yield
    ip.invalidate()
    observability.reset_cache()


@pytest.fixture
def ctx() -> WorkspaceContext:
    return WorkspaceContext(account_id="111122223333", region="us-west-2", resources={},
                            id=WS)


def _isolate(pid: str) -> None:
    _project(pid)
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": "isolated"}}
        db.commit()
    finally:
        db.close()


def _runtime(name: str, pid: str | None) -> Agent:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method="harness", status="active",
                        spec={"name": name, "method": "harness", "model_id": MODEL,
                              "system_prompt": "hi"},
                        resource_id=f"{name}_rt-AbC123")
            db.add(row)
            db.commit()
            db.refresh(row)
            db.expunge(row)
            return row
    finally:
        db.close()


def _record_profile(pid: str = "alpha", model: str = MODEL, arn: str = PROFILE_ARN) -> None:
    db = SessionLocal()
    try:
        db.add(ProjectResource(project_id=pid, workspace_id=WS,
                               kind=f"inference_profile:{model}", resource_id="abc123xyz",
                               arn=arn, status="ready", detail={"model": model}))
        db.commit()
    finally:
        db.close()


# ── span resource attributes ──────────────────────────────────────────────────


def test_platform_attributes_go_first_and_a_spec_cannot_claim_a_project():
    merged = project_telemetry.merge_otel_resource_attributes(
        "team=search,launchpad.project=beta,deployment.environment=prod",
        project_telemetry.resource_attributes("alpha", "a1", WS),
    )
    assert merged.startswith("launchpad.project=alpha,launchpad.agent=a1,launchpad.workspace=")
    assert "launchpad.project=beta" not in merged
    assert merged.endswith("team=search,deployment.environment=prod")
    assert project_telemetry.parse_otel_resource_attributes(merged)["launchpad.project"] == "alpha"


def test_a_shared_project_deploys_exactly_as_before():
    _project("alpha")  # shared
    spec = AgentSpec(name="plain", method="zip_runtime", model_id=MODEL, system_prompt="x",
                     env={"A": "1"})
    with project_context("alpha"):
        env = runtime_environment(spec, {}, agent_id="a1")
    with project_context(TEST_PROJECT):
        assert runtime_environment(spec, {}) == env
    assert project_telemetry.ENV_OTEL_RESOURCE_ATTRIBUTES not in env
    assert project_telemetry.tags_from_environment(env) == {}


def test_an_isolated_projects_runtime_carries_its_project():
    _isolate("alpha")
    spec = AgentSpec(name="iso", method="zip_runtime", model_id=MODEL, system_prompt="x",
                     env={"OTEL_RESOURCE_ATTRIBUTES": "team=search"})
    with project_context("alpha"):
        env = runtime_environment(spec, {}, agent_id="a1", workspace_id=WS)
    attrs = project_telemetry.parse_otel_resource_attributes(env["OTEL_RESOURCE_ATTRIBUTES"])
    assert attrs == {"launchpad.project": "alpha", "launchpad.agent": "a1",
                     "launchpad.workspace": WS, "team": "search"}
    assert project_telemetry.tags_from_environment(env) == {
        "launchpad:project": "alpha", "launchpad:agent-id": "a1", "launchpad:workspace": WS,
    }


# ── resource tags on the runtime ─────────────────────────────────────────────


class _Control:
    def __init__(self, tag_error: Exception | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.tag_error = tag_error

    def create_agent_runtime(self, **kw):
        self.calls.append(("create", kw))
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt/rt-1"}

    def update_agent_runtime(self, **kw):
        self.calls.append(("update", kw))
        return {"agentRuntimeArn": "arn:rt/rt-1", "agentRuntimeVersion": "2"}

    def tag_resource(self, **kw):
        self.calls.append(("tag", kw))
        if self.tag_error:
            raise self.tag_error


def _env(pid: str = "alpha") -> dict[str, str]:
    return {"OTEL_RESOURCE_ATTRIBUTES": project_telemetry.merge_otel_resource_attributes(
        None, project_telemetry.resource_attributes(pid, "a1", WS))}


def test_create_runtime_tags_from_the_span_attributes():
    control = _Control()
    rt.create_code_runtime(control, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", environment=_env())
    rt.create_container_runtime(control, runtime_name="n", container_uri="u", role_arn="r",
                                environment=_env(), tags={"cost-center": "42"})
    rt.create_code_runtime(control, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", environment={"A": "1"})
    first, second, plain = (kw for _, kw in control.calls)
    assert first["tags"] == {"launchpad:project": "alpha", "launchpad:agent-id": "a1",
                             "launchpad:workspace": WS, "launchpad:managed-by": "launchpad"}
    assert second["tags"]["cost-center"] == "42"
    # cost tags (cost-tags D-1): every runtime is tagged — here no project is current
    assert plain["tags"] == {"launchpad:project": "shared",
                             "launchpad:managed-by": "launchpad"}


def test_a_republish_tags_the_runtime_and_a_refusal_never_fails_it():
    control = _Control()
    rt.update_code_runtime(control, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                           role_arn="r", environment=_env())
    assert control.calls[-1] == ("tag", {"resourceArn": "arn:rt/rt-1", "tags": {
        "launchpad:project": "alpha", "launchpad:agent-id": "a1", "launchpad:workspace": WS,
        "launchpad:managed-by": "launchpad"}})
    assert "tags" not in control.calls[0][1]  # UpdateAgentRuntime has no tags member

    denied = _Control(ClientError({"Error": {"Code": "AccessDenied"}}, "TagResource"))
    out = rt.update_container_runtime(denied, runtime_id="rt-1", container_uri="u",
                                      role_arn="r", environment=_env())
    assert out["agentRuntimeVersion"] == "2"

    plain = _Control()
    with project_context("alpha"):
        rt.update_code_runtime(plain, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                               role_arn="r", environment={"A": "1"},
                               tags={"launchpad:agent-id": "a1"})
    # a shared project's runtime catches up on its cost tags too (cost-tags D-1)
    assert plain.calls[-1] == ("tag", {"resourceArn": "arn:rt/rt-1", "tags": {
        "launchpad:project": "alpha", "launchpad:managed-by": "launchpad",
        "launchpad:agent-id": "a1"}})


# ── application inference profiles ───────────────────────────────────────────


class _Bedrock:
    def __init__(self, existing: list[dict[str, Any]] | None = None, fail: bool = False):
        self.existing = existing or []
        self.fail = fail
        self.created: list[dict[str, Any]] = []
        self.listed = 0

    def list_inference_profiles(self, **kw):
        assert kw["typeEquals"] == "APPLICATION"
        self.listed += 1
        return {"inferenceProfileSummaries": self.existing}

    def create_inference_profile(self, **kw):
        if self.fail:
            raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "Create")
        self.created.append(kw)
        return {"inferenceProfileArn": PROFILE_ARN, "status": "ACTIVE"}


def test_profile_names_fit_the_service_pattern():
    assert ip.profile_name("alpha", MODEL) == "lp-alpha-us-anthropic-claude-sonnet-4-v1-0"
    long = ip.profile_name("a" * 32, "anthropic." + "x" * 80)
    assert len(long) <= 64 and long.startswith("lp-aaaa")
    assert ip.profile_name("a" * 32, "anthropic." + "x" * 80) == long  # deterministic


def test_a_profile_is_created_tagged_recorded_and_then_reused(ctx):
    _isolate("alpha")
    bedrock = _Bedrock()
    db = SessionLocal()
    try:
        made = ip.ensure_profile(db, bedrock, project_id="alpha", workspace=ctx,
                                 model_id=MODEL)
        again = ip.ensure_profile(db, bedrock, project_id="alpha", workspace=ctx,
                                  model_id=MODEL)
    finally:
        db.close()
    assert made.arn == again.arn == PROFILE_ARN
    [request] = bedrock.created
    assert bedrock.listed == 1  # the second call answered from the ledger
    assert request["modelSource"] == {
        "copyFrom": f"arn:aws:bedrock:us-west-2:111122223333:inference-profile/{MODEL}"}
    assert {"key": "launchpad:project", "value": "alpha"} in request["tags"]
    assert {"key": "launchpad:workspace", "value": WS} in request["tags"]
    db = SessionLocal()
    try:
        row = db.get(ProjectResource, ("alpha", WS, f"inference_profile:{MODEL}"))
        assert row.status == "ready" and row.resource_id == "abc123xyz"
        assert row.detail["model"] == MODEL
    finally:
        db.close()


def test_a_profile_left_by_an_earlier_run_is_adopted(ctx):
    bedrock = _Bedrock(existing=[{"inferenceProfileName": ip.profile_name("alpha", "m.x"),
                                  "inferenceProfileArn": PROFILE_ARN,
                                  "inferenceProfileId": "abc123xyz"}])
    db = SessionLocal()
    try:
        found = ip.ensure_profile(db, bedrock, project_id="alpha", workspace=ctx,
                                  model_id="m.x")
    finally:
        db.close()
    assert found.arn == PROFILE_ARN and not bedrock.created
    assert ip.source_arn("m.x", ctx) == "arn:aws:bedrock:us-west-2::foundation-model/m.x"


def test_the_model_id_changes_only_behind_the_setting_and_isolation(monkeypatch, ctx):
    _project("shared")
    _isolate("alpha")
    bedrock = _Bedrock()
    db = SessionLocal()
    try:
        assert ip.model_id_for(db, project_id="alpha", workspace=ctx, model_id=MODEL,
                               bedrock=bedrock) == MODEL  # setting off (the default)
        monkeypatch.setattr(ip, "enabled", lambda: True)
        assert ip.model_id_for(db, project_id="shared", workspace=ctx, model_id=MODEL,
                               bedrock=bedrock) == MODEL
        assert ip.model_id_for(db, project_id="alpha", workspace=ctx, model_id=MODEL,
                               bedrock=bedrock) == PROFILE_ARN
        with pytest.raises(ip.InferenceProfileError, match="AccessDeniedException"):
            ip.model_id_for(db, project_id="alpha", workspace=ctx, model_id="other.m",
                            bedrock=_Bedrock(fail=True))
    finally:
        db.close()


# ── the harness deploy ───────────────────────────────────────────────────────


def _stage_ctx(ctx: WorkspaceContext, logs: list[str]) -> SimpleNamespace:
    return SimpleNamespace(workspace=ctx, session=SessionLocal, log=logs.append, scratch={})


def test_the_harness_request_of_an_isolated_project(monkeypatch, ctx):
    _isolate("alpha")
    agent = _runtime("iso", "alpha")
    params = {"harnessName": "iso", "environmentVariables": {"A": "1"},
              "model": {"bedrockModelConfig": {"modelId": MODEL, "apiFormat": "converse"}}}
    bedrock = _Bedrock()
    monkeypatch.setattr(ip, "enabled", lambda: True)
    monkeypatch.setattr(WorkspaceContext, "client", lambda self, service, **kw: bedrock)
    logs: list[str] = []
    out = harness_deployer._attribute_to_project(_stage_ctx(ctx, logs), agent, params)
    assert out["tags"] == {"launchpad:project": "alpha", "launchpad:agent-id": agent.id,
                           "launchpad:workspace": WS, "launchpad:managed-by": "launchpad",
                           "launchpad:department": TEST_DEPARTMENT}
    assert out["environmentVariables"]["A"] == "1"
    assert "launchpad.project=alpha" in out["environmentVariables"]["OTEL_RESOURCE_ATTRIBUTES"]
    assert out["model"]["bedrockModelConfig"] == {"modelId": PROFILE_ARN,
                                                  "apiFormat": "converse"}
    assert params["model"]["bedrockModelConfig"]["modelId"] == MODEL  # input untouched
    assert any("inference profile" in line for line in logs)


def test_a_shared_projects_harness_request_only_gains_its_cost_tags(ctx):
    _project("alpha")
    agent = _runtime("plain", "alpha")
    params = {"harnessName": "plain", "model": {"bedrockModelConfig": {"modelId": MODEL}}}
    out = harness_deployer._attribute_to_project(_stage_ctx(ctx, []), agent, params)
    assert {k: v for k, v in out.items() if k != "tags"} == params  # no span attributes
    assert out["tags"] == {"launchpad:project": "alpha", "launchpad:workspace": WS,
                           "launchpad:agent-id": agent.id,
                           "launchpad:managed-by": "launchpad",
                           "launchpad:department": TEST_DEPARTMENT}


def test_the_harness_is_tagged_after_an_update_best_effort():
    control = _Control()
    harness_deployer._tag_harness(control, "arn:h", {"launchpad:project": "alpha"}, print)
    assert control.calls == [("tag", {"resourceArn": "arn:h",
                                      "tags": {"launchpad:project": "alpha"}})]
    logs: list[str] = []
    denied = _Control(RuntimeError("no"))
    harness_deployer._tag_harness(denied, "arn:h", {"launchpad:project": "alpha"}, logs.append)
    assert logs and "failed" in logs[0]
    harness_deployer._tag_harness(control, "arn:h", None, print)
    assert len(control.calls) == 1


# ── observability ────────────────────────────────────────────────────────────


def test_an_isolated_projects_dashboard_filters_on_the_span_attribute():
    _isolate("alpha")
    allow = ProjectAgents(SessionLocal(), WS, "alpha")
    assert allow.service_filter() == '\n| filter resource.attributes.launchpad.project = "alpha"'
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    line = ProjectAgents(SessionLocal(), WS, "alpha").service_filter()
    assert line.startswith('\n| filter resource.attributes.launchpad.project = "alpha" or ')
    assert "not ispresent(resource.attributes.launchpad.project)" in line
    assert "mine_rt" in line and "theirs_rt" not in line
    # a shared project keeps the service-name filter
    shared = ProjectAgents(SessionLocal(), WS, TEST_PROJECT).service_filter()
    assert shared.startswith("\n| filter resource.attributes.service.name like /")


def test_rows_stamped_with_a_project_are_decided_by_it():
    _isolate("alpha")
    _runtime("mine", "alpha")
    rows = [
        {"project": "alpha", "service": "unknown.DEFAULT", "session_id": None},
        # stamped with another project: not ours even on our runtime's name
        {"project": "beta", "service": "mine_rt.DEFAULT", "session_id": None},
        {"service": "mine_rt.DEFAULT", "session_id": None},  # unstamped: the resolver
    ]
    kept = ProjectAgents(SessionLocal(), WS, "alpha").keep_rows(rows)
    assert kept == [rows[0], rows[2]]
    allow = ProjectAgents(SessionLocal(), WS, "alpha")
    assert allow.keeps_trace({"meta": {"project": "alpha", "service": "x"}})
    assert not allow.keeps_trace({"meta": {"project": "beta", "service": "mine_rt.DEFAULT"}})
    assert not allow.keeps_session("s" * 40, {"traces": [{"project": "beta"}]})


def test_trace_rows_carry_the_span_project_only_when_present():
    stamped = observability._trace_row({"traceId": "t", "project": "alpha"}, None, str)
    plain = observability._trace_row({"traceId": "t"}, None, str)
    bogus = observability._trace_row({"traceId": "t", "project": "Not A Slug"}, None, str)
    assert stamped["project"] == "alpha"
    assert "project" not in plain and "project" not in bogus
    assert "launchpad.project" in observability.q_trace_aggregates()
    assert "launchpad.project" in observability.q_session_aggregates()


def test_a_profile_arn_is_priced_as_its_model():
    _record_profile()
    assert observability.estimate_cost(PROFILE_ARN, 1e6, 0, prices=PRICES) == 3.0
    assert observability.estimate_cost(
        "arn:aws:bedrock:us-west-2:1:application-inference-profile/unknown", 1e6, 0,
        prices=PRICES) is None


# ── cost by project ──────────────────────────────────────────────────────────


def _stub_costs(monkeypatch, by_service, by_session=()):
    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    # the per-model query groups by service + span project too: the same rows serve it
    monkeypatch.setattr(cost_service, "run_insights_queries", lambda q, hours, logs=None, **k: {
        "by_service": list(by_service), "by_session": list(by_session),
        "by_model": list(by_service)})
    monkeypatch.setattr(cost_service, "get_settings",
                        lambda: SimpleNamespace(model_prices=PRICES))
    monkeypatch.setattr(cost_service, "cached", lambda key, force, build: build())


def test_cost_is_attributed_by_span_then_profile_then_agent(monkeypatch, ctx):
    _isolate("alpha")
    _project("beta")
    mine = _runtime("mine", "alpha")
    theirs = _runtime("theirs", "beta")
    _record_profile("alpha")
    _stub_costs(monkeypatch, [
        # the same runtime before (unstamped) and after (stamped) isolation: one row
        {"service": "mine_rt.DEFAULT", "span_project": "alpha", "tokens_in": "1000000",
         "model": "claude-sonnet"},
        {"service": "mine_rt.DEFAULT", "span_project": "", "tokens_in": "1000000",
         "model": "claude-sonnet"},
        # an unknown runtime calling alpha's inference profile
        {"service": "byoc.DEFAULT", "tokens_out": "1000000", "model": PROFILE_ARN},
        {"service": "theirs_rt.DEFAULT", "tokens_in": "1000000", "model": "claude-sonnet"},
        {"service": "ghost.DEFAULT", "tokens_in": "1000000", "model": "claude-sonnet"},
    ], [
        {"session_id": "o" * 40, "tokens_in": "1000000", "model": "claude-sonnet",
         "span_project": "alpha"},
    ])
    db = SessionLocal()
    try:
        whole = cost_service.cost_report(db, ctx)
        alpha = cost_service.cost_report(db, ctx, project="alpha")
        beta = cost_service.cost_report(db, ctx, project="beta")
    finally:
        db.close()
    rows = {(r["service"], r["project_source"]): r for r in alpha["by_agent"]}
    assert rows[("mine_rt.DEFAULT", "span")]["agent_id"] == mine.id
    assert rows[("mine_rt.DEFAULT", "span")]["tokens"] == 2_000_000
    assert rows[("byoc.DEFAULT", "inference_profile")]["est_cost_usd"] == pytest.approx(15.0)
    assert alpha["total_est_cost_usd"] == pytest.approx(3 + 3 + 15)
    assert alpha["unpriced_models"] == []
    assert [r["actor"] for r in alpha["by_actor"]] == ["—"]  # stamped, no console identity
    assert [r["agent_id"] for r in beta["by_agent"]] == [theirs.id]
    by_project = {r["project_id"]: r for r in whole["by_project"]}
    assert by_project["alpha"]["est_cost_usd"] == pytest.approx(21.0)
    assert by_project["beta"]["est_cost_usd"] == pytest.approx(3.0)
    assert by_project[None]["agents"] == 1  # the ghost: attributed to no project
    assert [r["project_id"] for r in alpha["by_project"]] == ["alpha"]


def test_month_to_date_by_project_uses_the_same_attribution(monkeypatch, ctx):
    _isolate("alpha")
    _record_profile("alpha")
    _stub_costs(monkeypatch, [
        {"service": "x.DEFAULT", "span_project": "alpha", "tokens_in": "1000000",
         "model": "claude-sonnet"},
        {"service": "y.DEFAULT", "tokens_out": "1000000", "model": PROFILE_ARN},
    ])
    db = SessionLocal()
    try:
        assert cost_service.month_to_date_usd(db, ctx, project="alpha") == pytest.approx(18.0)
    finally:
        db.close()


def test_the_cost_query_groups_by_the_span_project():
    query = cost_service.q_cost_by_service()
    assert 'coalesce(resource.attributes.launchpad.project, "") as span_project' in query
    assert "by resource.attributes.service.name as service, span_project" in query
