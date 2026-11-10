"""`stacks/naming.py` — the EnvName suffix rule, and its two guarantees (N11), plus the
D17 carve-out (the shared OpenSearch domain stack is never suffixed).

1. With the default empty env name, every stack name, physical resource name and
   export name is byte-for-byte what it was before the knob existed. The reference
   is `tests/fixtures/physical_names_default.json`, a snapshot taken from a synth of
   the pre-EnvName code (commit b5ee527) — not regenerated from the code under test.
   D17 moved the domain and the admin role out of `launchpad-memory` into
   `launchpad-memory-domain`: the *set* of physical names is still exactly the pre-D17
   one (asserted against that same fixture), and their per-stack placement is pinned by
   `tests/fixtures/physical_names_default_d17.json` (snapshot of the first D17 synth).
2. With `-c env_name=t1`, every name of the two per-environment stacks ends in `-t1`,
   still starts with the prefix the EC2 ops role's IAM wildcards match on, and stays
   inside each service's own name limit even for the longest allowed env name — while
   the domain stack and its two names are byte-identical to the default's.
"""

import json
import re
from pathlib import Path

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from stacks.base_stack import LaunchpadBaseStack
from stacks.memory_domain_stack import DOMAIN_STACK_NAME, LaunchpadMemoryDomainStack
from stacks.memory_stack import LaunchpadMemoryStack
from stacks.naming import (
    ENV_NAME_MAX_LEN,
    ENV_NAME_PATTERN,
    env_name_from_context,
    suffixed,
    validate_env_name,
)

SNAPSHOT = Path(__file__).parent / "fixtures" / "physical_names_default.json"
SNAPSHOT_D17 = Path(__file__).parent / "fixtures" / "physical_names_default_d17.json"
ACCOUNT = "111111111111"
REGION = "cn-northwest-1"

# The CloudFormation property that carries each resource type's physical name.
NAME_PROPS = {
    "AWS::IAM::Role": "RoleName",
    "AWS::S3::Bucket": "BucketName",
    "AWS::ECR::Repository": "RepositoryName",
    "AWS::CodeBuild::Project": "Name",
    "AWS::Lambda::Function": "FunctionName",
    "AWS::SQS::Queue": "QueueName",
    "AWS::OpenSearchService::Domain": "DomainName",
    "AWS::ApiGateway::RestApi": "Name",
    "AWS::ApiGateway::ApiKey": "Name",
    "AWS::ApiGateway::UsagePlan": "UsagePlanName",
    "AWS::SSM::Parameter": "Name",
}

# Service limits for the name kinds this app fixes (characters).
NAME_LIMITS = {
    "AWS::IAM::Role": 64,
    "AWS::S3::Bucket": 63,
    "AWS::ECR::Repository": 256,
    "AWS::CodeBuild::Project": 255,
    "AWS::Lambda::Function": 64,
    "AWS::SQS::Queue": 80,
    "AWS::OpenSearchService::Domain": 28,
    "AWS::ApiGateway::RestApi": 1024,
    "AWS::ApiGateway::ApiKey": 1024,
    "AWS::ApiGateway::UsagePlan": 1024,
    "AWS::SSM::Parameter": 2048,
}


def _synth(env_name: str | None):
    """(base, memory) — the two per-environment stacks, as every test below reads them;
    the shared domain stack is reachable as `base.node.root`'s third child (`_domain`)."""
    context = {} if env_name is None else {"env_name": env_name}
    app = cdk.App(context=context)
    env = cdk.Environment(account=ACCOUNT, region=REGION)
    base_id = suffixed("launchpad-base", env_name or "")
    memory_id = suffixed("launchpad-memory", env_name or "")
    base = LaunchpadBaseStack(app, base_id, env=env)
    domain = LaunchpadMemoryDomainStack(app, DOMAIN_STACK_NAME, env=env)
    mem = LaunchpadMemoryStack(app, memory_id, base=base, env=env)
    mem.add_dependency(domain)
    return base, mem


def _domain(base):
    children = base.node.root.node.children
    return next(c for c in children if isinstance(c, LaunchpadMemoryDomainStack))


def _names_of(tpl: Template) -> dict:
    body = tpl.to_json()
    resources = {}
    for logical_id, res in body["Resources"].items():
        prop = NAME_PROPS.get(res["Type"])
        if prop and prop in res.get("Properties", {}):
            resources[logical_id] = {"type": res["Type"], "name": res["Properties"][prop]}
    exports = {
        key: out["Export"]["Name"]
        for key, out in body.get("Outputs", {}).items()
        if "Export" in out
    }
    return {"resources": dict(sorted(resources.items())), "exports": dict(sorted(exports.items()))}


def _snapshot_of(base, mem) -> dict:
    domain = _domain(base)
    return {
        "stacks": [base.stack_name, domain.stack_name, mem.stack_name],
        base.stack_name: _names_of(Template.from_stack(base)),
        domain.stack_name: _names_of(Template.from_stack(domain)),
        mem.stack_name: _names_of(Template.from_stack(mem)),
    }


def _physical_names(snapshot: dict, *stacks: str) -> set[str]:
    return {
        entry["name"]
        for stack in stacks
        for entry in snapshot[stack]["resources"].values()
        if entry["type"] != "AWS::SSM::Parameter"  # D17-new, not a pre-D17 name
    }


# ── the helper itself ───────────────────────────────────────────────────────


def test_suffixed_is_identity_for_the_empty_env_name():
    assert suffixed("launchpad-base", "") == "launchpad-base"
    assert suffixed("launchpad-agent-execution-role-cn-northwest-1", "") == (
        "launchpad-agent-execution-role-cn-northwest-1"
    )


def test_suffixed_appends_dash_env_at_the_end_exactly_once():
    assert suffixed("launchpad-base", "t1") == "launchpad-base-t1"
    assert suffixed("launchpad-agent-execution-role-cn-northwest-1", "t1") == (
        "launchpad-agent-execution-role-cn-northwest-1-t1"
    )
    assert suffixed("lp-memory-cn-northwest-1", "t1") == "lp-memory-cn-northwest-1-t1"
    # applying the rule to an already-suffixed base is a caller bug, not idempotent
    assert suffixed(suffixed("x", "t1"), "t1") == "x-t1-t1"


@pytest.mark.parametrize("bad", ["T1", "t-1", "t_1", "t1 ", " t1", "abcd", "t.1", "中"])
def test_env_name_rejects_anything_outside_the_pattern(bad):
    with pytest.raises(ValueError, match="env_name"):
        validate_env_name(bad)
    with pytest.raises(ValueError):
        suffixed("launchpad-base", bad)


@pytest.mark.parametrize("ok", ["", "a", "t1", "abc", "999"])
def test_env_name_accepts_lowercase_alphanumerics_up_to_the_cap(ok):
    assert validate_env_name(ok) == ok
    assert re.fullmatch(ENV_NAME_PATTERN, ok)


def test_env_name_pattern_is_the_one_the_other_two_layers_share():
    """The EC2 template's `EnvName` AllowedPattern and the backend's `Settings.env_name`
    validator must stay identical to this regex — the other suites assert the same
    literal, so a drift in any layer fails somewhere."""
    assert ENV_NAME_PATTERN == r"^[a-z0-9]{0,3}$"
    assert ENV_NAME_MAX_LEN == 3


def test_env_name_from_context_treats_missing_as_empty():
    app = cdk.App()
    assert env_name_from_context(app.node) == ""
    assert env_name_from_context(cdk.App(context={"env_name": "t1"}).node) == "t1"
    with pytest.raises(ValueError):
        env_name_from_context(cdk.App(context={"env_name": "Too-Long"}).node)


# ── guarantee 1: the default is byte-identical to before ──────────────────────


@pytest.mark.parametrize("env_name", [None, ""])
def test_default_env_name_reproduces_the_pre_envname_snapshot_exactly(env_name):
    """The pre-EnvName fixture is the reference for every *physical name*: the base stack
    is byte-identical, and the memory stack's six names are exactly the union of what the
    D17 split now puts in `launchpad-memory-domain` (domain + admin role) and
    `launchpad-memory` (queue, DLQ, extractor role, extractor Lambda)."""
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    got = _snapshot_of(*_synth(env_name))
    assert got["launchpad-base"] == expected["launchpad-base"]
    assert _physical_names(got, "launchpad-memory-domain", "launchpad-memory") == (
        _physical_names(expected, "launchpad-memory")
    )
    assert got["launchpad-memory"]["exports"] == got["launchpad-memory-domain"]["exports"] == {}


@pytest.mark.parametrize("env_name", [None, ""])
def test_default_env_name_reproduces_the_d17_placement_snapshot_exactly(env_name):
    expected = json.loads(SNAPSHOT_D17.read_text(encoding="utf-8"))
    assert _snapshot_of(*_synth(env_name)) == expected


def test_snapshot_fixture_covers_every_fixed_name_kind():
    """Guards the snapshot itself: if a new named resource kind is added to a stack,
    this fails until NAME_PROPS (and the fixture) learn about it."""
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    expected_d17 = json.loads(SNAPSHOT_D17.read_text(encoding="utf-8"))
    base, mem = _synth("")
    for stack in (base, _domain(base), mem):
        body = Template.from_stack(stack).to_json()
        for res in body["Resources"].values():
            props = res.get("Properties", {})
            for prop in ("RoleName", "BucketName", "RepositoryName", "FunctionName",
                         "QueueName", "DomainName", "UsagePlanName", "Name"):
                # a literal string is a physical name we fix; an intrinsic ({"Ref": ..},
                # {"Fn::GetAtt": ..}) is a *reference* to one (Lambda::Permission,
                # Custom::S3AutoDeleteObjects) and belongs to the resource it points at
                if isinstance(props.get(prop), str):
                    assert res["Type"] in NAME_PROPS, res["Type"]
    assert len(expected["launchpad-base"]["resources"]) == 10
    assert len(expected["launchpad-memory"]["resources"]) == 6
    assert len(expected_d17["launchpad-memory-domain"]["resources"]) == 2 + 3  # + SSM params
    assert len(expected_d17["launchpad-memory"]["resources"]) == 4


# ── guarantee 2: env_name suffixes every fixed name, within limits ────────────


@pytest.fixture(scope="module")
def t1():
    return _synth("t1")


def test_stack_names_carry_the_suffix(t1):
    base, mem = t1
    assert (base.stack_name, mem.stack_name) == ("launchpad-base-t1", "launchpad-memory-t1")


def test_every_fixed_physical_name_ends_with_the_suffix(t1):
    base, mem = t1
    snap = _snapshot_of(base, mem)
    default = json.loads(SNAPSHOT_D17.read_text(encoding="utf-8"))
    for default_stack, stack in (("launchpad-base", base), ("launchpad-memory", mem)):
        got = snap[stack.stack_name]["resources"]
        want = default[default_stack]["resources"]
        assert set(got) == set(want), "resource set changed with env_name"
        for logical_id, entry in got.items():
            assert entry["name"] == f"{want[logical_id]['name']}-t1", logical_id
            assert entry["name"].count("-t1") == 1, f"suffix applied twice on {logical_id}"


def test_the_shared_domain_stack_ignores_the_env_name(t1):
    """D17: the domain stack, its domain, admin role and SSM parameters are the same
    objects for every environment — `-c env_name=t1` must not touch any of them."""
    base, mem = t1
    snap = _snapshot_of(base, mem)
    default = json.loads(SNAPSHOT_D17.read_text(encoding="utf-8"))
    assert snap["launchpad-memory-domain"] == default["launchpad-memory-domain"]
    assert "launchpad-memory-domain" in snap["stacks"]
    assert Template.from_stack(_domain(base)).to_json() == Template.from_stack(
        _domain(_synth("")[0])
    ).to_json()


def test_suffixed_names_keep_the_prefixes_the_ops_role_wildcards_match(t1):
    """infra/ec2/launchpad-cn-ec2.yaml scopes the ops role to role/launchpad-*,
    repository/launchpad-*, project/launchpad-* and sqs launchpad-memory-extract-*."""
    base, mem = t1
    snap = _snapshot_of(base, mem)
    for stack in (base, mem):
        for entry in snap[stack.stack_name]["resources"].values():
            if entry["type"] in ("AWS::IAM::Role", "AWS::ECR::Repository",
                                 "AWS::CodeBuild::Project"):
                assert entry["name"].startswith("launchpad-"), entry
            if entry["type"] == "AWS::SQS::Queue":
                assert entry["name"].startswith("launchpad-memory-extract-"), entry


def test_cross_stack_exports_follow_the_suffixed_stack_name(t1):
    base, mem = t1
    exports = _snapshot_of(base, mem)[base.stack_name]["exports"]
    assert exports, "the memory stack imports base outputs; exports must exist"
    for name in exports.values():
        assert name.startswith("launchpad-base-t1:"), name


def test_hand_built_queue_arn_in_the_exec_role_policy_uses_the_suffixed_queue(t1):
    """memory_stack.py builds the extraction queue's ARN by hand (no cross-stack
    token). It must name the *suffixed* queue or the hub exec role can never send."""
    _, mem = t1
    text = json.dumps(Template.from_stack(mem).to_json())
    assert "launchpad-memory-extract-cn-northwest-1-t1" in text
    assert ':launchpad-memory-extract-cn-northwest-1"' not in text


def test_longest_env_name_stays_inside_every_service_limit():
    longest = "z" * ENV_NAME_MAX_LEN
    base, mem = _synth(longest)
    snap = _snapshot_of(base, mem)
    for stack in (base, mem):
        for logical_id, entry in snap[stack.stack_name]["resources"].items():
            limit = NAME_LIMITS[entry["type"]]
            assert len(entry["name"]) <= limit, (logical_id, entry["name"], limit)
    # the former binding constraint: the OpenSearch domain name. Since D17 it is shared
    # and unsuffixed, so the longest env name no longer reaches the 28-character cap.
    (domain,) = [
        e for e in snap["launchpad-memory-domain"]["resources"].values()
        if e["type"] == "AWS::OpenSearchService::Domain"
    ]
    assert domain["name"] == f"lp-memory-{REGION}"
    assert len(domain["name"]) <= 28
    assert re.fullmatch(r"[a-z][a-z0-9-]{2,27}", domain["name"])
    # stack names: CloudFormation allows 128
    assert max(len(base.stack_name), len(mem.stack_name)) <= 128


def test_env_name_context_is_validated_at_synth():
    with pytest.raises(ValueError, match="env_name"):
        _synth("Prod")
