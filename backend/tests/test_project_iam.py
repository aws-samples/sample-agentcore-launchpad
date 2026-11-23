"""Phase 2 isolation, IAM (projects design §10.3, §10.15, R10-iam): the project
permissions boundary, its provisioner, and the agent roles that carry it.

The "IAM simulation" here is a small offline evaluator (`_allowed`) over the policy
documents — Allow statements, IAM wildcards (`*`, `?`) on actions and resources, and
the condition operators the boundary uses — run as §10.15's matrix: project A's
boundary must allow A's resources and implicitly deny B's memory, KB, S3 prefix,
runtime, M2M provider and its secret. A second check runs every grant the per-agent
identity policy can make through the boundary, so the ceiling never denies what an
agent legitimately does. `iam:SimulatePrincipalPolicy` against a live account is the
nightly integration test's job (§10.15), not this file's.
"""

import fnmatch
import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import project_context
from app.models.ledger import Agent
from app.models.project_isolation import KIND_BOUNDARY_POLICY, ProjectResource
from app.schemas.agent import AgentSpec
from app.services import agent_iam, cost_tags, project_iam
from app.services import project_isolation as svc
from app.services.project_iam import BoundaryInputs, boundary_policy_document
from app.services.workspace_iam import ForeignResourceError, project_boundary_policy_name
from tests.conftest import ws_ctx
from tests.test_project_scope import _project, app  # noqa: F401

ACCT = "111122223333"
REGION = "us-west-2"
WS = DEFAULT_WORKSPACE_ID
BUCKET = "lp-artifacts"
AC = f"arn:aws:bedrock-agentcore:{REGION}:{ACCT}"
KB_PROVIDER = f"{AC}:token-vault/default/oauth2credentialprovider/launchpad-kb-gw-m2m"


def _inputs(pid: str = "alpha", **over: Any) -> BoundaryInputs:
    base: dict[str, Any] = dict(
        project_id=pid, account_id=ACCT, region=REGION, artifacts_bucket=BUCKET,
        legacy_memory_id="launchpad_memory-abc", kb_gateway_provider_arn=KB_PROVIDER,
    )
    return BoundaryInputs(**{**base, **over})


# ── a minimal offline IAM evaluator ───────────────────────────────────────────


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def _match(pattern: str, value: str) -> bool:
    # IAM wildcards: `*` any run (including `/`), `?` one character; ARNs carry no `[`
    return fnmatch.fnmatchcase(value, pattern)


def _condition_holds(condition: dict[str, Any], context: dict[str, str]) -> bool:
    for op, keys in condition.items():
        for key, expected in keys.items():
            expected = _as_list(expected)
            present = key in context
            value = context.get(key, "")
            if op == "StringEquals":
                ok = present and value in expected
            elif op == "StringEqualsIfExists":
                ok = not present or value in expected
            elif op == "StringLike":
                ok = present and any(_match(e, value) for e in expected)
            elif op == "StringNotLike":
                ok = not present or not any(_match(e, value) for e in expected)
            else:  # pragma: no cover — an operator the boundary started using
                raise AssertionError(f"evaluator does not know {op}")
            if not ok:
                return False
    return True


def _allowed(policy: dict[str, Any], action: str, resource: str,
             context: dict[str, str] | None = None) -> bool:
    """Allow-only evaluation (the boundary has no Deny): implicit deny unless one
    statement matches action, resource and conditions."""
    context = context or {}
    for statement in policy["Statement"]:
        assert statement["Effect"] == "Allow"
        if not any(_match(a, action) for a in _as_list(statement["Action"])):
            continue
        if not any(_match(r, resource) for r in _as_list(statement["Resource"])):
            continue
        if _condition_holds(statement.get("Condition") or {}, context):
            return True
    return False


def _tag(pid: str) -> dict[str, str]:
    return {"aws:ResourceTag/launchpad:project": pid}


# ── the document: one assertion per capability (§10.3 table) ──────────────────


def test_names_follow_the_spoke_prefix_and_the_regional_rule():
    assert svc.resource_names("risk-desk").boundary_policy == "launchpad-project-risk-desk"
    assert project_boundary_policy_name("risk-desk", "us-west-2") == "launchpad-project-risk-desk"
    assert (project_boundary_policy_name("risk-desk", "us-east-1")
            == "launchpad-project-risk-desk-us-east-1")


def test_the_iam_provisioner_is_registered():
    assert isinstance(svc.get_provisioners().iam, project_iam.IamBoundaryProvisioner)


def test_the_boundary_fits_a_managed_policy():
    doc = boundary_policy_document(_inputs(kb_ids=("KB1", "KB2"),
                                           oauth2_connections=("crm",),
                                           api_key_connections=("weather",)))
    assert project_iam.policy_chars(doc) <= project_iam.MAX_POLICY_CHARS
    assert len({s["Sid"] for s in doc["Statement"]}) == len(doc["Statement"])


def test_models_foundation_and_system_profiles_then_only_tagged_application_profiles():
    doc = boundary_policy_document(_inputs())
    fm = "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-x"
    sys_profile = f"arn:aws:bedrock:{REGION}:{ACCT}:inference-profile/global.anthropic.claude-x"
    app_profile = f"arn:aws:bedrock:{REGION}:{ACCT}:application-inference-profile/p1"
    for action in ("bedrock:InvokeModel", "bedrock:ConverseStream"):
        assert _allowed(doc, action, fm) and _allowed(doc, action, sys_profile)
    assert _allowed(doc, "bedrock:InvokeModel", app_profile, _tag("alpha"))
    assert not _allowed(doc, "bedrock:InvokeModel", app_profile, _tag("beta"))
    # once the project has application profiles (§10.8), system profiles are out
    narrowed = boundary_policy_document(_inputs(application_inference_profiles=True))
    assert not _allowed(narrowed, "bedrock:InvokeModel", sys_profile)
    assert _allowed(narrowed, "bedrock:InvokeModel", app_profile, _tag("alpha"))


# ── the two-project matrix (§10.15) ───────────────────────────────────────────


B_RESOURCES = {
    "memory": ("bedrock-agentcore:CreateEvent", f"{AC}:memory/launchpad_mem_beta-a1b2c3d4e5", {}),
    "kb": ("bedrock:Retrieve",
           f"arn:aws:bedrock:{REGION}:{ACCT}:knowledge-base/KBBETA", _tag("beta")),
    "s3": ("s3:GetObject", f"arn:aws:s3:::{BUCKET}/projects/beta/kb/k1/doc.pdf", {}),
    "runtime": ("bedrock-agentcore:InvokeAgentRuntime", f"{AC}:runtime/beta_agent-x", _tag("beta")),
    "m2m": ("bedrock-agentcore:GetResourceOauth2Token",
            f"{AC}:token-vault/default/oauth2credentialprovider/launchpad-gw-m2m-beta", {}),
    "m2m-secret": ("secretsmanager:GetSecretValue",
                   f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:"
                   "bedrock-agentcore-identity!default/oauth2/launchpad-gw-m2m-beta-Ab12Cd", {}),
    "ecr": ("ecr:BatchGetImage", f"arn:aws:ecr:{REGION}:{ACCT}:repository/launchpad-agents/beta",
            {}),
    "gateway": ("bedrock-agentcore:InvokeGateway", f"{AC}:gateway/beta-gw-abcdefghij",
                _tag("beta")),
    "tagged-secret": ("secretsmanager:GetSecretValue",
                      f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:beta-db-Ab12Cd",
                      _tag("beta")),
}


def _own(resource: str) -> str:
    return resource.replace("beta", "alpha").replace("BETA", "ALPHA")


@pytest.mark.parametrize("what", sorted(B_RESOURCES))
def test_project_a_reaches_its_own_resource_but_not_project_bs(what):
    action, b_resource, b_context = B_RESOURCES[what]
    doc = boundary_policy_document(_inputs("alpha", kb_ids=()))
    assert not _allowed(doc, action, b_resource, b_context), f"A reaches B's {what}"
    own_context = {k: "alpha" for k in b_context}
    assert _allowed(doc, action, _own(b_resource), own_context), f"A denied its own {what}"


def test_project_ids_that_share_a_prefix_do_not_overlap():
    """`alpha` vs `alpha-2`: name-prefix grants end at a delimiter the other id cannot
    supply (memory names have no `-`; vault secrets end in exactly six characters)."""
    doc = boundary_policy_document(_inputs("alpha"))
    other_memory = f"{AC}:memory/launchpad_mem_alpha_2-a1b2c3d4e5"
    other_secret = (f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:bedrock-agentcore-"
                    "identity!default/oauth2/launchpad-gw-m2m-alpha-2-Ab12Cd")
    other_objects = f"arn:aws:s3:::{BUCKET}/projects/alpha-2/agents/x/code.zip"
    assert not _allowed(doc, "bedrock-agentcore:ListEvents", other_memory)
    assert not _allowed(doc, "secretsmanager:GetSecretValue", other_secret)
    assert not _allowed(doc, "s3:GetObject", other_objects)


def test_platform_gateways_and_skills_stay_reachable():
    doc = boundary_policy_document(_inputs())
    assert _allowed(doc, "bedrock-agentcore:InvokeGateway", f"{AC}:gateway/launchpad-gw-abcdefghij",
                    _tag("platform"))
    assert not _allowed(doc, "bedrock-agentcore:InvokeGateway",
                        f"{AC}:gateway/launchpad-gw-abcdefghij")  # untagged
    assert _allowed(doc, "s3:GetObject", f"arn:aws:s3:::{BUCKET}/skills/pdf/SKILL.md")
    assert _allowed(doc, "s3:ListBucket", f"arn:aws:s3:::{BUCKET}",
                    {"s3:prefix": "projects/alpha/kb/"})
    assert not _allowed(doc, "s3:ListBucket", f"arn:aws:s3:::{BUCKET}",
                        {"s3:prefix": "projects/beta/"})
    # legacy agent keys are read-only: nothing grants a write anywhere in the bucket
    assert not _allowed(doc, "s3:PutObject", f"arn:aws:s3:::{BUCKET}/agents/x/code.zip")


def test_legacy_shared_resources_leave_the_boundary_at_retire():
    shared_memory = f"{AC}:memory/launchpad_memory-abc"
    shared_m2m = f"{AC}:token-vault/default/oauth2credentialprovider/launchpad-gw-m2m"
    live = boundary_policy_document(_inputs())
    assert _allowed(live, "bedrock-agentcore:ListEvents", shared_memory)
    assert _allowed(live, "bedrock-agentcore:GetResourceOauth2Token", shared_m2m)
    assert _allowed(live, "bedrock-agentcore:GetResourceOauth2Token", KB_PROVIDER)
    retired = boundary_policy_document(_inputs(include_legacy=False))
    assert not _allowed(retired, "bedrock-agentcore:ListEvents", shared_memory)
    assert not _allowed(retired, "bedrock-agentcore:GetResourceOauth2Token", shared_m2m)
    assert not _allowed(retired, "bedrock-agentcore:GetResourceOauth2Token", KB_PROVIDER)
    # the project's own provider stays
    own = f"{AC}:token-vault/default/oauth2credentialprovider/launchpad-gw-m2m-alpha"
    assert _allowed(retired, "bedrock-agentcore:GetResourceOauth2Token", own)


def test_explicit_lists_cover_connections_and_untagged_kbs():
    doc = boundary_policy_document(_inputs(kb_ids=("KBOWN",), oauth2_connections=("crm",),
                                           api_key_connections=("weather",)))
    kb = f"arn:aws:bedrock:{REGION}:{ACCT}:knowledge-base/KBOWN"
    assert _allowed(doc, "bedrock:Retrieve", kb)  # no tag needed for a listed KB
    assert not _allowed(doc, "bedrock:Retrieve", kb.replace("KBOWN", "KBOTHER"))
    vault = f"{AC}:token-vault/default"
    assert _allowed(doc, "bedrock-agentcore:GetResourceOauth2Token",
                    f"{vault}/oauth2credentialprovider/crm")
    assert _allowed(doc, "bedrock-agentcore:GetResourceApiKey",
                    f"{vault}/apikeycredentialprovider/weather")
    assert not _allowed(doc, "bedrock-agentcore:GetResourceApiKey",
                        f"{vault}/apikeycredentialprovider/other")
    secret = (f"arn:aws:secretsmanager:{REGION}:{ACCT}:secret:"
              "bedrock-agentcore-identity!default/apikey/weather-Ab12Cd")
    assert _allowed(doc, "secretsmanager:GetSecretValue", secret)


def test_metrics_only_in_the_agentcore_namespace():
    doc = boundary_policy_document(_inputs())
    assert _allowed(doc, "cloudwatch:PutMetricData", "*",
                    {"cloudwatch:namespace": "bedrock-agentcore"})
    assert not _allowed(doc, "cloudwatch:PutMetricData", "*", {"cloudwatch:namespace": "Other"})


def test_extra_boundary_builders_append_statements(monkeypatch):
    extra = {"Sid": "BoundaryExtra", "Effect": "Allow", "Action": ["s3files:Foo"],
             "Resource": "*"}
    monkeypatch.setattr(project_iam, "EXTRA_BOUNDARY_STATEMENT_BUILDERS",
                        [lambda inp: [extra] if inp.project_id == "alpha" else []])
    assert extra in boundary_policy_document(_inputs())["Statement"]
    assert extra not in boundary_policy_document(_inputs("beta"))["Statement"]


# ── the identity policy fits under the boundary ───────────────────────────────


ROLE_CTX = agent_iam.RoleContext(
    account_id=ACCT, region=REGION, artifacts_bucket=BUCKET,
    ecr_repo_arn=f"arn:aws:ecr:{REGION}:{ACCT}:repository/launchpad-agents",
    memory_id="launchpad_memory-abc", oauth_provider_arn=KB_PROVIDER,
)


S3_AP = f"arn:aws:s3files:{REGION}:{ACCT}:file-system/fs-1/access-point/ap-1"


def _specs() -> list[AgentSpec]:
    base = {"name": "probe", "system_prompt": "p"}
    return [
        AgentSpec(**base, method="zip_runtime"),
        AgentSpec(**base, method="zip_runtime", memory={"short_term": True, "long_term": True}),
        AgentSpec(**base, method="harness", knowledge_bases=[{"kb_id": "KBOWN"}]),
        AgentSpec(**base, method="zip_runtime", protocol="a2a"),
        AgentSpec(**base, method="container",
                  tools=[{"type": "builtin", "name": "code-interpreter"},
                         {"type": "builtin", "name": "browser"}]),
        AgentSpec(**base, method="zip_runtime",
                  tools=[{"type": "mcp", "name": "crm", "config": {"url": "https://x"},
                          "auth": {"connection": "crm", "kind": "oauth2"}},
                         {"type": "gateway", "name": "gw",
                          "config": {"gateway_id": "launchpad-gw-abcdefghij"}}]),
        AgentSpec(**base, method="zip_runtime", model_source="mantle"),
        AgentSpec(**base, method="container", filesystem={"s3_files": [{
            "access_point_arn": S3_AP, "mount_path": "/mnt/a"}]},
                  network={"subnets": ["subnet-1"], "security_groups": ["sg-1"]}),
    ]


def _concrete(resource: str) -> str:
    """A representative ARN for an identity-policy resource pattern."""
    return resource.replace("??????", "Ab12Cd").replace("*", "x1")


def test_every_identity_grant_survives_the_boundary():
    """For each action an agent of project A can be granted, some boundary statement
    allows it on that resource — given A's own resource tags."""
    specs = _specs()
    needs = project_iam._Needs()
    for spec in specs:
        needs.add_spec(spec)
    boundary = boundary_policy_document(_inputs(
        kb_ids=tuple(sorted(needs.kb_ids)), oauth2_connections=tuple(sorted(needs.oauth2)),
    ))
    context = {**_tag("alpha"), "cloudwatch:namespace": "bedrock-agentcore",
               "s3:prefix": "skills/pdf/"}
    for spec in specs:
        docs = [agent_iam.policy_document(spec, ROLE_CTX, inbound_jwt=True)]
        if agent_iam.fs_policy_document(spec):
            docs.append(agent_iam.fs_policy_document(spec))
        for doc in docs:
            for statement in doc["Statement"]:
                for action in _as_list(statement["Action"]):
                    for resource in _as_list(statement["Resource"]):
                        if resource == "*":
                            # an unscopable grant: the request's resource is whatever
                            # the call names, so the action must be in the boundary
                            assert any(
                                any(_match(a, action) for a in _as_list(b["Action"]))
                                for b in boundary["Statement"]
                            ), f"{spec.method}: boundary lacks {action}"
                            continue
                        concrete = _concrete(resource)
                        assert _allowed(boundary, action, concrete, context), (
                            f"{spec.method}: boundary denies {action} on {concrete}"
                        )


def test_s3_bucket_mount_grants_survive_the_boundary():
    """An `s3bucket` catalog mount's role statements (per-session with the
    `aws:AgentCoreRuntimeSourceSid` variable, shared, KMS) fit under the boundary of a
    project the entries are allowed to — and nothing else on S3 opens (probe P9: the mount
    runs under the boundary, so without these the mount fails 424 access denied)."""
    from app.models.ledger import Workspace
    from app.services import storage_mounts
    from tests.conftest import TEST_PROJECT

    kms = f"arn:aws:kms:{REGION}:{ACCT}:key/1234abcd-12ab-34cd-56ef-1234567890ab"
    agent_id = "b" * 32
    sid_var = "${aws:AgentCoreRuntimeSourceSid}"
    sid = "0123456789012345678901234567890123456789"
    db = SessionLocal()
    try:
        for entry in ({"id": "reports", "bucket": "acme-data", "prefix": "team",
                       "scope": "per_session", "max_mode": "rw"},
                      {"id": "lake", "bucket": "acme-lake", "scope": "shared"}):
            storage_mounts.register(db, DEFAULT_WORKSPACE_ID, {**entry, "kind": "s3bucket"}, "o")
        db.query(storage_mounts.StorageMount).filter_by(id="reports").one().kms_key_arn = kms
        db.commit()
        storage_mounts.set_project_entries(db, DEFAULT_WORKSPACE_ID, TEST_PROJECT,
                                           ["reports", "lake"])
        workspace = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        inp = _inputs(TEST_PROJECT, account_id=workspace.account_id, region=workspace.region)
        stored = storage_mounts.normalize_spec_storage(
            db, DEFAULT_WORKSPACE_ID, TEST_PROJECT,
            {"name": "probe", "method": "zip_runtime", "system_prompt": "p",
             "runtime_config": {"platform_version": "V2", "billing_model": "consumption"},
             "filesystem": {"mounts": [{"storage_id": "reports", "mode": "rw"},
                                       {"storage_id": "lake", "mode": "ro"}]}})
    finally:
        db.close()
    boundary = boundary_policy_document(inp)
    document = agent_iam.fs_policy_document(AgentSpec.model_validate(stored), agent_id)
    assert document and any("kms:Decrypt" in _as_list(st["Action"])
                            for st in document["Statement"])
    for statement in document["Statement"]:
        condition = json.loads(json.dumps(statement.get("Condition") or {}).replace(sid_var, sid))
        prefixes = (condition.get("StringLike") or {}).get("s3:prefix") or [""]
        for action in _as_list(statement["Action"]):
            for resource in _as_list(statement["Resource"]):
                concrete = resource.replace(sid_var, sid).replace("*", "x1")
                for prefix in prefixes:
                    context = {"s3:prefix": prefix.replace("*", "x1")}
                    assert _allowed(boundary, action, concrete, context), (action, concrete)
    # …and the boundary opens no other bucket, nor a prefix outside the entry's
    assert not _allowed(boundary, "s3:GetObject", "arn:aws:s3:::someone-else/x")
    assert not _allowed(boundary, "s3:ListBucket", "arn:aws:s3:::acme-data",
                        {"s3:prefix": "other/"})
    # a project the entries are not allowed to gets nothing of it
    other = boundary_policy_document(_inputs("beta", account_id=inp.account_id,
                                             region=inp.region))
    assert not _allowed(other, "s3:GetObject", "arn:aws:s3:::acme-data/team/x")


def test_extra_statement_builders_reach_the_identity_policy(monkeypatch):
    seen: list[Any] = []

    def build(spec, resources):
        seen.append((spec.name, resources.account_id))
        return [{"Sid": "SmExtra", "Effect": "Allow", "Action": ["s3files:ClientMount"],
                 "Resource": "*"}]

    monkeypatch.setattr(agent_iam, "EXTRA_STATEMENT_BUILDERS", [build, lambda s, r: None])
    doc = agent_iam.policy_document(_specs()[0], ROLE_CTX)
    assert doc["Statement"][-1]["Sid"] == "SmExtra"
    assert seen == [("probe", ACCT)]


# ── the IAM writes (a stub IAM client) ────────────────────────────────────────


class NoSuchEntity(Exception):
    """Name-matched by project_iam._is_no_such_entity."""


class EntityAlreadyExists(Exception):
    """Name-matched by agent_iam._is_already_exists."""


class StubIam:
    def __init__(self):
        self.policies: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []

    # managed policies
    def get_policy(self, PolicyArn):
        if PolicyArn not in self.policies:
            raise NoSuchEntity(PolicyArn)
        p = self.policies[PolicyArn]
        return {"Policy": {"Arn": PolicyArn, "DefaultVersionId": p["default"]}}

    def create_policy(self, PolicyName, PolicyDocument, Description, Tags):
        self.calls.append("create_policy")
        arn = f"arn:aws:iam::{ACCT}:policy/{PolicyName}"
        self.policies[arn] = {"tags": Tags, "default": "v1", "next": 2,
                              "versions": {"v1": json.loads(PolicyDocument)}}
        return {"Policy": {"Arn": arn, "DefaultVersionId": "v1"}}

    def list_policy_tags(self, PolicyArn):
        return {"Tags": self.policies[PolicyArn]["tags"]}

    def get_policy_version(self, PolicyArn, VersionId):
        return {"PolicyVersion": {"Document": self.policies[PolicyArn]["versions"][VersionId]}}

    def list_policy_versions(self, PolicyArn):
        p = self.policies[PolicyArn]
        return {"Versions": [{"VersionId": v, "IsDefaultVersion": v == p["default"]}
                             for v in p["versions"]]}

    def delete_policy_version(self, PolicyArn, VersionId):
        self.calls.append(f"delete_version:{VersionId}")
        del self.policies[PolicyArn]["versions"][VersionId]

    def create_policy_version(self, PolicyArn, PolicyDocument, SetAsDefault):
        p = self.policies[PolicyArn]
        assert len(p["versions"]) < 5, "IAM keeps five versions"
        vid = f"v{p['next']}"
        p["next"] += 1
        p["versions"][vid] = json.loads(PolicyDocument)
        if SetAsDefault:
            p["default"] = vid
        self.calls.append(f"create_version:{vid}")
        return {"PolicyVersion": {"VersionId": vid}}

    # roles
    def create_role(self, RoleName, AssumeRolePolicyDocument, **kwargs):
        self.calls.append(f"create_role:{RoleName}")
        if RoleName in self.roles:
            raise EntityAlreadyExists(RoleName)
        self.roles[RoleName] = {"tags": list(kwargs.get("Tags") or []),
                                "boundary": kwargs.get("PermissionsBoundary"), "policies": {}}
        return {"Role": {"Arn": f"arn:aws:iam::{ACCT}:role/{RoleName}"}}

    def get_role(self, RoleName):
        return {"Role": {"Arn": f"arn:aws:iam::{ACCT}:role/{RoleName}"}}

    def update_assume_role_policy(self, RoleName, PolicyDocument):
        pass

    def tag_role(self, RoleName, Tags):
        self.calls.append(f"tag_role:{RoleName}")
        self.roles[RoleName]["tags"] += Tags

    def put_role_permissions_boundary(self, RoleName, PermissionsBoundary):
        self.calls.append(f"put_boundary:{RoleName}")
        self.roles[RoleName]["boundary"] = PermissionsBoundary

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.roles[RoleName]["policies"][PolicyName] = json.loads(PolicyDocument)

    def delete_role_policy(self, RoleName, PolicyName):
        raise NoSuchEntity(PolicyName)


def test_ensure_boundary_creates_then_converges_then_versions():
    iam = StubIam()
    first = project_iam.ensure_boundary_policy(iam, _inputs(), WS)
    assert first.changed and first.version == 1 and first.label == "boundary-v1"
    assert first.name == "launchpad-project-alpha"
    tags = {t["Key"]: t["Value"] for t in iam.policies[first.arn]["tags"]}
    assert tags == {"launchpad:project": "alpha", "launchpad:workspace": WS,
                    "launchpad:managed": "true"}
    again = project_iam.ensure_boundary_policy(iam, _inputs(), WS)
    assert not again.changed and again.version == 1
    assert iam.calls == ["create_policy"]  # an unchanged document writes nothing
    # each change is a new default version; the oldest non-default ones make room
    for n in range(2, 9):
        state = project_iam.ensure_boundary_policy(
            iam, _inputs(oauth2_connections=(f"c{n}",)), WS)
        assert state.changed and state.version == n
    versions = iam.policies[first.arn]["versions"]
    assert len(versions) == 5 and iam.policies[first.arn]["default"] == "v8"
    assert "delete_version:v1" in iam.calls


def test_a_policy_not_tagged_for_the_project_is_refused():
    iam = StubIam()
    state = project_iam.ensure_boundary_policy(iam, _inputs(), WS)
    iam.policies[state.arn]["tags"] = [{"Key": "launchpad:project", "Value": "someone-else"}]
    with pytest.raises(ForeignResourceError):
        project_iam.ensure_boundary_policy(iam, _inputs(), WS)


def test_an_oversized_boundary_is_refused_before_any_write():
    iam = StubIam()
    many = tuple(f"KB{i:028d}" for i in range(120))
    with pytest.raises(svc.ProvisionError, match="over IAM's"):
        project_iam.ensure_boundary_policy(iam, _inputs(kb_ids=many), WS)
    assert iam.calls == []


# ── ledger-backed: the provisioner and the deploy path ────────────────────────


def _isolate(pid: str) -> None:
    db = SessionLocal()
    try:
        from app.models.projects import Project

        row = db.get(Project, pid)
        row.settings = {**(row.settings or {}), "isolation": {"mode": "isolated"}}
        db.commit()
    finally:
        db.close()


def _add_agent(pid: str, agent_id: str, spec: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        with project_context(pid):
            db.add(Agent(id=agent_id, workspace_id=WS, project_id=pid, name=spec["name"],
                         method=spec["method"], spec=spec, version="1", status="active"))
            db.commit()
    finally:
        db.close()


class _Workspace:
    """A workspace context whose every client is the stub IAM (WorkspaceContext is
    frozen, so the client cannot be swapped on one)."""

    def __init__(self, iam: StubIam):
        ctx = ws_ctx({"artifacts_bucket": BUCKET, "memory_id": "launchpad_memory-abc",
                      "oauth_provider_arn": KB_PROVIDER, "ecr_repo": "launchpad-agents"},
                     account_id=ACCT, region=REGION)
        self.account_id, self.region, self.id = ctx.account_id, ctx.region, ctx.id
        self.partition, self.resources = ctx.partition, ctx.resources
        self._iam = iam

    def client(self, service: str, *args: Any, **kwargs: Any) -> StubIam:
        assert service == "iam", service
        return self._iam


def _workspace(iam: StubIam) -> Any:
    return _Workspace(iam)


class _NoQuota:
    def check(self, ctx):
        return []


@pytest.fixture
def alpha(app):  # noqa: F811
    _project("alpha")
    _isolate("alpha")
    return "alpha"


def test_the_boundary_stage_provisions_and_records(alpha):
    _add_agent("alpha", "agentkb1", {"name": "kb-bot", "method": "harness",
                                     "system_prompt": "p",
                                     "knowledge_bases": [{"kb_id": "KBALPHA"}]})
    iam = StubIam()
    db = SessionLocal()
    try:
        job, _ = svc.queue_provision(db, "alpha", WS, actor="test")
        job_id = job.id
    finally:
        db.close()
    provisioners = svc.Provisioners(iam=project_iam.IamBoundaryProvisioner(),
                                    memory=svc._PENDING, kb=svc._PENDING,
                                    gateway=svc._PENDING, storage=svc._PENDING,
                                    quotas=_NoQuota())
    svc.execute_provision_job(job_id, provisioners, workspace_factory=lambda _ws: _workspace(iam))
    db = SessionLocal()
    try:
        with project_context(None):
            stages = {e["name"]: e for e in svc.job_stages(db.get(svc.Job, job_id))}
        row = db.get(ProjectResource, ("alpha", WS, KIND_BOUNDARY_POLICY))
        assert stages["boundary"]["status"] == "succeeded", stages["boundary"]
        assert row.status == "ready" and row.resource_id == "launchpad-project-alpha"
        assert row.detail["version"] == 1 and row.detail["kb_ids"] == ["KBALPHA"]
    finally:
        db.close()
    doc = iam.policies[row.arn]["versions"]["v1"]
    assert _allowed(doc, "bedrock:Retrieve",
                    f"arn:aws:bedrock:{REGION}:{ACCT}:knowledge-base/KBALPHA")


def _settings(per_agent: bool = True):
    return SimpleNamespace(per_agent_execution_roles=per_agent)


def _agent_row(pid: str | None, agent_id: str = "abcdef1234") -> Agent:
    return Agent(workspace_id=WS, project_id=pid, id=agent_id, name="probe",
                 method="zip_runtime", spec={}, version="1")


def _probe_spec(**over: Any) -> AgentSpec:
    return AgentSpec(**{"name": "probe", "method": "zip_runtime", "system_prompt": "p",
                        **over})


def test_an_isolated_agent_role_is_created_with_the_boundary_and_tags(alpha):
    iam = StubIam()
    agent = _agent_row("alpha")
    spec = _probe_spec(tools=[{"type": "mcp", "name": "crm", "config": {"url": "https://x"},
                               "auth": {"connection": "crm", "kind": "oauth2"}}])
    arn, detail = agent_iam.provision_execution_role(agent, spec, _settings(), _workspace(iam))
    role = iam.roles[agent_iam.role_name_for("probe", "abcdef1234")]
    assert role["boundary"] == f"arn:aws:iam::{ACCT}:policy/launchpad-project-alpha"
    tags = {t["Key"]: t["Value"] for t in role["tags"]}
    assert tags["launchpad:project"] == "alpha" and tags["launchpad:workspace"] == WS
    assert tags["launchpad:isolation"] == "isolated"
    assert detail.endswith("boundary boundary-v1") and agent.isolation_applied == "boundary-v1"
    # the spec's Connection is in the boundary even before the agent row stores it
    doc = iam.policies[role["boundary"]]["versions"]["v1"]
    assert _allowed(doc, "bedrock-agentcore:GetResourceOauth2Token",
                    f"{AC}:token-vault/default/oauth2credentialprovider/crm")
    db = SessionLocal()
    try:
        row = db.get(ProjectResource, ("alpha", WS, KIND_BOUNDARY_POLICY))
        assert row is not None and row.detail["connections"] == ["crm"]
    finally:
        db.close()


def test_an_adopted_role_gets_the_boundary_and_tags(alpha):
    iam = StubIam()
    name = agent_iam.role_name_for("probe", "abcdef1234")
    iam.roles[name] = {"tags": [], "boundary": None, "policies": {}}
    agent_iam.provision_execution_role(_agent_row("alpha"), _probe_spec(), _settings(),
                                       _workspace(iam))
    assert f"tag_role:{name}" in iam.calls and f"put_boundary:{name}" in iam.calls
    assert iam.roles[name]["boundary"].endswith(":policy/launchpad-project-alpha")


def test_a_shared_projects_agent_role_is_unchanged(app):  # noqa: F811
    _project("gamma")
    iam = StubIam()
    for pid in ("gamma", None):
        iam.roles.clear()
        agent = _agent_row(pid)
        _arn, detail = agent_iam.provision_execution_role(agent, _probe_spec(), _settings(),
                                                          _workspace(iam))
        role = iam.roles[agent_iam.role_name_for("probe", "abcdef1234")]
        assert role["boundary"] is None
        # no isolation tag; the cost tags (cost-tags §1.2) are on every agent's role
        keys = {t["Key"] for t in role["tags"]}
        assert keys == {agent_iam.MANAGED_TAG_KEY, "launchpad:managed",
                        *cost_tags.agent_tags(agent, WS)}
        assert "launchpad:isolation" not in keys
        assert "boundary" not in detail and agent.isolation_applied is None
    assert not iam.policies


def test_an_isolated_agent_never_falls_back_to_the_shared_role(alpha):
    ws = _workspace(StubIam())
    ws.resources["execution_role_arn"] = f"arn:aws:iam::{ACCT}:role/launchpad-agent-execution-role"
    with pytest.raises(AppError) as err:
        agent_iam.provision_execution_role(_agent_row("alpha"), _probe_spec(),
                                           _settings(per_agent=False), ws)
    assert err.value.code == "project.isolation_requires_per_agent_role"
    assert err.value.status_code == 409
    # candidates: the live role is fine, the shared fallback is not
    live = {"roleArn": f"arn:aws:iam::{ACCT}:role/launchpad-agent-probe-abcdef12"}
    assert agent_iam.live_runtime_role_arn(live, ws, "alpha") == live["roleArn"]
    with pytest.raises(AppError):
        agent_iam.live_runtime_role_arn({}, ws, "alpha")
    # a shared project keeps both fallbacks
    assert agent_iam.live_runtime_role_arn({}, ws, None) == ws.resources["execution_role_arn"]
