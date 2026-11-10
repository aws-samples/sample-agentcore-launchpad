"""N11 — the `EnvName` suffix in the backend and the two scripts.

Two guarantees, mirrored from `infra/tests/test_naming.py`:

1. With the default empty `Settings.env_name`, every name the backend creates or looks
   up is byte-for-byte the pre-N11 name.
2. With `env_name="t1"`, every fixed `launchpad-*` resource / stack name ends in `-t1`,
   per-agent role names stay inside IAM's 64 characters, and teardown / bootstrap only
   touch the environment they were started for.

Env switching goes through one seam, `app.core.naming.current_env_name`, which every
call site resolves at call time (module attribute, never a bound import).
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import ValidationError

import app.core.config as config_mod
from app.core import naming
from app.core.config import Settings
from app.services import agent_iam, gateway_bootstrap, identity_providers, workspace_iam
from app.services import bootstrap as bs
from app.services import memory_bootstrap as mb
from app.services import workspace_bootstrap as wb

SCRIPTS = Path(__file__).parents[2] / "scripts"
INFRA_NAMING = Path(__file__).parents[2] / "infra" / "stacks" / "naming.py"
EC2_TEMPLATE = Path(__file__).parents[2] / "infra" / "ec2" / "launchpad-cn-ec2.yaml"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"launchpad_{name}_script", SCRIPTS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def env_t1(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")


@pytest.fixture
def env_default(monkeypatch):
    monkeypatch.setattr(naming, "current_env_name", lambda: "")


# ── the helper and the setting ──────────────────────────────────────────────


def test_resource_name_is_identity_for_the_empty_env(env_default):
    assert naming.resource_name("launchpad-gw") == "launchpad-gw"
    assert naming.suffixed("launchpad-gw", "") == "launchpad-gw"


def test_resource_name_appends_dash_env_at_the_end_once(env_t1):
    assert naming.resource_name("launchpad-gw") == "launchpad-gw-t1"
    assert naming.resource_name("launchpad-agent-execution-role-cn-northwest-1") == (
        "launchpad-agent-execution-role-cn-northwest-1-t1"
    )
    assert naming.resource_name("launchpad-base", env_name="") == "launchpad-base"
    assert naming.resource_name("launchpad-base", env_name="r2") == "launchpad-base-r2"


@pytest.mark.parametrize("bad", ["T1", "t-1", "t_1", "abcd", "t1 ", "prod"])
def test_env_name_is_validated_in_the_helper_and_in_settings(bad, tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="env_name"):
        naming.validate_env_name(bad)
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "missing.yaml")
    with pytest.raises(ValidationError):
        Settings(env_name=bad)


def test_settings_env_name_defaults_empty_and_reads_launchpad_env_name(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_FILE", tmp_path / "missing.yaml")
    monkeypatch.delenv(naming.ENV_NAME_ENV_VAR, raising=False)
    assert Settings().env_name == ""
    monkeypatch.setenv(naming.ENV_NAME_ENV_VAR, "t1")
    assert Settings().env_name == "t1"
    assert naming.ENV_NAME_ENV_VAR == "LAUNCHPAD_ENV_NAME"


def test_pattern_is_identical_across_the_three_layers():
    """The backend cannot import the CDK app, so the parity is pinned textually: the
    CDK helper's module and the EC2 template must carry this exact regex."""
    assert naming.ENV_NAME_PATTERN == r"^[a-z0-9]{0,3}$"
    infra_text = INFRA_NAMING.read_text(encoding="utf-8")
    assert f"ENV_NAME_MAX_LEN = {naming.ENV_NAME_MAX_LEN}" in infra_text
    assert 'ENV_NAME_PATTERN = rf"^[a-z0-9]{{0,{ENV_NAME_MAX_LEN}}}$"' in infra_text
    assert 'AllowedPattern: "^[a-z0-9]{0,3}$"' in EC2_TEMPLATE.read_text(encoding="utf-8")


# ── every fixed name goes through the helper ────────────────────────────────

FIXED_NAMES = [
    (bs.base_stack_name, "launchpad-base"),
    (mb.memory_stack_name, "launchpad-memory"),
    (gateway_bootstrap.gateway_name, "launchpad-gw"),
    (gateway_bootstrap.api_key_provider_name, "launchpad-office-facts-key"),
    (gateway_bootstrap.gateway_m2m_provider_name, "launchpad-gw-m2m"),
    (wb.ecr_repo_name, "launchpad-agents"),
    (wb.codebuild_project_name, "launchpad-agent-builder"),
    (naming.default_ecr_repo, "launchpad-agents"),
    (lambda: workspace_iam.regional_role_name(workspace_iam.EXECUTION_ROLE_BASE, "cn-northwest-1"),
     "launchpad-agent-execution-role-cn-northwest-1"),
    (lambda: workspace_iam.regional_role_name(workspace_iam.GATEWAY_ROLE_BASE, "cn-northwest-1"),
     "launchpad-gateway-role-cn-northwest-1"),
    (lambda: workspace_iam.regional_role_name(workspace_iam.CODEBUILD_ROLE_BASE, "cn-northwest-1"),
     "launchpad-codebuild-role-cn-northwest-1"),
    (lambda: workspace_iam.regional_role_name(workspace_iam.EXECUTION_ROLE_BASE, "us-west-2"),
     "launchpad-agent-execution-role"),
    (agent_iam.agent_role_pattern, "role/launchpad-agent-*"),
]


@pytest.mark.parametrize(("resolve", "default"), FIXED_NAMES, ids=[d for _, d in FIXED_NAMES])
def test_default_env_keeps_every_fixed_name_byte_identical(env_default, resolve, default):
    assert resolve() == default


@pytest.mark.parametrize(("resolve", "default"), FIXED_NAMES, ids=[d for _, d in FIXED_NAMES])
def test_env_t1_suffixes_every_fixed_name_at_the_end(env_t1, resolve, default):
    got = resolve()
    assert got == f"{default}-t1"
    assert got.count("-t1") == 1
    assert len(got) <= 64 if "role" in default else True


def test_artifacts_bucket_name_matches_the_cdk_shape_with_the_suffix(env_t1):
    class Ctx:
        account_id = "111122223333"
        region = "cn-northwest-1"

    assert wb._artifacts_bucket_name(Ctx()) == "launchpad-artifacts-111122223333-cn-northwest-1-t1"
    assert len(wb._artifacts_bucket_name(Ctx())) <= 63


def test_system_provider_names_follow_the_env(env_t1):
    assert identity_providers.system_provider_names() == {
        "launchpad-office-facts-key-t1",
        "launchpad-gw-m2m-t1",
    }


def test_gateway_and_provider_ensure_functions_default_to_the_env_name(env_t1):
    """`ensure_gateway` / `ensure_api_key_provider` resolve their default name at call
    time (not at import), so the module-level base constants are never used raw."""
    calls = []

    class Control:
        def list_gateways(self, **kw):
            return {"items": [{"gatewayId": "gw-1", "name": "launchpad-gw-t1"}]}

        def get_gateway(self, gatewayIdentifier):
            calls.append(gatewayIdentifier)
            return {"gatewayId": "gw-1", "gatewayArn": "arn", "gatewayUrl": "https://x"}

        def list_api_key_credential_providers(self, **kw):
            return {"credentialProviders": [
                {"name": "launchpad-office-facts-key", "credentialProviderArn": "arn:default"},
                {"name": "launchpad-office-facts-key-t1", "credentialProviderArn": "arn:t1"},
            ]}

    gw, created = gateway_bootstrap.ensure_gateway(Control(), role_arn="r", region="cn-northwest-1")
    assert (gw["id"], created, calls) == ("gw-1", False, ["gw-1"])
    arn, created = gateway_bootstrap.ensure_api_key_provider(Control(), "secret")
    assert (arn, created) == ("arn:t1", False)


# ── per-agent roles ─────────────────────────────────────────────────────────


def test_role_name_for_default_env_is_unchanged(env_default):
    assert agent_iam.role_name_for("support-bot", "abcdef1234567890") == (
        "launchpad-agent-support-bot-abcdef12"
    )


def test_role_name_for_env_t1_ends_with_the_suffix_and_fits_iam(env_t1):
    name = agent_iam.role_name_for("support-bot", "abcdef1234567890")
    assert name == "launchpad-agent-support-bot-abcdef12-t1"
    long = agent_iam.role_name_for("x" * 300, "abcdef1234567890")
    assert len(long) == 64 and long.endswith("-abcdef12-t1")
    # explicit env wins over the setting, and the longest env still fits
    longest = agent_iam.role_name_for("x" * 300, "abcdef1234567890", env_name="zzz")
    assert len(longest) == 64 and longest.endswith("-abcdef12-zzz")
    assert agent_iam.role_name_for("support-bot", "abcdef1234567890", env_name="") == (
        "launchpad-agent-support-bot-abcdef12"
    )


def test_ensure_role_tags_the_env_only_when_set(monkeypatch):
    from tests.test_agent_iam_lifecycle import CTX, StubIam, _agent, _spec

    monkeypatch.setattr(naming, "current_env_name", lambda: "")
    iam = StubIam()
    agent = _agent()
    agent_iam.ensure_role(iam, agent, _spec(), CTX)
    default_tags = iam.roles[agent_iam.role_name_for(agent.name, agent.id)]["tags"]
    assert all(t["Key"] != naming.ENV_TAG_KEY for t in default_tags)

    monkeypatch.setattr(naming, "current_env_name", lambda: "t1")
    iam = StubIam()
    agent_iam.ensure_role(iam, agent, _spec(), CTX)
    name = agent_iam.role_name_for(agent.name, agent.id)
    assert name.endswith("-t1")
    assert {"Key": naming.ENV_TAG_KEY, "Value": "t1"} in iam.roles[name]["tags"]


# ── scripts: bootstrap (cdk context) ────────────────────────────────────────


def test_cdk_context_args_default_env_is_todays_behaviour(env_default):
    assert bs.cdk_context_args({}) == []
    assert bs.cdk_context_args(
        {"LAUNCHPAD_CDK_CONTEXT": "memory_master_arn=arn:x, env_name="}
    ) == ["--context", "memory_master_arn=arn:x", "--context", "env_name="]


def test_cdk_context_args_appends_env_name_when_the_variable_lacks_it(env_t1):
    assert bs.cdk_context_args({}) == ["--context", "env_name=t1"]
    assert bs.cdk_context_args({"LAUNCHPAD_CDK_CONTEXT": "memory_master_arn=arn:x"}) == [
        "--context", "memory_master_arn=arn:x", "--context", "env_name=t1",
    ]
    # the EC2 template's own line (env_name=t1 already present) is passed through once
    assert bs.cdk_context_args(
        {"LAUNCHPAD_CDK_CONTEXT": "memory_master_arn=arn:x,env_name=t1"}
    ) == ["--context", "memory_master_arn=arn:x", "--context", "env_name=t1"]


def test_cdk_context_args_refuses_a_mismatch_between_context_and_setting(env_t1, env_default):
    # env_default was applied last → setting is "", context says t1
    with pytest.raises(RuntimeError, match="env_name='t1'.*''"):
        bs.cdk_context_args({"LAUNCHPAD_CDK_CONTEXT": "env_name=t1"})


def test_bootstrap_script_routes_through_the_shared_cdk_context_args(env_t1, monkeypatch):
    script = _load_script("bootstrap")
    monkeypatch.delenv("LAUNCHPAD_CDK_CONTEXT", raising=False)
    assert script.cdk_context_args() == ["--context", "env_name=t1"]
    calls = []
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    script.deploy_cdk()
    assert calls == [["uv", "run", "cdk", "deploy", "--all", "--require-approval", "never",
                      "--context", "env_name=t1"]]


# ── scripts: teardown ───────────────────────────────────────────────────────


def test_teardown_discovers_only_its_own_environment(env_t1, monkeypatch):
    from tests.test_teardown_script import StubControl, StubIam

    teardown = _load_script("teardown")
    monkeypatch.delenv(teardown.EC2_STACK_NAME_ENV, raising=False)
    control = StubControl(
        gateways=[
            {"gatewayId": "gw-default", "name": "launchpad-gw"},
            {"gatewayId": "gw-t1", "name": "launchpad-gw-t1"},
            {"gatewayId": "gw-t2", "name": "launchpad-gw-t2"},
        ],
        targets={"gw-t1": [{"targetId": "t-hr", "name": "hr-database"}]},
        api_key_providers=[
            {"name": "launchpad-office-facts-key", "credentialProviderArn": "arn:d"},
            {"name": "launchpad-office-facts-key-t1", "credentialProviderArn": "arn:t1"},
        ],
    )
    tag = teardown.agent_iam.MANAGED_TAG_KEY
    iam = StubIam(roles={
        "launchpad-agent-foo-12345678": [{"Key": tag, "Value": "foo"}],  # default env
        "launchpad-agent-bar-12345678-t1": [{"Key": tag, "Value": "bar"},
                                            {"Key": naming.ENV_TAG_KEY, "Value": "t1"}],
        "launchpad-agent-baz-12345678-t2": [{"Key": tag, "Value": "baz"},
                                            {"Key": naming.ENV_TAG_KEY, "Value": "t2"}],
    })
    clients = {"bedrock-agentcore-control": control, "iam": iam}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])

    def get_stack_outputs(region, name=None):
        assert name in (None, "launchpad-memory-t1")  # never the default stack
        return {}

    monkeypatch.setattr(teardown.bs, "get_stack_outputs", get_stack_outputs)

    targets = teardown.collect_targets("cn-northwest-1")
    assert targets == [
        ("gateway-target", "gw-t1/t-hr", "launchpad-gw-t1/hr-database"),
        ("gateway", "gw-t1", "gateway launchpad-gw-t1"),
        ("api-key-provider", "launchpad-office-facts-key-t1", "arn:t1"),
        ("agent-role", "launchpad-agent-bar-12345678-t1",
         f"per-agent execution role ({tag}=bar)"),
        ("cdk-stack", "launchpad-memory-t1",
         "memory substrate stack (OpenSearch domain, queue, Lambda)"),
        ("cdk-stack", "launchpad-base-t1", "cloudformation stack + all resources"),
    ]


def test_teardown_default_env_skips_roles_tagged_for_another_environment(env_default, monkeypatch):
    from tests.test_teardown_script import StubControl, StubIam

    teardown = _load_script("teardown")
    monkeypatch.delenv(teardown.EC2_STACK_NAME_ENV, raising=False)
    tag = teardown.agent_iam.MANAGED_TAG_KEY
    iam = StubIam(roles={
        "launchpad-agent-foo-12345678": [{"Key": tag, "Value": "foo"}],
        "launchpad-agent-bar-12345678-t1": [{"Key": tag, "Value": "bar"},
                                            {"Key": naming.ENV_TAG_KEY, "Value": "t1"}],
    })
    clients = {"bedrock-agentcore-control": StubControl(), "iam": iam}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])
    monkeypatch.setattr(teardown.bs, "get_stack_outputs", lambda region, name=None: {})
    ids = [ident for _, ident, _ in teardown.collect_targets("cn-northwest-1")]
    assert "launchpad-agent-foo-12345678" in ids
    assert "launchpad-agent-bar-12345678-t1" not in ids
    assert ids[-2:] == ["launchpad-memory", "launchpad-base"]


def test_teardown_destroys_the_base_stack_with_the_same_cdk_context(env_t1, monkeypatch):
    teardown = _load_script("teardown")
    monkeypatch.delenv("LAUNCHPAD_CDK_CONTEXT", raising=False)
    calls = []
    monkeypatch.setattr(teardown.subprocess, "run", lambda *a, **kw: calls.append(a[0]))
    teardown.delete_target("cdk-stack", "launchpad-base-t1", "cn-northwest-1")
    assert calls == [["uv", "run", "cdk", "destroy", "launchpad-base-t1", "--force",
                      "--context", "env_name=t1"]]


def test_teardown_memory_stack_branch_keys_on_the_suffixed_name(env_t1, monkeypatch):
    from tests.test_teardown_script import StubCloudFormation

    teardown = _load_script("teardown")
    cfn = StubCloudFormation()
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: cfn)
    teardown.delete_target("cdk-stack", "launchpad-memory-t1", "cn-northwest-1")
    assert cfn.ops("delete_stack") == [{"StackName": "launchpad-memory-t1"}]
    assert cfn.waiters and cfn.waiters[0][1]["StackName"] == "launchpad-memory-t1"
