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


class _Paginator:
    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **kwargs):
        yield from self._pages


class StubIam(_Calls):
    def __init__(self, roles: dict[str, list[dict]] | None = None, inline: dict | None = None):
        super().__init__()
        self.roles = roles or {}  # name -> tags
        self.inline = inline or {}  # name -> inline policy names

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

    def delete_role(self, **kwargs):
        return self._record("delete_role", **kwargs)


def _wire(monkeypatch, control, iam, registry=None):
    clients = {
        "bedrock-agentcore-control": control,
        "agent-registry-control": registry or StubRegistryControl(),
        "iam": iam,
    }
    monkeypatch.setattr(teardown.bs, "_client", lambda service, region: clients[service])
    monkeypatch.setattr(
        teardown.bs, "get_stack_outputs", lambda region: {"ArtifactsBucketName": "b"}
    )
    monkeypatch.setattr(teardown.time, "sleep", lambda _s: None)


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


def test_collect_targets_orders_gateway_layer_before_memory_and_registry(monkeypatch):
    control, iam = _populated_account()
    control.list_memories = lambda **kw: {
        "memories": [{"id": "launchpad_memory-abc", "arn": "arn:mem"}]
    }

    class Registry(StubRegistryControl):
        def list_registries(self, **kwargs):
            return {"registries": [{"name": teardown.bs.REGISTRY_NAME,
                                    "registryId": "reg-1", "registryArn": "arn:reg"}]}

    _wire(monkeypatch, control, iam, registry=Registry())
    kinds = [kind for kind, _, _ in teardown.collect_targets("us-west-2")]
    assert kinds.index("agent-role") < kinds.index("memory") < kinds.index("registry")
    assert kinds.index("gateway") < kinds.index("memory")
    assert kinds[-1] == "cdk-stack"


def test_collect_targets_without_gateway_layer_is_unchanged(monkeypatch):
    control = StubControl()
    control.list_memories = lambda **kw: {
        "memories": [
            {"id": "launchpad_memory-abc", "arn": "arn:mem"},
            {"id": "someone_else-xyz", "arn": "arn:other"},
        ]
    }
    _wire(monkeypatch, control, StubIam())
    targets = teardown.collect_targets("us-west-2")
    assert targets == [
        ("memory", "launchpad_memory-abc", "arn:mem"),
        ("cdk-stack", teardown.bs.STACK_NAME, "cloudformation stack + all resources"),
    ]


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
    assert ops == ["list_role_policies", "delete_role_policy", "delete_role_policy", "delete_role"]
    assert iam.ops("delete_role_policy") == [
        {"RoleName": ORPHAN_ROLE, "PolicyName": "fs"},
        {"RoleName": ORPHAN_ROLE, "PolicyName": "gateway"},
    ]
    assert iam.ops("delete_role") == [{"RoleName": ORPHAN_ROLE}]


def test_scope_constants_match_the_services_that_create_the_resources():
    from app.services import agent_iam, gateway_bootstrap, kb_gateway

    assert teardown.GATEWAY_NAMES == (gateway_bootstrap.GATEWAY_NAME, kb_gateway.KB_GATEWAY_NAME)
    assert teardown.API_KEY_PROVIDER_NAME == gateway_bootstrap.API_KEY_PROVIDER_NAME
    assert teardown.GATEWAY_M2M_PROVIDER_NAME == gateway_bootstrap.GATEWAY_M2M_PROVIDER_NAME
    assert SHARED_ROLE.startswith(agent_iam._ROLE_PREFIX)  # why the tag is load-bearing
    assert SHARED_ROLE == teardown.EXECUTION_ROLE_BASE
