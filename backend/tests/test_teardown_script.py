"""scripts/teardown.py — discovery scope and per-kind deletion, with stub clients.

The script lives outside `backend/app`, so neither the client-funnel guard nor
`test_script_signatures.py` covers it; these tests load it by path and exercise
`collect_targets` / `delete_target` against an in-memory account.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "teardown.py"

GW_ID = "gw-launchpad-0001"
KB_GW_ID = "gw-kb-0002"
OTHER_GW_ID = "gw-other-0003"
SHARED_ROLE = "launchpad-agent-execution-role"
ORPHAN_ROLE = "launchpad-agent-foo-12345678"


def _load_teardown_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("launchpad_teardown_script", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


teardown = _load_teardown_script()


class _Calls:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def _record(self, op: str, **kwargs):
        self.calls.append((op, kwargs))
        return {}

    def ops(self, op: str) -> list[dict]:
        return [kw for name, kw in self.calls if name == op]


class StubControl(_Calls):
    """bedrock-agentcore-control with a configurable account."""

    def __init__(self, *, gateways=(), targets=None, api_key_providers=(), oauth_providers=()):
        super().__init__()
        self.gateways = list(gateways)
        self.targets = dict(targets or {})  # gateway_id -> [target summaries]
        self.api_key_providers = list(api_key_providers)
        self.oauth_providers = list(oauth_providers)

    # --- listings -----------------------------------------------------------
    def list_gateways(self, **kwargs):
        return {"items": self.gateways}

    def list_gateway_targets(self, gatewayIdentifier, **kwargs):
        return {"items": self.targets.get(gatewayIdentifier, [])}

    def list_api_key_credential_providers(self, **kwargs):
        return {"credentialProviders": self.api_key_providers}

    def list_oauth2_credential_providers(self, **kwargs):
        return {"credentialProviders": self.oauth_providers}

    def list_memories(self, **kwargs):
        return {"memories": []}

    def list_agent_runtimes(self, **kwargs):
        return {"agentRuntimes": []}

    # --- deletes --------------------------------------------------------------
    def delete_gateway_target(self, **kwargs):
        return self._record("delete_gateway_target", **kwargs)

    def delete_gateway(self, **kwargs):
        return self._record("delete_gateway", **kwargs)

    def delete_api_key_credential_provider(self, **kwargs):
        return self._record("delete_api_key_credential_provider", **kwargs)

    def delete_oauth2_credential_provider(self, **kwargs):
        return self._record("delete_oauth2_credential_provider", **kwargs)


class StubRegistryControl:
    def list_registries(self, **kwargs):
        return {"registries": []}


class StubCloudFormation(_Calls):
    """cloudformation client stub for the EC2-stack (N5) and memory-stack (N6)
    teardown targets."""

    def __init__(self, *, exists=True):
        super().__init__()
        self.exists = exists
        self.waiters: list[tuple[str, dict]] = []

    def describe_stacks(self, **kwargs):
        self.calls.append(("describe_stacks", kwargs))
        if not self.exists:
            raise Exception("ValidationError: Stack does not exist")
        return {"Stacks": [{"StackName": kwargs["StackName"]}]}

    def delete_stack(self, **kwargs):
        return self._record("delete_stack", **kwargs)

    def get_waiter(self, name):
        self.calls.append(("get_waiter", {"name": name}))

        class _Waiter:
            def wait(_self, **kwargs):
                self.waiters.append((name, kwargs))

        return _Waiter()


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        yield from self._pages


class StubIam(_Calls):
    def __init__(self, roles: dict[str, list[dict]] | None = None, inline: dict | None = None,
                 attached: dict | None = None):
        super().__init__()
        self.roles = roles or {}  # name -> tags
        self.inline = inline or {}  # name -> inline policy names
        self.attached = attached or {}  # name -> managed policy ARNs (N11-obs43)

    def get_paginator(self, op):
        assert op == "list_roles"
        names = sorted(self.roles)
        # Two pages, to prove the walk is paginated rather than first-page-only.
        half = max(1, len(names) // 2)
        pages = [{"Roles": [{"RoleName": n} for n in names[:half]]}]
        if names[half:]:
            pages.append({"Roles": [{"RoleName": n} for n in names[half:]]})
        return _Paginator(pages)

    def list_role_tags(self, RoleName):
        self.calls.append(("list_role_tags", {"RoleName": RoleName}))
        return {"Tags": self.roles[RoleName]}

    def get_role(self, RoleName):
        raise Exception("NoSuchEntity")

    def list_role_policies(self, RoleName):
        self.calls.append(("list_role_policies", {"RoleName": RoleName}))
        return {"PolicyNames": self.inline.get(RoleName, [])}

    def delete_role_policy(self, **kwargs):
        return self._record("delete_role_policy", **kwargs)

    def list_attached_role_policies(self, RoleName):
        self.calls.append(("list_attached_role_policies", {"RoleName": RoleName}))
        return {"AttachedPolicies": [{"PolicyArn": a} for a in self.attached.get(RoleName, [])]}

    def detach_role_policy(self, **kwargs):
        return self._record("detach_role_policy", **kwargs)

    def delete_role(self, **kwargs):
        return self._record("delete_role", **kwargs)


def _wire(monkeypatch, control, iam, registry=None, cloudformation=None, memory_exists=False,
          logs=None):
    clients = {
        "bedrock-agentcore-control": control,
        "agent-registry-control": registry or StubRegistryControl(),
        "iam": iam,
    }
    if cloudformation is not None:
        clients["cloudformation"] = cloudformation
    if logs is not None:
        clients["logs"] = logs

    def get_stack_outputs(region, name=teardown.bs.STACK_NAME):
        if name == teardown.MEMORY_STACK_NAME and not memory_exists:
            raise Exception("ValidationError: Stack does not exist")
        return {"ArtifactsBucketName": "b"}

    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])
    monkeypatch.setattr(teardown.bs, "get_stack_outputs", get_stack_outputs)
    monkeypatch.setattr(teardown.time, "sleep", lambda _s: None)


@pytest.fixture(autouse=True)
def _clear_ec2_stack_env(monkeypatch):
    """Deterministic default: no test discovers the EC2-stack target unless it opts in."""
    monkeypatch.delenv(teardown.EC2_STACK_NAME_ENV, raising=False)


def _populated_account() -> tuple[StubControl, StubIam]:
    control = StubControl(
        gateways=[
            {"gatewayId": OTHER_GW_ID, "name": "someone-else-gw"},
            {"gatewayId": KB_GW_ID, "name": teardown.KB_GATEWAY_NAME},
            {"gatewayId": GW_ID, "name": teardown.GATEWAY_NAME},
        ],
        targets={
            GW_ID: [
                {"targetId": "t-hr", "name": "hr-database"},
                {"targetId": "t-facts", "name": "office-facts"},
            ],
            KB_GW_ID: [{"targetId": "t-kb", "name": "product-docs-k5yakymmu6"}],
            OTHER_GW_ID: [{"targetId": "t-other", "name": "theirs"}],
        },
        api_key_providers=[
            {"name": "someone-else-key", "credentialProviderArn": "arn:other-key"},
            {"name": teardown.API_KEY_PROVIDER_NAME, "credentialProviderArn": "arn:facts-key"},
        ],
        oauth_providers=[
            {"name": teardown.GATEWAY_M2M_PROVIDER_NAME, "credentialProviderArn": "arn:m2m"},
            {"name": "someone-else-oauth", "credentialProviderArn": "arn:other-oauth"},
        ],
    )
    iam = StubIam(
        roles={
            ORPHAN_ROLE: [{"Key": teardown.agent_iam.MANAGED_TAG_KEY, "Value": "foo-uuid"}],
            "launchpad-agent-untagged": [],
            SHARED_ROLE: [{"Key": "aws:cloudformation:stack-name", "Value": "launchpad-base"}],
            "other-role": [{"Key": teardown.agent_iam.MANAGED_TAG_KEY, "Value": "x"}],
        }
    )
    return control, iam


def test_collect_targets_discovers_gateway_layer_dependents_first(monkeypatch):
    control, iam = _populated_account()
    _wire(monkeypatch, control, iam)

    targets = teardown.collect_targets("us-west-2")
    kinds = [kind for kind, _, _ in targets]
    ids = [ident for _, ident, _ in targets]

    # Exactly 3 targets, 2 gateways, 2 providers, 1 agent role — plus the stack.
    assert kinds == [
        "gateway-target",
        "gateway-target",
        "gateway-target",
        "gateway",
        "gateway",
        "api-key-provider",
        "oauth2-provider",
        "agent-role",
        "cdk-stack",
    ]
    assert ids[:3] == [f"{GW_ID}/t-hr", f"{GW_ID}/t-facts", f"{KB_GW_ID}/t-kb"]
    assert ids[3:5] == [GW_ID, KB_GW_ID]
    assert ids[5:8] == [
        teardown.API_KEY_PROVIDER_NAME,
        teardown.GATEWAY_M2M_PROVIDER_NAME,
        ORPHAN_ROLE,
    ]

    # Every target precedes its own gateway; the gateway layer precedes the stack.
    for gateway_id in (GW_ID, KB_GW_ID):
        last_target = max(i for i, t in enumerate(ids) if t.startswith(f"{gateway_id}/"))
        assert last_target < ids.index(gateway_id) < kinds.index("cdk-stack")

    # Nothing we did not create.
    joined = " ".join(ids)
    for stranger in (OTHER_GW_ID, "t-other", "someone-else", "untagged", SHARED_ROLE, "other-role"):
        assert stranger not in joined
    # The shared stack role was excluded before a tag lookup was even attempted.
    tagged = [kw["RoleName"] for kw in iam.ops("list_role_tags")]
    assert SHARED_ROLE not in tagged and "other-role" not in tagged


def test_collect_targets_without_gateway_layer_is_just_the_stack(monkeypatch):
    """Registry has no AWS footprint; with the memory stack not (yet) deployed
    either, there is nothing beyond the base stack to discover."""
    control = StubControl()
    _wire(monkeypatch, control, StubIam())
    targets = teardown.collect_targets("us-west-2")
    assert targets == [
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


def test_collect_targets_orders_memory_stack_before_base_stack_when_it_exists(monkeypatch):
    """R21: the memory stack imports launchpad-base's exports, so it must be
    discovered (and therefore deleted) before the base stack."""
    _wire(monkeypatch, StubControl(), StubIam(), memory_exists=True)
    targets = teardown.collect_targets("cn-northwest-1")
    assert targets == [
        (
            "cdk-stack",
            teardown.MEMORY_STACK_NAME,
            "memory substrate stack (OpenSearch domain, queue, Lambda)",
        ),
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


def test_collect_targets_full_reverse_order_ec2_then_memory_then_base(monkeypatch):
    """Full creation-order reversal (N5 + N6/R21): the EC2 control-plane stack,
    then the memory stack, then the base stack — each depends on (or is used
    by) the one after it, so each must be torn down before it."""
    monkeypatch.setenv(teardown.EC2_STACK_NAME_ENV, "launchpad-cn-ec2")
    cfn = StubCloudFormation(exists=True)
    _wire(monkeypatch, StubControl(), StubIam(), cloudformation=cfn, memory_exists=True)

    targets = teardown.collect_targets("cn-northwest-1")

    assert targets == [
        ("ec2-stack", "launchpad-cn-ec2", "control-plane EC2 CloudFormation stack"),
        (
            "cdk-stack",
            teardown.MEMORY_STACK_NAME,
            "memory substrate stack (OpenSearch domain, queue, Lambda)",
        ),
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


def test_collect_targets_orders_ec2_stack_before_cdk_stack_when_env_set(monkeypatch):
    """N5: the CN control-plane EC2 stack is the thing *using* the base stack's
    resources, so it must be discovered (and therefore deleted) before it."""
    monkeypatch.setenv(teardown.EC2_STACK_NAME_ENV, "launchpad-cn-ec2")
    cfn = StubCloudFormation(exists=True)
    _wire(monkeypatch, StubControl(), StubIam(), cloudformation=cfn)

    targets = teardown.collect_targets("cn-northwest-1")

    assert targets == [
        ("ec2-stack", "launchpad-cn-ec2", "control-plane EC2 CloudFormation stack"),
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]
    assert cfn.ops("describe_stacks") == [{"StackName": "launchpad-cn-ec2"}]


def test_collect_targets_skips_ec2_stack_when_env_unset(monkeypatch):
    _wire(monkeypatch, StubControl(), StubIam())
    targets = teardown.collect_targets("cn-northwest-1")
    assert targets == [
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


def test_collect_targets_skips_ec2_stack_when_env_set_but_stack_is_gone(monkeypatch):
    """Best-effort discovery: a stale env var naming an already-deleted stack
    must not blow up collect_targets."""
    monkeypatch.setenv(teardown.EC2_STACK_NAME_ENV, "launchpad-cn-ec2")
    cfn = StubCloudFormation(exists=False)
    _wire(monkeypatch, StubControl(), StubIam(), cloudformation=cfn)
    targets = teardown.collect_targets("cn-northwest-1")
    assert targets == [
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


def test_delete_ec2_stack_calls_cloudformation_delete_stack(monkeypatch):
    cfn = StubCloudFormation()
    _wire(monkeypatch, StubControl(), StubIam(), cloudformation=cfn)
    teardown.delete_target("ec2-stack", "launchpad-cn-ec2", "cn-northwest-1")
    assert cfn.ops("delete_stack") == [{"StackName": "launchpad-cn-ec2"}]


def test_delete_memory_stack_deletes_via_cloudformation_and_waits(monkeypatch):
    """R21: the memory stack is deleted via the raw CloudFormation API (not
    `cdk destroy`) and *waited on* to completion before teardown is allowed to
    touch the base stack — launchpad-base's deletion would otherwise race a
    memory-stack delete CloudFormation still considers in-flight."""
    cfn = StubCloudFormation()
    _wire(monkeypatch, StubControl(), StubIam(), cloudformation=cfn)
    teardown.delete_target("cdk-stack", teardown.MEMORY_STACK_NAME, "cn-northwest-1")
    assert cfn.ops("delete_stack") == [{"StackName": teardown.MEMORY_STACK_NAME}]
    assert cfn.waiters == [
        (
            "stack_delete_complete",
            {
                "StackName": teardown.MEMORY_STACK_NAME,
                "WaiterConfig": teardown.MEMORY_STACK_DELETE_WAITER_CONFIG,
            },
        )
    ]


def test_delete_base_stack_still_uses_cdk_destroy_with_the_stack_name(monkeypatch):
    """The base stack keeps the `cdk destroy` path (its removal policies, e.g.
    S3 auto-empty, are CloudFormation custom resources either way) — now
    passed the stack name explicitly, since the CDK app holds two stacks."""
    calls = []
    monkeypatch.setattr(
        teardown.subprocess, "run", lambda *a, **kw: calls.append((a, kw))
    )
    _wire(monkeypatch, StubControl(), StubIam())
    teardown.delete_target("cdk-stack", teardown.bs.STACK_NAME, "cn-northwest-1")
    (args, kwargs) = calls[0]
    assert args[0] == ["uv", "run", "cdk", "destroy", teardown.bs.STACK_NAME, "--force"]
    assert kwargs["cwd"] == teardown.REPO_ROOT / "infra" and kwargs["check"] is True


def test_delete_gateway_target_uses_gateway_and_target_ids(monkeypatch):
    control = StubControl()
    _wire(monkeypatch, control, StubIam())
    teardown.delete_target("gateway-target", f"{GW_ID}/t-hr", "us-west-2")
    assert control.ops("delete_gateway_target") == [
        {"gatewayIdentifier": GW_ID, "targetId": "t-hr"}
    ]


def test_delete_gateway_waits_for_targets_then_deletes(monkeypatch):
    listings = iter([[{"targetId": "t-hr", "status": "DELETING"}], []])
    control = StubControl()
    control.list_gateway_targets = lambda **kw: {"items": next(listings)}
    _wire(monkeypatch, control, StubIam())

    teardown.delete_target("gateway", GW_ID, "us-west-2")

    assert control.ops("delete_gateway") == [{"gatewayIdentifier": GW_ID}]


def test_delete_gateway_retries_on_conflict_then_gives_up(monkeypatch):
    class Conflict(Exception):
        pass

    Conflict.__name__ = "ConflictException"
    control = StubControl()
    attempts: list[str] = []

    def delete_gateway(**kwargs):
        attempts.append(kwargs["gatewayIdentifier"])
        raise Conflict("gateway has targets")

    control.delete_gateway = delete_gateway
    _wire(monkeypatch, control, StubIam())
    with pytest.raises(Conflict):
        teardown.delete_target("gateway", GW_ID, "us-west-2")
    assert len(attempts) == teardown.GATEWAY_DELETE_ATTEMPTS


def test_delete_gateway_does_not_retry_other_errors(monkeypatch):
    control = StubControl()

    def delete_gateway(**kwargs):
        raise RuntimeError("AccessDeniedException")

    control.delete_gateway = delete_gateway
    _wire(monkeypatch, control, StubIam())
    with pytest.raises(RuntimeError):
        teardown.delete_target("gateway", GW_ID, "us-west-2")


def test_delete_credential_providers_by_name(monkeypatch):
    control = StubControl()
    _wire(monkeypatch, control, StubIam())
    teardown.delete_target("api-key-provider", teardown.API_KEY_PROVIDER_NAME, "us-west-2")
    teardown.delete_target("oauth2-provider", teardown.GATEWAY_M2M_PROVIDER_NAME, "us-west-2")
    assert control.ops("delete_api_key_credential_provider") == [
        {"name": teardown.API_KEY_PROVIDER_NAME}
    ]
    assert control.ops("delete_oauth2_credential_provider") == [
        {"name": teardown.GATEWAY_M2M_PROVIDER_NAME}
    ]


def test_delete_agent_role_removes_inline_policies_first(monkeypatch):
    iam = StubIam(inline={ORPHAN_ROLE: ["fs", "gateway"]})
    _wire(monkeypatch, StubControl(), iam)
    teardown.delete_target("agent-role", ORPHAN_ROLE, "us-west-2")
    ops = [op for op, _ in iam.calls]
    assert ops == [
        "list_role_policies", "delete_role_policy", "delete_role_policy",
        "list_attached_role_policies",   # N11-obs43: managed attachments are detached too
        "delete_role",
    ]
    assert iam.ops("delete_role_policy") == [
        {"RoleName": ORPHAN_ROLE, "PolicyName": "fs"},
        {"RoleName": ORPHAN_ROLE, "PolicyName": "gateway"},
    ]
    assert iam.ops("delete_role") == [{"RoleName": ORPHAN_ROLE}]


def test_scope_constants_match_the_services_that_create_the_resources():
    from app.services import agent_iam, gateway_bootstrap

    # the KB connector gateway name is pinned locally: nothing in the CN build creates one
    assert teardown.GATEWAY_NAMES == (gateway_bootstrap.GATEWAY_NAME, "launchpad-kb-gw")
    assert teardown.API_KEY_PROVIDER_NAME == gateway_bootstrap.API_KEY_PROVIDER_NAME
    assert teardown.GATEWAY_M2M_PROVIDER_NAME == gateway_bootstrap.GATEWAY_M2M_PROVIDER_NAME
    assert SHARED_ROLE.startswith(agent_iam._ROLE_PREFIX)  # why the tag is load-bearing
    assert SHARED_ROLE == teardown.EXECUTION_ROLE_BASE



# ─── N12: the evaluation worker Runtime ──────────────────────────────────────

def _runtimes(*names: str) -> list[dict]:
    return [{"agentRuntimeId": f"rt-{i}", "agentRuntimeName": n} for i, n in enumerate(names)]


def _runtime_targets(targets) -> list[str]:
    return [i for k, i, _d in targets if k == "agent-runtime"]


def test_eval_worker_runtime_pattern_is_the_exact_sanitize_runtime_name_shape(monkeypatch):
    """`^<runtime_name_base(preset name)>_[0-9a-f]{6}$`, env resolved at call time, and
    exactly what `zip_runtime.sanitize_runtime_name` produces for the preset."""
    from app.deployer.zip_runtime import sanitize_runtime_name
    from app.system_agents.presets import EVAL_WORKER_BASE_NAME

    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    pattern = teardown.eval_worker_runtime_pattern()
    assert pattern.pattern == r"^launchpad_eval_worker_[0-9a-f]{6}$"
    assert pattern == teardown.agent_iam.runtime_name_pattern("launchpad-eval-worker")
    assert pattern.match(sanitize_runtime_name("launchpad-eval-worker"))
    assert teardown.agent_iam.RUNTIME_SUFFIX_HEX_LEN == 6
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "t1")
    pattern = teardown.eval_worker_runtime_pattern()
    assert pattern.pattern == r"^launchpad_eval_worker_t1_[0-9a-f]{6}$"
    assert pattern.match(sanitize_runtime_name(f"{EVAL_WORKER_BASE_NAME}-t1"))


def test_collect_targets_includes_this_environments_eval_worker_runtime(monkeypatch):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    control, iam = _populated_account()
    runtimes = [
        {"agentRuntimeId": "w-1", "agentRuntimeName": "launchpad_eval_worker_abc123"},
        {"agentRuntimeId": "o-1", "agentRuntimeName": "support_bot_abc123"},
        {"agentRuntimeId": "w-2", "agentRuntimeName": "launchpad_eval_worker_def456"},
    ]
    control.list_agent_runtimes = lambda **kwargs: {"agentRuntimes": runtimes}
    _wire(monkeypatch, control, iam)
    targets = teardown.collect_targets("us-west-2")
    kinds = [k for k, _i, _d in targets]
    assert [(k, i) for k, i, _d in targets if k == "agent-runtime"] == [
        ("agent-runtime", "w-1"), ("agent-runtime", "w-2")]
    # after the gateway layer, before the per-agent roles (the worker's own is one of them)
    assert kinds.index("oauth2-provider") < kinds.index("agent-runtime") < kinds.index("agent-role")
    assert "o-1" not in [i for _k, i, _d in targets]  # another agent's runtime is not ours


# N12-bug12: the default environment's base `launchpad_eval_worker` is a strict prefix of
# every suffixed environment's base (`launchpad_eval_worker_t1`), so a `startswith` selector
# took t1's worker `launchpad_eval_worker_t1_bd4aba` with the default environment (live
# `--dry-run`, 2026-10-11). The account below holds one worker per environment plus
# look-alikes; each environment's teardown must select exactly its own.
_CROSS_ENV_RUNTIMES = (
    "launchpad_eval_worker_ab12cd",      # default environment's worker
    "launchpad_eval_worker_t1_bd4aba",   # t1's worker — base is `launchpad_eval_worker_t1`
    "launchpad_eval_worker_t1x_ffffff",  # another env (`t1x`) — a prefix of neither
    "launchpad_eval_worker_t1_bd4aba_",  # trailing garbage: not the shape
    "launchpad_eval_worker_t1_bd4ab",    # 5 hex: not the shape
    "launchpad_eval_worker_t1_BD4ABA",   # upper-case hex: not what uuid4().hex produces
    "launchpad_eval_worker_t1_g1h2i3",   # not hex
    "launchpad_eval_worker_abc1234",     # 7 hex
    "launchpad_eval_worker",             # bare base
    "xlaunchpad_eval_worker_ab12cd",     # not anchored at the start
)


def test_default_environment_teardown_selects_only_its_own_eval_worker(monkeypatch):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    control, iam = _populated_account()
    runtimes = _runtimes(*_CROSS_ENV_RUNTIMES)
    control.list_agent_runtimes = lambda **kwargs: {"agentRuntimes": runtimes}
    _wire(monkeypatch, control, iam)
    selected = _runtime_targets(teardown.collect_targets("cn-northwest-1"))
    names = {r["agentRuntimeId"]: r["agentRuntimeName"] for r in runtimes}
    assert [names[i] for i in selected] == ["launchpad_eval_worker_ab12cd"]


def test_suffixed_environment_teardown_selects_only_its_own_eval_worker(monkeypatch):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "t1")
    control, iam = _populated_account()
    runtimes = _runtimes(*_CROSS_ENV_RUNTIMES)
    control.list_agent_runtimes = lambda **kwargs: {"agentRuntimes": runtimes}
    _wire(monkeypatch, control, iam)
    selected = _runtime_targets(teardown.collect_targets("cn-northwest-1"))
    names = {r["agentRuntimeId"]: r["agentRuntimeName"] for r in runtimes}
    assert [names[i] for i in selected] == ["launchpad_eval_worker_t1_bd4aba"]


def test_teardown_has_no_prefix_based_runtime_or_name_selector():
    """Every selector matches an exact name, an exact shape, or a scoping tag — a bare
    `startswith` on an env-suffixable base is the N12-bug12 leak. The two `startswith`
    calls left are the per-agent IAM role sweep, which is additionally gated by the
    `launchpad:agent-id` + `launchpad:env` tags (and the fixed `launchpad-agent-` prefix
    is not env-suffixed — the env goes last in the role name)."""
    import re

    source = SCRIPT.read_text()
    starts = re.findall(r"^\s*if (?:not )?name\.startswith\((.+)\):", source, re.M)
    assert sorted(starts) == ["EXECUTION_ROLE_BASE", "agent_iam._ROLE_PREFIX"]
    assert "eval_worker_runtime_prefix" not in source


def test_delete_agent_runtime_deletes_and_tolerates_a_gone_runtime(monkeypatch):
    class _Exceptions:
        class ResourceNotFoundException(Exception):
            pass

    class _Control(_Calls):
        exceptions = _Exceptions()

        def delete_agent_runtime(self, **kwargs):
            self._record("delete_agent_runtime", **kwargs)
            if kwargs["agentRuntimeId"] == "gone":
                raise self.exceptions.ResourceNotFoundException()
            return {}

    control = _Control()
    monkeypatch.setattr(teardown.bs, "_client", lambda service, _region: control)
    teardown.delete_target("agent-runtime", "w-1", "cn-northwest-1")
    teardown.delete_target("agent-runtime", "gone", "cn-northwest-1")
    assert control.ops("delete_agent_runtime") == [{"agentRuntimeId": "w-1"},
                                                   {"agentRuntimeId": "gone"}]


# ── N-SCN-obs8: runtime log groups go with each runtime the script deletes ───────────────


def test_collect_targets_puts_a_log_group_target_right_after_each_runtime(monkeypatch):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    control, iam = _populated_account()
    runtimes = [
        {"agentRuntimeId": "w-1", "agentRuntimeName": "launchpad_eval_worker_abc123"},
        {"agentRuntimeId": "o-1", "agentRuntimeName": "support_bot_abc123"},
        {"agentRuntimeId": "w-2", "agentRuntimeName": "launchpad_eval_worker_def456"},
    ]
    control.list_agent_runtimes = lambda **kwargs: {"agentRuntimes": runtimes}
    _wire(monkeypatch, control, iam)
    targets = teardown.collect_targets("us-west-2")
    kinds = ("agent-runtime", teardown.LOG_GROUPS_KIND)
    runtime_ish = [(k, i) for k, i, _d in targets if k in kinds]
    assert runtime_ish == [
        ("agent-runtime", "w-1"), (teardown.LOG_GROUPS_KIND, "w-1"),
        ("agent-runtime", "w-2"), (teardown.LOG_GROUPS_KIND, "w-2"),
    ]
    desc = next(d for k, i, d in targets if k == teardown.LOG_GROUPS_KIND and i == "w-1")
    assert desc.startswith("runtime log groups /aws/bedrock-agentcore/runtimes/w-1-*")
    assert "settle re-check at +10/30/60 s" in desc
    assert "o-1" not in [i for _k, i, _d in targets]


def test_with_log_groups_follows_skill_lab_runtimes_too_and_leaves_other_kinds_alone():
    out = teardown.with_log_groups([
        ("agent-role", "r", "role"),
        ("skill-lab-runtime", "s-1", "arn"),
        ("cdk-stack", "launchpad-base", "stack"),
    ])
    assert [(k, i) for k, i, _d in out] == [
        ("agent-role", "r"), ("skill-lab-runtime", "s-1"), (teardown.LOG_GROUPS_KIND, "s-1"),
        ("cdk-stack", "launchpad-base"),
    ]


class _StubLogs(_Calls):
    """CloudWatch Logs: ``groups`` under the prefix; ``reappear`` come back (a trailing
    flush) on the describe index given, once."""

    def __init__(self, groups, *, reappear=None, reappear_on_describe=None):
        super().__init__()
        self.groups = list(groups)
        self.reappear = list(reappear or [])
        self.reappear_on_describe = reappear_on_describe
        self.describes = 0

    def describe_log_groups(self, logGroupNamePrefix, nextToken=None):
        self.describes += 1
        if self.describes == self.reappear_on_describe:
            self.groups.extend(self.reappear)
            self.reappear = []
        return {"logGroups": [{"logGroupName": g} for g in sorted(self.groups)
                              if g.startswith(logGroupNamePrefix)]}

    def delete_log_group(self, logGroupName):
        self._record("delete_log_group", logGroupName=logGroupName)
        self.groups.remove(logGroupName)


class _GoneControl(_Calls):
    """GetAgentRuntime: DELETING for ``deleting_polls`` calls, then ResourceNotFound."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, deleting_polls=0):
        super().__init__()
        self.deleting_polls = deleting_polls

    def get_agent_runtime(self, agentRuntimeId):
        self._record("get_agent_runtime", agentRuntimeId=agentRuntimeId)
        if len(self.ops("get_agent_runtime")) <= self.deleting_polls:
            return {"agentRuntimeId": agentRuntimeId, "status": "DELETING"}
        raise self.exceptions.ResourceNotFoundException("ResourceNotFoundException")


def test_delete_runtime_log_groups_waits_for_the_runtime_deletes_and_settles(monkeypatch, capsys):
    """The runtime is DELETING for two polls; its group is deleted, re-created by the
    trailing flush before the +10 s check and deleted again; +30/+60 s find nothing."""
    control = _GoneControl(deleting_polls=2)
    group = "/aws/bedrock-agentcore/runtimes/w-1-DEFAULT"
    logs = _StubLogs([group, "/aws/bedrock-agentcore/runtimes/w-12-DEFAULT"],
                     reappear=[group], reappear_on_describe=2)
    clients = {"bedrock-agentcore-control": control, "logs": logs}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, _region: clients[service])
    slept: list[float] = []
    monkeypatch.setattr(teardown.time, "sleep", slept.append)

    teardown.delete_target(teardown.LOG_GROUPS_KIND, "w-1", "cn-northwest-1")

    assert len(control.ops("get_agent_runtime")) == 3  # DELETING ×2, then gone
    assert [kw["logGroupName"] for kw in logs.ops("delete_log_group")] == [group, group]
    assert "/aws/bedrock-agentcore/runtimes/w-12-DEFAULT" in logs.groups  # a longer id: not ours
    # two runtime-gone polls (3 s) then the settle schedule +10/+30/+60 s
    assert slept == [3, 3, 10, 20, 30]
    assert logs.describes == 4
    out = capsys.readouterr().out
    assert "settle check +10 s: 1 re-created" in out
    assert "1 log group(s) deleted · settle: 3 check(s) · 1 re-created and deleted again" in out


def test_delete_runtime_log_groups_stops_early_and_never_raises(monkeypatch, capsys):
    control = _GoneControl()
    logs = _StubLogs(["/aws/bedrock-agentcore/runtimes/w-1-DEFAULT"])
    clients = {"bedrock-agentcore-control": control, "logs": logs}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, _region: clients[service])
    slept: list[float] = []
    monkeypatch.setattr(teardown.time, "sleep", slept.append)

    teardown.delete_target(teardown.LOG_GROUPS_KIND, "w-1", "cn-northwest-1")

    assert len(logs.ops("delete_log_group")) == 1 and slept == [10, 20] and logs.describes == 3
    assert "stopped early (two consecutive empty checks)" in capsys.readouterr().out


# ─── D20 P1-d: the policy decisions log group ───────────────────────────────────────────


class StubLogs:
    def __init__(self, groups):
        self.groups = list(groups)
        self.deleted: list[str] = []

        class _Exc:
            class ResourceNotFoundException(Exception):
                pass

        self.exceptions = _Exc

    def describe_log_groups(self, logGroupNamePrefix, limit=50):  # noqa: N803
        return {"logGroups": [{"logGroupName": g} for g in self.groups
                              if g.startswith(logGroupNamePrefix)]}

    def delete_log_group(self, logGroupName):  # noqa: N803
        if logGroupName not in self.groups:
            raise self.exceptions.ResourceNotFoundException()
        self.groups.remove(logGroupName)
        self.deleted.append(logGroupName)


def test_collect_targets_includes_this_environments_policy_log_group(monkeypatch):
    """The `policy` bootstrap stage's CloudWatch group belongs to no stack, so the teardown
    lists it — after the agent roles, before the stacks — and only this environment's."""
    control, iam = _populated_account()
    logs = StubLogs(["/launchpad/policy/decisions", "/launchpad/policy/decisions-t1"])
    _wire(monkeypatch, control, iam, logs=logs)
    targets = teardown.collect_targets("us-west-2")
    kinds = [k for k, _i, _d in targets]
    assert kinds.count(teardown.POLICY_LOG_GROUP_KIND) == 1
    idx = kinds.index(teardown.POLICY_LOG_GROUP_KIND)
    assert kinds.index("agent-role") < idx < kinds.index("cdk-stack")
    assert targets[idx][1] == "/launchpad/policy/decisions"
    teardown.delete_target(teardown.POLICY_LOG_GROUP_KIND, targets[idx][1], "us-west-2")
    assert logs.deleted == ["/launchpad/policy/decisions"]
    # a second delete of a gone group is tolerated (best-effort teardown)
    teardown.delete_target(teardown.POLICY_LOG_GROUP_KIND, "/launchpad/policy/decisions",
                           "us-west-2")


def test_collect_targets_skips_the_policy_log_group_when_absent_or_undiscoverable(monkeypatch):
    control, iam = _populated_account()
    _wire(monkeypatch, control, iam, logs=StubLogs(["/launchpad/policy/decisions-t1"]))
    kinds = [k for k, _i, _d in teardown.collect_targets("us-west-2")]
    assert teardown.POLICY_LOG_GROUP_KIND not in kinds
    # no logs client at all (the other tests' wiring): discovery is best-effort
    _wire(monkeypatch, control, iam)
    kinds = [k for k, _i, _d in teardown.collect_targets("us-west-2")]
    assert teardown.POLICY_LOG_GROUP_KIND not in kinds


# ─── N11-obs43: account-level residue, gated on "no other environment remains" ──────────
# Deploy-ops 6b's teardown of the default environment left three
# `launchpad-base-OfficeFactsApiCloudWatchRole…` IAM roles (CDK retains the API Gateway
# CloudWatch role; the account-wide ApiGateway::Account setting points at it) and the
# service-created `/aws/lambda/<fn>` groups of pre-N11-obs56 stacks. User ruling 2026-10-14
# (#3): delete them ONLY when no other `launchpad-*` environment stack remains.

APIGW_ROLE = "launchpad-base-OfficeFactsApiCloudWatchRole1AA11C75-2r3WjhTageyJ"
APIGW_ROLE_T1 = "launchpad-base-t1-OfficeFactsApiCloudWatchRoleAB12CD34-QyTgf49LB2q4"
APIGW_MANAGED_POLICY = (
    "arn:aws-cn:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
)
REGION = "cn-northwest-1"


class StubCloudFormationWithStacks(StubCloudFormation):
    """cloudformation with a paginated `list_stacks` (the D17 / N11-obs43 gate)."""

    def __init__(self, *, stacks: list[str], exists=True):
        super().__init__(exists=exists)
        self.stacks = list(stacks)
        self.list_filters: list[list[str]] = []

    def get_paginator(self, op):
        assert op == "list_stacks"
        stub = self

        class _StacksPaginator:
            def paginate(_self, **kwargs):
                stub.list_filters.append(kwargs["StackStatusFilter"])
                half = max(1, len(stub.stacks) // 2)   # two pages: pagination is walked
                yield {"StackSummaries": [{"StackName": n} for n in stub.stacks[:half]]}
                yield {"StackSummaries": [{"StackName": n} for n in stub.stacks[half:]]}

        return _StacksPaginator()


class StubLogsPaged(StubLogs):
    """StubLogs with a two-page `describe_log_groups` and the prefix recorded."""

    def __init__(self, groups):
        super().__init__(groups)
        self.prefixes: list[str] = []

    def describe_log_groups(self, logGroupNamePrefix, limit=50, nextToken=None):  # noqa: N803
        self.prefixes.append(logGroupNamePrefix)
        matching = [g for g in self.groups if g.startswith(logGroupNamePrefix)]
        half = max(1, len(matching) // 2)
        if nextToken is None:
            return {"logGroups": [{"logGroupName": g} for g in matching[:half]],
                    "nextToken": "page-2"}
        return {"logGroups": [{"logGroupName": g} for g in matching[half:]]}


def _residue_account() -> tuple[StubIam, StubLogsPaged]:
    iam = StubIam(
        roles={
            APIGW_ROLE: [], APIGW_ROLE_T1: [],
            "launchpad-base-HrDatabaseServiceRole9F1-abc": [],   # goes with the stack
            "other-OfficeFactsApiCloudWatchRole1AA11C75-x": [],   # not ours
            "launchpad-base-abcd-OfficeFactsApiCloudWatchRole11-x": [],  # env > 3 chars
            ORPHAN_ROLE: [{"Key": teardown.agent_iam.MANAGED_TAG_KEY, "Value": "foo"}],
        },
        attached={APIGW_ROLE: [APIGW_MANAGED_POLICY], APIGW_ROLE_T1: [APIGW_MANAGED_POLICY]},
    )
    logs = StubLogsPaged([
        "/aws/lambda/launchpad-hr-database",
        "/aws/lambda/launchpad-office-facts-t1",
        f"/aws/lambda/launchpad-memory-extractor-{REGION}",
        "/aws/lambda/launchpad-memory-extractor-us-west-2",   # another region's stack
        "/aws/lambda/launchpad-hr-database-abcd",             # env > 3 chars: not ours
        "/aws/lambda/launchpad-something-else",
        "launchpad-base-HrDatabaseLogsABC-xyz",               # CDK-named: goes with the stack
        "/launchpad/policy/decisions-t1",
    ])
    return iam, logs


def _wire_residue(monkeypatch, cfn, iam, logs, *, existing: set[str]):
    clients = {"bedrock-agentcore-control": StubControl(), "iam": iam,
               "cloudformation": cfn, "logs": logs}
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])

    def get_stack_outputs(region, name=None):
        name = name or teardown.bs.base_stack_name()
        if name not in existing:
            raise Exception("ValidationError: Stack does not exist")
        return {}

    monkeypatch.setattr(teardown.bs, "get_stack_outputs", get_stack_outputs)


def _of_kind(targets, kind) -> list[str]:
    return [i for k, i, _d in targets if k == kind]


@pytest.mark.parametrize("name,matches", [
    (APIGW_ROLE, True),
    (APIGW_ROLE_T1, True),
    ("launchpad-base-OfficeFactsApiCloudWatchRole-x", True),       # no hash
    ("launchpad-base-abcd-OfficeFactsApiCloudWatchRole11-x", False),  # env too long
    ("launchpad-base-HrDatabaseServiceRole9F1-abc", False),
    ("launchpad-base-OfficeFactsApiCloudWatchRole", False),        # no random suffix
    ("other-OfficeFactsApiCloudWatchRole1AA11C75-x", False),
    ("launchpad-agent-execution-role", False),
])
def test_apigw_cloudwatch_role_pattern(name, matches):
    assert bool(teardown._APIGW_CLOUDWATCH_ROLE_RE.match(name)) is matches


@pytest.mark.parametrize("name,matches", [
    ("/aws/lambda/launchpad-hr-database", True),
    ("/aws/lambda/launchpad-hr-database-t1", True),
    ("/aws/lambda/launchpad-office-facts", True),
    (f"/aws/lambda/launchpad-memory-extractor-{REGION}", True),
    (f"/aws/lambda/launchpad-memory-extractor-{REGION}-t1", True),
    ("/aws/lambda/launchpad-memory-extractor-us-west-2", False),   # other region
    ("/aws/lambda/launchpad-memory-extractor", False),             # region-less: not a stack fn
    ("/aws/lambda/launchpad-hr-database-abcd", False),
    ("/aws/lambda/launchpad-something-else", False),
    ("launchpad-base-HrDatabaseLogsABC-xyz", False),
    ("/aws/bedrock-agentcore/runtimes/x-DEFAULT", False),
])
def test_lambda_log_group_pattern_is_exact_per_region(name, matches):
    assert bool(teardown.lambda_log_group_pattern(REGION).match(name)) is matches


def test_residue_constants_match_the_cdk_stacks():
    """The names are CDK's, not the backend's — pin them to the stack sources."""
    infra = Path(__file__).parents[2] / "infra" / "stacks"
    base = (infra / "base_stack.py").read_text(encoding="utf-8")
    memory = (infra / "memory_stack.py").read_text(encoding="utf-8")
    construct = teardown.APIGW_CLOUDWATCH_ROLE_CONSTRUCT
    assert construct.endswith("CloudWatchRole")   # CDK's child id on a RestApi
    assert f'"{construct[: -len("CloudWatchRole")]}"' in base   # the RestApi construct id
    for fn in teardown.SAMPLE_LAMBDA_BASE_NAMES:
        assert f'function_name=name("{fn}")' in base
    extractor = f'function_name=name(f"{teardown.MEMORY_EXTRACTOR_LAMBDA_BASE}-{{self.region}}")'
    assert extractor in memory
    assert teardown._ENV_BASE_STACK_RE.pattern.startswith("^launchpad\\-base") or \
        teardown._ENV_BASE_STACK_RE.match("launchpad-base-t1")
    assert teardown._ENV_MEMORY_STACK_RE is not None   # the D17 alias survives


def test_other_environment_stacks_is_generic_and_the_memory_alias_is_unchanged(monkeypatch):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    cfn = StubCloudFormationWithStacks(stacks=[
        "launchpad-base", "launchpad-base-t1", "launchpad-base-abcd", "launchpad-memory",
        "launchpad-memory-t2", "launchpad-memory-domain", "CDKToolkit",
    ])
    assert teardown.other_environment_stacks(cfn, "launchpad-base", "launchpad-base") == [
        "launchpad-base-t1"
    ]
    assert teardown.other_environment_memory_stacks(cfn) == ["launchpad-memory-t2"]
    assert teardown.other_environments(cfn) == ["launchpad-base-t1", "launchpad-memory-t2"]
    assert "DELETE_COMPLETE" not in cfn.list_filters[0]   # only live stacks count
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "t1")
    assert teardown.other_environments(cfn) == [
        "launchpad-base", "launchpad-memory", "launchpad-memory-t2",
    ]


def test_residue_is_deleted_after_the_base_stack_when_no_other_environment_remains(
    monkeypatch, capsys
):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    iam, logs = _residue_account()
    cfn = StubCloudFormationWithStacks(
        stacks=["launchpad-base", "launchpad-memory", "launchpad-memory-domain", "CDKToolkit"]
    )
    _wire_residue(monkeypatch, cfn, iam, logs,
                  existing={"launchpad-base", "launchpad-memory", "launchpad-memory-domain"})
    targets = teardown.collect_targets(REGION, delete_shared_domain=True)
    kinds = [k for k, _i, _d in targets]
    # every environment's orphans — by now each is one — and nothing that a stack owns
    assert _of_kind(targets, teardown.APIGW_CLOUDWATCH_ROLE_KIND) == [APIGW_ROLE, APIGW_ROLE_T1]
    assert _of_kind(targets, teardown.LAMBDA_LOG_GROUP_KIND) == [
        "/aws/lambda/launchpad-hr-database",
        "/aws/lambda/launchpad-office-facts-t1",
        f"/aws/lambda/launchpad-memory-extractor-{REGION}",
    ]
    # paginated by prefix (two pages); the D20 policy-group probe shares this client
    assert logs.prefixes.count(teardown.LAMBDA_LOG_GROUP_PREFIX) == 2
    # after launchpad-base, before the shared domain
    base_idx = targets.index(
        ("cdk-stack", "launchpad-base", "cloudformation stack + all resources")
    )
    residue_idx = [i for i, k in enumerate(kinds)
                   if k in (teardown.APIGW_CLOUDWATCH_ROLE_KIND, teardown.LAMBDA_LOG_GROUP_KIND)]
    assert min(residue_idx) > base_idx
    assert max(residue_idx) < kinds.index("cdk-stack", base_idx + 1)   # the domain stack
    assert "kept while" not in capsys.readouterr().out
    # deletion: managed policy detached before the role goes; a gone group is tolerated
    teardown.delete_target(teardown.APIGW_CLOUDWATCH_ROLE_KIND, APIGW_ROLE, REGION)
    assert iam.ops("detach_role_policy") == [
        {"RoleName": APIGW_ROLE, "PolicyArn": APIGW_MANAGED_POLICY}
    ]
    assert iam.ops("delete_role") == [{"RoleName": APIGW_ROLE}]
    names = [n for n, _kw in iam.calls]
    assert names.index("detach_role_policy") < names.index("delete_role")
    group = "/aws/lambda/launchpad-hr-database"
    teardown.delete_target(teardown.LAMBDA_LOG_GROUP_KIND, group, REGION)
    teardown.delete_target(teardown.LAMBDA_LOG_GROUP_KIND, group, REGION)   # gone: tolerated
    assert logs.deleted == ["/aws/lambda/launchpad-hr-database"]


@pytest.mark.parametrize("env_name,live,expected_others", [
    ("", ["launchpad-base", "launchpad-memory", "launchpad-base-t1", "launchpad-memory-t1"],
     "launchpad-base-t1, launchpad-memory-t1"),
    ("t1", ["launchpad-base", "launchpad-memory", "launchpad-base-t1", "launchpad-memory-t1"],
     "launchpad-base, launchpad-memory"),
    # a half-deployed sibling (memory stack only) still counts
    ("", ["launchpad-base", "launchpad-memory-t2"], "launchpad-memory-t2"),
])
def test_residue_is_kept_and_listed_while_another_environment_remains(
    monkeypatch, capsys, env_name, live, expected_others
):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: env_name)
    iam, logs = _residue_account()
    cfn = StubCloudFormationWithStacks(stacks=[*live, "launchpad-memory-domain"])
    own_base = teardown.bs.base_stack_name()
    _wire_residue(monkeypatch, cfn, iam, logs, existing={own_base, teardown.mb.memory_stack_name()})
    targets = teardown.collect_targets(REGION)
    kinds = {k for k, _i, _d in targets}
    assert teardown.APIGW_CLOUDWATCH_ROLE_KIND not in kinds
    assert teardown.LAMBDA_LOG_GROUP_KIND not in kinds
    out = capsys.readouterr().out
    assert f"note: shared — kept while {expected_others} exist:" in out
    assert f"[{teardown.APIGW_CLOUDWATCH_ROLE_KIND}] {APIGW_ROLE}" in out
    assert f"[{teardown.LAMBDA_LOG_GROUP_KIND}] /aws/lambda/launchpad-hr-database" in out
    assert iam.ops("delete_role") == [] and logs.deleted == []


def test_residue_discovery_is_best_effort_and_silent_without_candidates(monkeypatch, capsys):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    # a matching role but no cloudformation/logs client at all: nothing is listed, nothing raised
    iam, _logs = _residue_account()
    _wire(monkeypatch, StubControl(), iam)
    kinds = {k for k, _i, _d in teardown.collect_targets(REGION)}
    assert teardown.APIGW_CLOUDWATCH_ROLE_KIND not in kinds
    # no candidates: the stack listing is never consulted and no note is printed
    cfn = StubCloudFormationWithStacks(stacks=["launchpad-base-t1"])
    _wire_residue(monkeypatch, cfn, StubIam(), StubLogsPaged([]), existing={"launchpad-base"})
    teardown.collect_targets(REGION)
    assert cfn.list_filters == []
    assert "kept while" not in capsys.readouterr().out


def test_delete_agent_role_still_works_when_nothing_is_attached(monkeypatch):
    iam = StubIam(inline={ORPHAN_ROLE: ["fs"]})
    _wire(monkeypatch, StubControl(), iam)
    teardown.delete_target("agent-role", ORPHAN_ROLE, "us-west-2")
    assert iam.ops("detach_role_policy") == []
    assert iam.ops("delete_role") == [{"RoleName": ORPHAN_ROLE}]


def test_main_dry_run_prints_the_kept_note_then_the_last_environment_lists_the_residue(
    monkeypatch, capsys
):
    monkeypatch.setattr(teardown.naming, "current_env_name", lambda: "")
    iam, logs = _residue_account()
    cfn = StubCloudFormationWithStacks(
        stacks=["launchpad-base", "launchpad-memory", "launchpad-base-t1"]
    )
    _wire_residue(monkeypatch, cfn, iam, logs, existing={"launchpad-base", "launchpad-memory"})
    monkeypatch.setattr(teardown.sys, "argv", ["teardown.py", "--dry-run", "--region", REGION])
    assert teardown.main() == 0
    out = capsys.readouterr().out
    assert "note: shared — kept while launchpad-base-t1 exist:" in out
    # listed under the note (4-space indent), never as a 2-space target line
    assert f"\n  [{teardown.APIGW_CLOUDWATCH_ROLE_KIND}] {APIGW_ROLE}" not in out
    assert "dry-run — nothing deleted" in out
    cfn.stacks.remove("launchpad-base-t1")
    assert teardown.main() == 0
    out = capsys.readouterr().out
    assert "kept while" not in out
    assert out.index("[cdk-stack] launchpad-base") < out.index(
        f"[{teardown.APIGW_CLOUDWATCH_ROLE_KIND}] {APIGW_ROLE}"
    ) < out.index("dry-run — nothing deleted")
    assert iam.ops("delete_role") == [] and logs.deleted == []   # a dry run deletes nothing
