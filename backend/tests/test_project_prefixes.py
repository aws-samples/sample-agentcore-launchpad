"""Project prefixes, registry metadata and inference profiles for code runtimes
(projects design §10.9, §10.10, R10-misc; §10.8 leftovers of R10-obs).

An isolated project's agents keep their artifacts under `projects/<pid>/`, build into
`launchpad-agents/<pid>` once the project's repository is ready in the workspace,
carry the project on their A2A record, and — behind `project_inference_profiles` —
run their code with the project's application inference profile, which their role
may invoke. AWS is stubbed throughout; a shared project (and so every single-project
workspace) keeps today's keys, repository, record and policy exactly.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import byoc as byoc_deployer
from app.deployer import container as container_deployer
from app.deployer import model_profile
from app.deployer import zip_runtime as zip_deployer
from app.models.project_isolation import ProjectResource
from app.models.projects import Project
from app.schemas.agent import AgentSpec
from app.services import (
    agent_iam,
    inference_profiles,
    project_isolation,
    project_storage,
    registry_console,
)
from app.services import workspace_bootstrap as wb
from app.services.agentcore import registry as reg
from app.services.workspace import WorkspaceContext
from app.templates.claude_sdk_agent import render_main_py as render_claude
from app.templates.strands_a2a_agent import render_a2a_main_py
from app.templates.strands_agent import render_main_py as render_strands
from tests.test_project_scope import _project

WS = DEFAULT_WORKSPACE_ID
ACCOUNT = "111122223333"
REGION = "us-west-2"
MODEL = "us.anthropic.claude-sonnet-4-v1:0"
PROFILE_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/abc123xyz"
SPOKE = Path(__file__).resolve().parents[2] / "infra" / "spoke"


def _err(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


class FakeWorkspace(WorkspaceContext):
    """A workspace whose clients are the stubs it was built with."""

    def client(self, service: str, cache_token: str | None = None, **cfg: Any) -> Any:
        return self.__dict__["_clients"][service]


def _ws(**clients: Any) -> FakeWorkspace:
    # a recorded profile is found before any Bedrock call
    clients.setdefault("bedrock", object())
    ws = FakeWorkspace(
        account_id=ACCOUNT, region=REGION, id=WS,
        resources={"artifacts_bucket": "bkt", "ecr_repo": "launchpad-agents",
                   "codebuild_project": "launchpad-agent-builder"},
    )
    object.__setattr__(ws, "_clients", clients)  # frozen dataclass
    return ws


def _isolate(pid: str) -> None:
    _project(pid)
    db = SessionLocal()
    try:
        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": "isolated"}}
        db.commit()
    finally:
        db.close()


def _record(pid: str, kind: str, resource_id: str, status: str = "ready",
            arn: str | None = None) -> None:
    db = SessionLocal()
    try:
        db.add(ProjectResource(project_id=pid, workspace_id=WS, kind=kind,
                               resource_id=resource_id, arn=arn, status=status))
        db.commit()
    finally:
        db.close()


def _agent(name: str = "iso", pid: str | None = "alpha", method: str = "zip_runtime",
           **spec: Any) -> SimpleNamespace:
    return SimpleNamespace(id=f"{name}-id", name=name, project_id=pid, method=method,
                           version="2", workspace_id=WS, arn="",
                           spec={"name": name, "method": method, **spec})


@pytest.fixture(autouse=True)
def _fresh_profiles():
    inference_profiles.invalidate()
    yield
    inference_profiles.invalidate()


# ── S3 keys ────────────────────────────────────────────────────────────────────


def test_a_shared_projects_keys_are_todays():
    _project("alpha")
    agent = _agent()
    assert zip_deployer.package_key(agent) == "agents/iso/deployment_package.zip"
    assert byoc_deployer.package_key(agent) == "agents/iso/byoc_package.zip"
    assert project_storage.artifact_key(None, "builds/iso/source.zip") == "builds/iso/source.zip"


def test_an_isolated_projects_keys_take_its_prefix():
    _isolate("alpha")
    agent = _agent()
    assert zip_deployer.package_key(agent) == "projects/alpha/agents/iso/deployment_package.zip"
    assert byoc_deployer.package_key(agent) == "projects/alpha/agents/iso/byoc_package.zip"
    assert project_storage.artifact_key("alpha", "builds/iso/source.zip") == (
        "projects/alpha/builds/iso/source.zip"
    )


# ── ECR repository ─────────────────────────────────────────────────────────────


def test_the_project_repository_is_used_only_once_it_is_ready():
    _isolate("alpha")
    ws = _ws()
    agent = _agent()
    assert container_deployer._image_ref(ws, agent)[1] == "launchpad-agents"
    _record("alpha", "ecr_repo", "launchpad-agents/alpha", status="provisioning")
    assert project_storage.ecr_repo(ws, "alpha") == "launchpad-agents"
    db = SessionLocal()
    try:
        db.get(ProjectResource, ("alpha", WS, "ecr_repo")).status = "ready"
        db.commit()
    finally:
        db.close()
    registry, repo, tag = container_deployer._image_ref(ws, agent)
    assert (repo, tag) == ("launchpad-agents/alpha", "iso-v2")
    assert project_storage.ecr_repos(ws, "alpha") == ["launchpad-agents/alpha",
                                                      "launchpad-agents"]


def test_a_shared_project_never_reads_a_project_repository():
    _project("alpha")
    _record("alpha", "ecr_repo", "launchpad-agents/alpha")  # left by an earlier isolation
    ws = _ws()
    assert container_deployer._image_ref(ws, _agent())[1] == "launchpad-agents"
    assert project_storage.ecr_repos(ws, "alpha") == ["launchpad-agents"]


class _Ecr:
    def __init__(self, existing: bool = False):
        self.existing = existing
        self.created: list[dict[str, Any]] = []

    def describe_repositories(self, repositoryNames):  # noqa: N803 — boto3 casing
        if not self.existing:
            raise _err("RepositoryNotFoundException")
        name = repositoryNames[0]
        return {"repositories": [{"repositoryName": name, "repositoryArn": f"arn:ecr/{name}",
                                  "repositoryUri": f"uri/{name}"}]}

    def create_repository(self, **kwargs):
        self.created.append(kwargs)
        name = kwargs["repositoryName"]
        return {"repository": {"repositoryArn": f"arn:ecr/{name}",
                               "repositoryUri": f"uri/{name}"}}


class _CodeBuild:
    def __init__(self, role: str = f"arn:aws:iam::{ACCOUNT}:role/launchpad-codebuild-role"):
        self.role = role

    def batch_get_projects(self, names):
        return {"projects": [{"name": names[0], "serviceRole": self.role}] if self.role
                else []}


class _Iam:
    def __init__(self, fail: str | None = None):
        self.fail = fail
        self.put: list[dict[str, Any]] = []

    def put_role_policy(self, **kwargs):
        if self.fail:
            raise _err(self.fail)
        self.put.append(kwargs)


def _provision_ctx(ws: FakeWorkspace, recorded: list) -> project_isolation.ProvisionContext:
    return project_isolation.ProvisionContext(
        project_id="alpha", workspace_id=WS, job_id="job-1", settings={},
        names=project_isolation.resource_names("alpha"), _record=recorded.append,
        _workspace=ws,
    )


def test_the_ecr_stage_makes_a_tagged_scanned_repository_and_widens_codebuild():
    ecr, iam = _Ecr(), _Iam()
    ws = _ws(ecr=ecr, codebuild=_CodeBuild(), iam=iam)
    recorded: list = []
    detail = project_storage.StorageProvisioner().ensure_ecr_repo(_provision_ctx(ws, recorded))
    assert detail == "launchpad-agents/alpha"
    (create,) = ecr.created
    assert create["repositoryName"] == "launchpad-agents/alpha"
    assert create["imageScanningConfiguration"] == {"scanOnPush": True}
    assert {"Key": "launchpad:project", "Value": "alpha"} in create["tags"]
    assert recorded[0].kind == "ecr_repo" and recorded[0].resource_id == "launchpad-agents/alpha"
    assert recorded[0].arn == "arn:ecr/launchpad-agents/alpha"
    (put,) = iam.put
    assert put["RoleName"] == "launchpad-codebuild-role"
    assert put["PolicyName"] == "launchpad-agent-builder"
    assert "repository/launchpad-agents/*" in put["PolicyDocument"]
    # the name agrees with the boundary's ECR statement and the design's names
    assert project_isolation.resource_names("alpha").ecr_repo == "launchpad-agents/alpha"


def test_the_ecr_stage_adopts_an_existing_repository_and_reports_an_unwidened_role():
    ecr = _Ecr(existing=True)
    ws = _ws(ecr=ecr, codebuild=_CodeBuild(), iam=_Iam(fail="AccessDenied"))
    recorded: list = []
    logs: list[str] = []
    ctx = _provision_ctx(ws, recorded)
    ctx.log = logs.append
    detail = project_storage.StorageProvisioner().ensure_ecr_repo(ctx)
    assert not ecr.created and recorded[0].resource_id == "launchpad-agents/alpha"
    assert "not widened" in detail
    assert any("AccessDenied" in line for line in logs)


def test_the_s3_prefix_stage_records_the_prefix():
    recorded: list = []
    detail = project_storage.StorageProvisioner().ensure_s3_prefix(
        _provision_ctx(_ws(), recorded))
    assert detail == "s3://bkt/projects/alpha/"
    assert recorded[0].kind == "s3_prefix"
    assert recorded[0].arn == "arn:aws:s3:::bkt/projects/alpha/"


def test_the_storage_provisioner_is_registered():
    assert isinstance(project_isolation.get_provisioners().storage,
                      project_storage.StorageProvisioner)


def test_the_bootstrap_codebuild_grant_covers_the_project_repositories():
    arn = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents"
    policy = wb.codebuild_policy(ACCOUNT, REGION, "bkt", arn)
    push = next(s for s in policy["Statement"] if s["Sid"] == "EcrPullPush")
    assert push["Resource"] == [arn, f"{arn}/*"]


def test_a_workspace_without_a_codebuild_project_is_reported_not_widened():
    logs: list[str] = []
    ws = _ws(codebuild=_CodeBuild(role=""), iam=_Iam())
    assert wb.ensure_codebuild_project_repos(ws, logs.append) is False
    assert logs and "not found" in logs[0]


# ── the execution role ─────────────────────────────────────────────────────────


def _role_ctx(**kw: Any) -> agent_iam.RoleContext:
    return agent_iam.RoleContext(
        account_id=ACCOUNT, region=REGION, artifacts_bucket="bkt",
        ecr_repo_arn=f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents", **kw)


def _statement(policy: dict, sid: str) -> dict:
    return next(s for s in policy["Statement"] if s["Sid"] == sid)


def test_the_role_may_invoke_the_projects_profile_and_its_models():
    spec = AgentSpec(name="iso", method="zip_runtime", model_id=MODEL, system_prompt="x")
    plain = agent_iam.policy_document(spec, _role_ctx())
    profiled = agent_iam.policy_document(spec, _role_ctx(inference_profile_arns=(PROFILE_ARN,)))
    before = _statement(plain, "BedrockModels")["Resource"]
    after = _statement(profiled, "BedrockModels")["Resource"]
    assert after == [*before, PROFILE_ARN]
    assert any(":foundation-model/" in r for r in after)  # the routed models stay


def test_the_role_pulls_from_the_project_repository_and_the_shared_one():
    spec = AgentSpec(name="iso", method="container", model_id=MODEL, system_prompt="x")
    project = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents/alpha"
    shared = f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents"
    ctx = _role_ctx()
    ctx = agent_iam.RoleContext(**{**ctx.__dict__, "ecr_repo_arn": project,
                                   "extra_ecr_repo_arns": (shared,)})
    assert _statement(agent_iam.policy_document(spec, ctx), "EcrPull")["Resource"] == [
        project, shared]


def test_role_context_names_the_project_repository_once_ready():
    _isolate("alpha")
    ws = _ws()
    assert agent_iam.role_context(ws, "alpha") == agent_iam.role_context(ws)
    _record("alpha", "ecr_repo", "launchpad-agents/alpha")
    ctx = agent_iam.role_context(ws, "alpha")
    assert ctx.ecr_repo_arn.endswith("repository/launchpad-agents/alpha")
    assert ctx.extra_ecr_repo_arns == (
        f"arn:aws:ecr:{REGION}:{ACCOUNT}:repository/launchpad-agents",)


def test_provisioning_grants_the_profile_only_behind_the_setting(monkeypatch):
    _isolate("alpha")
    captured: list[agent_iam.RoleContext] = []
    monkeypatch.setattr(agent_iam, "ensure_role",
                        lambda iam, agent, spec, ctx, log, boundary=None:
                        captured.append(ctx) or "arn:role")
    monkeypatch.setattr(agent_iam, "_project_boundary", lambda *a, **k: None)
    spec = AgentSpec(name="iso", method="zip_runtime", model_id=MODEL, system_prompt="x")
    settings = SimpleNamespace(per_agent_execution_roles=True)
    agent = _agent()
    agent_iam.provision_execution_role(agent, spec, settings, _ws(), iam=object())
    assert captured[-1].inference_profile_arns == ()
    monkeypatch.setattr(inference_profiles, "enabled", lambda: True)
    _record("alpha", f"inference_profile:{MODEL}", "abc123xyz", arn=PROFILE_ARN)
    agent_iam.provision_execution_role(agent, spec, settings, _ws(), iam=object())
    assert captured[-1].inference_profile_arns == (PROFILE_ARN,)


# ── the model id of code runtimes ─────────────────────────────────────────────


def test_which_models_a_profile_stands_in_for():
    def ids(**kw: Any) -> list[str]:
        base = {"name": "agent-a", "system_prompt": "x", "model_id": MODEL}
        return model_profile.profiled_model_ids(AgentSpec(**{**base, **kw}))

    assert ids(method="zip_runtime") == [MODEL]
    assert ids(method="container") == [MODEL]
    assert ids(method="harness") == [MODEL]
    assert ids(method="zip_runtime", model_source="mantle",
               model_id="openai.gpt-5.6-sol") == []
    assert ids(method="studio") == []


def test_code_runtimes_get_the_profile_only_when_enabled_and_isolated(monkeypatch):
    _project("shared")
    _isolate("alpha")
    _record("alpha", f"inference_profile:{MODEL}", "abc123xyz", arn=PROFILE_ARN)
    spec = AgentSpec(name="iso", method="zip_runtime", model_id=MODEL, system_prompt="x")
    ws = _ws()
    assert model_profile.model_environment("alpha", spec, ws) == {}  # setting off
    monkeypatch.setattr(inference_profiles, "enabled", lambda: True)
    assert model_profile.model_environment("shared", spec, ws) == {}
    assert model_profile.model_environment(None, spec, ws) == {}
    logs: list[str] = []
    assert model_profile.model_environment("alpha", spec, ws, logs.append) == {
        "LAUNCHPAD_MODEL_ID": PROFILE_ARN}
    assert logs and PROFILE_ARN in logs[-1]
    # the harness swaps the id in its own request, not through the variable
    harness = AgentSpec(name="iso", method="harness", model_id=MODEL, system_prompt="x")
    assert model_profile.model_environment("alpha", harness, ws) == {}


@pytest.mark.parametrize("render,method", [
    (render_strands, "zip_runtime"),
    (render_a2a_main_py, "zip_runtime"),
    (render_claude, "container"),
])
def test_the_templates_read_the_profile_before_their_rendered_id(render, method, monkeypatch):
    kw = {"protocol": "a2a"} if render is render_a2a_main_py else {}
    code = render(AgentSpec(name="iso", method=method, model_id=MODEL, system_prompt="x",
                            **kw))
    rendered = f'MODEL_ID = "{MODEL}"\n'
    override = 'MODEL_ID = os.environ.get("LAUNCHPAD_MODEL_ID") or MODEL_ID\n'
    assert rendered in code and override in code
    assert code.index(rendered) < code.index(override)
    # what the two lines do at import
    snippet = "import os\n" + rendered + override
    scope: dict[str, Any] = {}
    monkeypatch.delenv("LAUNCHPAD_MODEL_ID", raising=False)
    exec(snippet, scope)  # noqa: S102 — two literal template lines
    assert scope["MODEL_ID"] == MODEL
    monkeypatch.setenv("LAUNCHPAD_MODEL_ID", PROFILE_ARN)
    exec(snippet, scope)  # noqa: S102
    assert scope["MODEL_ID"] == PROFILE_ARN


# ── the A2A record (§10.9) ────────────────────────────────────────────────────


class _Registry:
    def __init__(self, existing: dict[str, Any] | None = None, tag_fails: bool = False):
        self.existing = existing
        self.tag_fails = tag_fails
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def list_registry_records(self, **kwargs):
        return {"registryRecords": [self.existing] if self.existing else []}

    def create_registry_record(self, **kwargs):
        self.calls.append(("create", kwargs))
        return {"recordArn": "arn:reg/registry/r/record/rec-1", "status": "DRAFT"}

    def update_registry_record(self, **kwargs):
        self.calls.append(("update", kwargs))
        return {**(self.existing or {}), "recordArn": "arn:reg/record/rec-1"}

    def tag_resource(self, **kwargs):
        self.calls.append(("tag", kwargs))
        if self.tag_fails:
            raise RuntimeError("denied")


def _descriptors() -> dict[str, Any]:
    card = reg.build_a2a_card(name="iso", description="d", arn="arn:rt", method="zip_runtime")
    return reg.build_a2a_descriptors(card)


def test_the_card_names_the_project_only_when_given():
    plain = reg.build_a2a_card(name="iso", description="d", arn="arn:rt", method="zip_runtime")
    assert "launchpad.project" not in plain["metadata"]
    card = reg.build_a2a_card(name="iso", description="d", arn="arn:rt", method="zip_runtime",
                              project_id="alpha")
    assert card["metadata"]["launchpad.project"] == "alpha"
    assert reg.agent_card_diff(card, plain) == reg.agent_card_diff(plain, plain)


def test_a_new_record_is_created_with_the_tags():
    client = _Registry()
    reg.upsert_record(client, "reg-1", name="iso", description="d", descriptor_type="A2A",
                      descriptors=_descriptors(), tags={"launchpad:project": "alpha"})
    (kind, kwargs), = client.calls
    assert kind == "create" and kwargs["tags"] == {"launchpad:project": "alpha"}
    untagged = _Registry()
    reg.upsert_record(untagged, "reg-1", name="iso", description="d", descriptor_type="A2A",
                      descriptors=_descriptors())
    assert "tags" not in untagged.calls[0][1]


def test_a_refreshed_record_is_tagged_best_effort():
    existing = {"recordId": "rec-1", "name": "iso", "recordType": "AGENT",
                "recordArn": "arn:reg/record/rec-1"}
    client = _Registry(existing=existing, tag_fails=True)
    record, created = reg.upsert_record(
        client, "reg-1", name="iso", description="d", descriptor_type="A2A",
        descriptors=_descriptors(), tags={"launchpad:project": "alpha"})
    assert not created
    assert [c[0] for c in client.calls] == ["update", "tag"]
    assert client.calls[1][1] == {"resourceArn": "arn:reg/record/rec-1",
                                  "tags": {"launchpad:project": "alpha"}}
    plain = _Registry(existing=existing)
    reg.upsert_record(plain, "reg-1", name="iso", description="d", descriptor_type="A2A",
                      descriptors=_descriptors())
    assert [c[0] for c in plain.calls] == ["update"]


def _register(agent: SimpleNamespace, monkeypatch) -> _Registry:
    client = _Registry()
    monkeypatch.setattr(registry_console, "registry_control_client", lambda ws: client)
    ws = _ws()
    ws.resources["registry_id"] = "reg-1"
    registry_console.register_agent_record(agent, ws, auto_submit=False)
    return client


def test_an_isolated_projects_agent_record_carries_its_project(monkeypatch):
    _isolate("alpha")
    client = _register(_agent(), monkeypatch)
    (_, kwargs), = client.calls
    assert kwargs["tags"] == {"launchpad:project": "alpha", "launchpad:agent-id": "iso-id",
                              "launchpad:workspace": WS}
    card = json.loads(kwargs["descriptors"]["a2aAgentCard"]["data"])
    assert card["metadata"]["launchpad.project"] == "alpha"


def test_a_shared_projects_agent_record_is_unchanged(monkeypatch):
    _project("alpha")
    client = _register(_agent(), monkeypatch)
    (_, kwargs), = client.calls
    assert "tags" not in kwargs
    assert "launchpad.project" not in json.dumps(kwargs["descriptors"])


# ── the spoke templates ────────────────────────────────────────────────────────


def _template_statements(name: str) -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load((SPOKE / name).read_text(encoding="utf-8"))
    # the role's inline policy plus its attached customer managed policy
    documents = [
        policy["PolicyDocument"]
        for r in doc["Resources"].values() if r["Type"] == "AWS::IAM::Role"
        for policy in r["Properties"]["Policies"]
    ] + [r["Properties"]["PolicyDocument"]
         for r in doc["Resources"].values() if r["Type"] == "AWS::IAM::ManagedPolicy"]
    return {s["Sid"]: s for d in documents for s in d["Statement"]}


@pytest.mark.parametrize("name", ["launchpad-workspace-role.yaml",
                                  "launchpad-workspace-role-prod.yaml"])
def test_both_templates_let_the_hub_make_tagged_inference_profiles(name):
    statements = _template_statements(name)
    profiles = statements["BedrockApplicationInferenceProfiles"]
    assert {"bedrock:CreateInferenceProfile", "bedrock:TagResource"} <= set(profiles["Action"])
    assert any("application-inference-profile/*" in r["Fn::Sub"] for r in profiles["Resource"])
    assert statements["BedrockListInferenceProfiles"]["Action"] == [
        "bedrock:ListInferenceProfiles"]
    # tags on runtimes, harnesses, memories and registry records
    assert {"bedrock-agentcore:*", "agent-registry:*"} <= set(
        statements["AgentCoreAndRegistry"]["Action"])


def test_only_the_building_template_makes_project_repositories():
    repos = _template_statements("launchpad-workspace-role.yaml")["EcrProjectRepositories"]
    assert {"ecr:CreateRepository", "ecr:TagResource"} <= set(repos["Action"])
    assert repos["Resource"]["Fn::Sub"].endswith("repository/launchpad-agents/*")
    assert not {"ecr:PutImage", "ecr:UploadLayerPart"} & set(repos["Action"])
    assert "EcrProjectRepositories" not in _template_statements(
        "launchpad-workspace-role-prod.yaml")
