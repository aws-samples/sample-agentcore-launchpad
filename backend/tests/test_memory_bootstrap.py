"""Hub + workspace memory substrate bootstrap: resource mapping and the idempotent
OpenSearch FGAC provisioning (ensure_memory_substrate, map_agent_role)."""

from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.services import memory_bootstrap as mb
from app.services import opensearch_http as core
from tests.memory_fakes import FakeOpenSearch

INFRA = Path(__file__).parents[2] / "infra"

OUTPUTS = {
    "MemoryDomainEndpoint": "search-lp-memory-abc.cn-northwest-1.es.example",
    "MemoryDomainArn": "arn:aws-cn:es:cn-northwest-1:111:domain/lp-memory-cn-northwest-1",
    "MemoryDomainName": "lp-memory-cn-northwest-1",
    "MemoryQueueUrl": "https://sqs.cn-northwest-1.example/111/launchpad-memory-extract-cn-northwest-1",
    "MemoryQueueArn": "arn:aws-cn:sqs:cn-northwest-1:111:launchpad-memory-extract-cn-northwest-1",
    "MemoryExtractorRoleArn": "arn:aws-cn:iam::111:role/launchpad-memory-extractor-cn-northwest-1",
    "MemoryExtractorFunctionName": "launchpad-memory-extractor-cn-northwest-1",
    "MemoryAdminRoleArn": "arn:aws-cn:iam::111:role/launchpad-memory-admin-cn-northwest-1",
}


def test_memory_resources_map_every_output_and_clear_the_reason():
    res = mb.memory_resources(OUTPUTS, bucket="bkt", store_id="launchpad_memory-default")
    assert set(res) == set(mb.MEMORY_RESOURCE_KEYS) | {"memory_unavailable_reason"}
    assert res["memory_id"] == "launchpad_memory-default" and res["memory_bucket"] == "bkt"
    assert res["memory_prefix"] == "memory"
    assert res["memory_os_endpoint"] == OUTPUTS["MemoryDomainEndpoint"]
    assert res["memory_index"] == "launchpad-memory"
    assert res["memory_admin_role_arn"] == OUTPUTS["MemoryAdminRoleArn"]
    assert res["memory_unavailable_reason"] == ""
    assert (
        mb.memory_resources({**OUTPUTS, "MemoryAdminRoleArn": ""}, bucket="b", store_id="s")[
            "memory_admin_role_arn"
        ]
        == ""
    )


def _server_normalized_role(body: dict) -> dict:
    """What a *live* OpenSearch security plugin actually hands back on
    `GET _plugins/_security/api/roles/<role>` — never byte-identical to the PUT
    body even when nothing changed. It adds `reserved`/`hidden`/`static` at the
    role's top level and `dls`/`fls`/`masked_fields` to every `index_permissions`
    entry, and is free to reorder lists. A fake that just echoes the PUT body back
    verbatim (as this one used to) hides exactly the bug `_security_role_matches`
    (N6 node-fix round) exists to tolerate — simulating it here keeps the
    `test_ensure_memory_substrate_creates_then_reuses` reuse/PUT-count assertions
    honest."""
    out = dict(body)
    out.setdefault("reserved", False)
    out.setdefault("hidden", False)
    out.setdefault("static", False)
    out["index_permissions"] = [
        {**entry, "dls": "", "fls": [], "masked_fields": []}
        for entry in reversed(out.get("index_permissions") or [])
    ]
    out["cluster_permissions"] = list(reversed(out.get("cluster_permissions") or []))
    return out


class SecOS(FakeOpenSearch):
    """FakeOpenSearch + the two security-plugin endpoints (GET 404 → PUT). The
    roles endpoint's GET response is server-normalized (`_server_normalized_role`)
    the way real OpenSearch's would be; the rolesmapping endpoint echoes verbatim,
    since that is a plain `{"users": [...]}` document the plugin does not augment."""

    def __init__(self):
        super().__init__()
        self.security: dict[str, dict] = {}

    def request(self, method, path, body=None, params=None):
        self.requests.append((method, path, body))
        if path.startswith("/_plugins/_security/api/"):
            if method == "GET":
                if path in self.security:
                    live = self.security[path]
                    if "/api/roles/" in path:
                        live = _server_normalized_role(live)
                    return {path.rsplit("/", 1)[-1]: live}
                raise core.OpenSearchError(404, "not found")
            self.security[path] = body
            return {"status": "CREATED"}
        return self.mapping_request(method, path, body) or {}


def test_ensure_memory_substrate_creates_then_reuses():
    os_ = SecOS()
    log = []
    kw = dict(
        exec_role_arn="arn:aws-cn:iam::111:role/exec",
        extractor_role_arn="arn:aws-cn:iam::111:role/extractor",
        agent_role_pattern="arn:aws-cn:iam::111:role/launchpad-agent-*",
        log=log.append,
    )
    out = mb.ensure_memory_substrate(os_, **kw)
    assert out["created"] == [
        "index launchpad-memory",
        "index launchpad-jobs",
        "index launchpad-kb-docs",
        "security role launchpad_memory_rw",
        "role mapping launchpad_memory_rw",
    ]
    assert out["reused"] == []
    role = os_.security["/_plugins/_security/api/roles/launchpad_memory_rw"]
    # D17: the default environment enumerates its own index families — never
    # `launchpad-*`, which would also match every `launchpad-<env>-…` index on the
    # shared domain.
    assert role["index_permissions"][0]["index_patterns"] == [
        "launchpad-memory*", "launchpad-jobs*", "launchpad-kb-*",
    ]
    assert "indices_all" in role["index_permissions"][0]["allowed_actions"]
    assert set(role["cluster_permissions"]) == {"cluster_composite_ops", "cluster_monitor"}
    assert os_.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw"] == {
        "users": [
            "arn:aws-cn:iam::111:role/exec",
            "arn:aws-cn:iam::111:role/extractor",
            "arn:aws-cn:iam::111:role/launchpad-agent-*",
        ]
    }
    again = mb.ensure_memory_substrate(os_, **kw)
    # r14-a: the reused memory index also reports its v2 mapping check (nothing to add)
    assert again["created"] == [] and len(again["reused"]) == 6
    assert "mapping launchpad-memory v2 fields" in again["reused"]
    assert len([r for r in os_.requests if r[0] == "PUT"]) == 2  # no second PUT


def test_map_agent_role_appends_once():
    os_ = SecOS()
    mb.ensure_memory_substrate(
        os_, exec_role_arn="e", extractor_role_arn="x", agent_role_pattern="p", log=lambda m: None
    )
    mb.map_agent_role(os_, "arn:aws-cn:iam::111:role/launchpad-agent-a-1234")
    mb.map_agent_role(os_, "arn:aws-cn:iam::111:role/launchpad-agent-a-1234")
    assert os_.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw"]["users"] == [
        "e",
        "x",
        "p",
        "arn:aws-cn:iam::111:role/launchpad-agent-a-1234",
    ]


def test_ensure_memory_substrate_preserves_fallback_mapped_roles():
    """R13a: an agent role `map_agent_role` appended between bootstrap runs must
    survive the next `ensure_memory_substrate` pass — the mapping is a superset
    check, not exact equality, so it must be reported `reused` with zero PUTs."""
    os_ = SecOS()
    kw = dict(exec_role_arn="e", extractor_role_arn="x", agent_role_pattern="p", log=lambda m: None)
    mb.ensure_memory_substrate(os_, **kw)
    mb.map_agent_role(os_, "arn:aws-cn:iam::111:role/launchpad-agent-extra")
    puts_before = len([r for r in os_.requests if r[0] == "PUT"])

    out = mb.ensure_memory_substrate(os_, **kw)

    mapping = os_.security["/_plugins/_security/api/rolesmapping/launchpad_memory_rw"]
    assert "arn:aws-cn:iam::111:role/launchpad-agent-extra" in mapping["users"]
    assert "role mapping launchpad_memory_rw" in out["reused"]
    assert "role mapping launchpad_memory_rw" not in out["created"]
    assert len([r for r in os_.requests if r[0] == "PUT"]) == puts_before


def test_ensure_memory_substrate_unions_a_missing_entry_without_dropping_live_ones():
    """A live mapping that drifted to miss one desired entry (e.g. the extractor
    role) is repaired by PUTting the union of what is live and what is desired —
    never by overwriting with just the desired three, which would drop whatever
    `map_agent_role` had already added."""
    os_ = SecOS()
    kw = dict(exec_role_arn="e", extractor_role_arn="x", agent_role_pattern="p", log=lambda m: None)
    mb.ensure_memory_substrate(os_, **kw)
    mb.map_agent_role(os_, "arn:aws-cn:iam::111:role/launchpad-agent-extra")
    path = "/_plugins/_security/api/rolesmapping/launchpad_memory_rw"
    os_.security[path]["users"] = ["e", "p", "arn:aws-cn:iam::111:role/launchpad-agent-extra"]

    out = mb.ensure_memory_substrate(os_, **kw)

    assert set(os_.security[path]["users"]) == {
        "e",
        "p",
        "x",
        "arn:aws-cn:iam::111:role/launchpad-agent-extra",
    }
    assert "role mapping launchpad_memory_rw" in out["created"]


def test_security_role_matches_a_server_normalized_and_reordered_live_body():
    """`_security_role_matches` (N6 node-fix round): the server-added keys
    (`dls`/`fls`/`masked_fields` per `index_permissions` entry, `reserved`/
    `hidden`/`static` at the role's top level) and reordered lists must NOT count
    as drift — only a real change (a missing allowed action) should."""
    desired = mb.security_role_body()
    live_same = _server_normalized_role(desired)
    assert mb._security_role_matches(desired, live_same)

    live_narrower = _server_normalized_role({
        **desired,
        "index_permissions": [
            {"index_patterns": ["launchpad-*"], "allowed_actions": ["read"]},
        ],
    })
    assert not mb._security_role_matches(desired, live_narrower)


def test_ensure_memory_substrate_second_pass_is_all_reused_with_zero_puts():
    """The N9 gate: a bootstrap run against a *live* OpenSearch (whose GET
    normalizes the role document, simulated by `SecOS`/`_server_normalized_role`)
    must report everything `reused` on the second pass and issue zero PUTs — the
    bug this fixes made every re-run PUT the security role unconditionally."""
    os_ = SecOS()
    kw = dict(exec_role_arn="e", extractor_role_arn="x", agent_role_pattern="p", log=lambda m: None)
    mb.ensure_memory_substrate(os_, **kw)
    puts_before = len([r for r in os_.requests if r[0] == "PUT"])

    out = mb.ensure_memory_substrate(os_, **kw)

    assert out["created"] == []
    assert set(out["reused"]) == {
        "index launchpad-memory", "index launchpad-jobs", "index launchpad-kb-docs",
        "mapping launchpad-memory v2 fields",  # r14-a: present → no PUT
        "security role launchpad_memory_rw", "role mapping launchpad_memory_rw",
    }
    assert len([r for r in os_.requests if r[0] == "PUT"]) == puts_before


def test_shared_memory_kind_counts_the_cn_substrate_and_the_global_arn():
    """N-ARCH-bug1: the one predicate the architect assistant, the proposal contract
    and the console use instead of testing the Global build's `memory_arn`."""
    cn = mb.memory_resources(OUTPUTS, bucket="bkt", store_id="launchpad_memory-default")
    assert mb.shared_memory_kind(cn) == mb.MEMORY_KIND_SELFHOSTED == "selfhosted"
    assert mb.workspace_has_shared_memory(cn) is True
    # a failed bootstrap leaves only (or additionally) a reason → nothing to bind
    assert mb.shared_memory_kind(mb.unavailable("launchpad-memory stack not found")) is None
    assert mb.workspace_has_shared_memory({**cn, "memory_unavailable_reason": "x"}) is False
    # the deployer binds on endpoint + store id: either missing means no memory
    assert mb.workspace_has_shared_memory({**cn, "memory_os_endpoint": ""}) is False
    assert mb.workspace_has_shared_memory({**cn, "memory_id": None}) is False
    # the Global build's AgentCore Memory
    arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:memory/launchpad_memory-x"
    assert mb.shared_memory_kind({"memory_arn": arn}) == mb.MEMORY_KIND_AGENTCORE
    assert mb.workspace_has_shared_memory({"memory_arn": arn}) is True
    # the architect catalog's compact snapshot carries the resolved kind (no substrate keys)
    snapshot = {"memory_arn": None, "memory_id": "s", "memory_kind": "selfhosted"}
    assert mb.shared_memory_kind(snapshot) == "selfhosted"
    assert mb.shared_memory_kind({**snapshot, "memory_id": None, "memory_kind": None}) is None
    # nothing configured
    for empty in ({}, None, {"execution_role_arn": "arn:aws-cn:iam::111:role/x"}):
        assert mb.shared_memory_kind(empty) is None
        assert mb.workspace_has_shared_memory(empty) is False


def test_memory_admin_role_name_and_statement_mirror_the_infra_sources():
    """N11-obs44a: the reason the bootstrap memory stage prints names the role the CDK
    domain stack creates and the Sid the EC2 template grants — both are mirrored
    literals here, so a rename on either side fails this test."""
    assert mb.memory_admin_role_name("cn-northwest-1") == "launchpad-memory-admin-cn-northwest-1"
    stack = (INFRA / "stacks" / "memory_domain_stack.py").read_text()
    assert 'f"launchpad-memory-admin-{region}"' in stack
    template = (INFRA / "ec2" / "launchpad-cn-ec2.yaml").read_text()
    assert f"Sid: {mb.MEMORY_ADMIN_ASSUME_SID}" in template
    assert f"role/{mb.MEMORY_ADMIN_ROLE_PREFIX}*" in template
    reason = mb.assume_denied_reason(
        "arn:aws-cn:iam::111:role/launchpad-memory-admin-cn-northwest-1",
        caller_arn="arn:aws-cn:iam::111:role/launchpad-cn-ops-cn-northwest-1-t1",
    )
    assert reason.startswith("cannot assume the memory admin role arn:aws-cn:iam::111:role/")
    assert "the caller arn:aws-cn:iam::111:role/launchpad-cn-ops-cn-northwest-1-t1 lacks" in reason
    assert "`MemoryAdminAssume`" in reason and "launchpad-cn-ec2.yaml" in reason
    assert "this host's ops role lacks" in mb.assume_denied_reason("arn:x")


# ── N11-obs44b: the --with-cdk precondition ──────────────────────────────────

ADMIN_ARN = "arn:aws-cn:iam::111:role/launchpad-memory-admin-cn-northwest-1"
OPS_ROLE = "launchpad-cn-ops-cn-northwest-1-t1"
OPS_CALLER = f"arn:aws-cn:sts::111:assumed-role/{OPS_ROLE}/i-0abc"
MEMORY_ADMIN_STATEMENT = {
    "Sid": "MemoryAdminAssume", "Effect": "Allow", "Action": "sts:AssumeRole",
    "Resource": ["arn:aws-cn:iam::111:role/launchpad-memory-admin-*"],
}


class _Sts:
    def __init__(self, error_code: str | None = None):
        self.error_code = error_code
        self.calls: list[dict] = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "m"}}, "AssumeRole")
        return {"Credentials": {"AccessKeyId": "AKIA-test", "SecretAccessKey": "x",
                                "SessionToken": "t"}}


class _Iam:
    """Inline policies of one role; `error_code` makes every read fail."""

    def __init__(self, policies: dict[str, list[dict]] | None = None,
                 error_code: str | None = None):
        self.policies = policies or {}
        self.error_code = error_code
        self.roles_read: list[str] = []

    def _fail(self, op):
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code, "Message": "m"}}, op)

    def list_role_policies(self, RoleName, Marker=None):
        self._fail("ListRolePolicies")
        self.roles_read.append(RoleName)
        names = sorted(self.policies)
        if Marker is None and len(names) > 1:   # exercise the pagination branch
            return {"PolicyNames": names[:1], "IsTruncated": True, "Marker": "m1"}
        return {"PolicyNames": names if Marker is None else names[1:], "IsTruncated": False}

    def get_role_policy(self, RoleName, PolicyName):
        self._fail("GetRolePolicy")
        return {"RoleName": RoleName, "PolicyName": PolicyName,
                "PolicyDocument": {"Version": "2012-10-17",
                                   "Statement": self.policies[PolicyName]}}


def _check(sts, iam, *, domain_exists, caller=OPS_CALLER):
    return mb.check_memory_admin_assume(
        sts, iam, admin_role_arn=ADMIN_ARN, caller_arn=caller, domain_exists=domain_exists
    )


def test_precheck_assumes_the_admin_role_once_when_the_domain_stack_exists():
    sts, iam = _Sts(), _Iam()
    assert _check(sts, iam, domain_exists=True) == (mb.PRECHECK_OK, f"assumed {ADMIN_ARN}")
    assert len(sts.calls) == 1
    assert sts.calls[0] == {"RoleArn": ADMIN_ARN, "DurationSeconds": 900,
                            "RoleSessionName": "launchpad-memory-admin-precheck"}
    assert iam.roles_read == []   # the real assumption is the proof; no policy read


@pytest.mark.parametrize("code", ["AccessDenied", "AccessDeniedException"])
def test_precheck_denied_assumption_carries_the_memory_stage_reason(code):
    status, detail = _check(_Sts(code), _Iam(), domain_exists=True)
    assert status == mb.PRECHECK_DENIED
    assert detail == mb.assume_denied_reason(ADMIN_ARN, caller_arn=OPS_CALLER)
    assert "`MemoryAdminAssume`" in detail and OPS_CALLER in detail


def test_precheck_other_sts_errors_are_undecided_not_denials():
    status, detail = _check(_Sts("RegionDisabledException"), _Iam(), domain_exists=True)
    assert status == mb.PRECHECK_UNKNOWN
    assert "RegionDisabledException (not a denial)" in detail


def test_precheck_before_the_first_migration_reads_the_callers_inline_policies():
    """The admin role is created by the domain stack, so before the first migration it
    cannot be assumed: the ops role's inline policies are inspected for the statement
    (two policies → the ListRolePolicies pagination branch is exercised)."""
    sts = _Sts("AccessDenied")   # would be refused — and must not even be asked
    iam = _Iam({"ops-base": [{"Sid": "Ssm", "Effect": "Allow", "Action": "ssm:GetParameter",
                              "Resource": "*"}],
                "ops-memory": [MEMORY_ADMIN_STATEMENT]})
    status, detail = _check(sts, iam, domain_exists=False)
    assert status == mb.PRECHECK_OK
    assert detail.startswith(f"{OPS_ROLE} carries the `MemoryAdminAssume` grant")
    assert "launchpad-memory-domain not deployed yet" in detail
    assert sts.calls == [] and iam.roles_read == [OPS_ROLE, OPS_ROLE]


def test_precheck_before_the_first_migration_denies_a_pre_d17_ops_role():
    """The live N11-obs44 shape: t1's ops role from the pre-D17 template — cdk-* assume,
    IAM on launchpad-* roles, but no sts:AssumeRole on the memory admin role."""
    iam = _Iam({"ops": [
        {"Sid": "CdkAssume", "Effect": "Allow", "Action": "sts:AssumeRole",
         "Resource": "arn:aws-cn:iam::111:role/cdk-*"},
        {"Effect": "Allow", "Action": ["iam:GetRole", "iam:PutRolePolicy"],
         "Resource": "arn:aws-cn:iam::111:role/launchpad-*"},
        {"Effect": "Deny", "Action": "sts:AssumeRole",
         "Resource": "arn:aws-cn:iam::111:role/launchpad-memory-admin-*"},
    ]})
    status, detail = _check(_Sts(), iam, domain_exists=False)
    assert status == mb.PRECHECK_DENIED
    assert detail == mb.assume_denied_reason(ADMIN_ARN, caller_arn=OPS_CALLER)


@pytest.mark.parametrize("statement", [
    MEMORY_ADMIN_STATEMENT,
    {"Effect": "Allow", "Action": ["sts:AssumeRole"],
     "Resource": "arn:aws-cn:iam::111:role/launchpad-memory-admin-*"},   # no Sid
    {"Effect": "Allow", "Action": "sts:*", "Resource": "*"},               # broad admin
    {"Effect": "Allow", "Action": "sts:AssumeRole",
     "Resource": "arn:aws-cn:iam::111:role/launchpad-*"},                   # wider prefix
])
def test_statement_shapes_that_grant_the_assumption(statement):
    assert mb.statement_grants_memory_admin_assume(statement)


@pytest.mark.parametrize("statement", [
    {"Effect": "Allow", "Action": "sts:AssumeRole", "Resource": "arn:aws-cn:iam::111:role/cdk-*"},
    {"Effect": "Allow", "Action": "sts:GetCallerIdentity", "Resource": "*"},
    {"Effect": "Deny", "Action": "sts:AssumeRole", "Resource": "*"},
    {"Sid": "Other"},
])
def test_statement_shapes_that_do_not_grant_the_assumption(statement):
    assert not mb.statement_grants_memory_admin_assume(statement)


def test_precheck_is_undecided_for_a_user_caller_or_unreadable_policies():
    user = "arn:aws-cn:iam::111:user/operator"
    status, detail = _check(_Sts(), _Iam(), domain_exists=False, caller=user)
    assert status == mb.PRECHECK_UNKNOWN and "is not an IAM role" in detail and user in detail
    status, detail = _check(_Sts(), _Iam(error_code="AccessDenied"), domain_exists=False)
    assert status == mb.PRECHECK_UNKNOWN
    assert f"inline policies of {OPS_ROLE} could not be read (AccessDenied)" in detail


@pytest.mark.parametrize("arn, name", [
    (OPS_CALLER, OPS_ROLE),
    ("arn:aws-cn:iam::111:role/launchpad-cn-ops-cn-northwest-1", "launchpad-cn-ops-cn-northwest-1"),
    ("arn:aws-cn:iam::111:user/operator", None),
    ("arn:aws-cn:iam::111:root", None),
    ("", None),
])
def test_caller_role_name(arn, name):
    assert mb.caller_role_name(arn) == name


# ── r8-nits (reviewer nit A on N11-obs44a): a non-host caller gets a grant, not a stack ──


def test_assume_denied_reason_for_a_host_role_points_at_the_ec2_stack():
    reason = mb.assume_denied_reason(ADMIN_ARN, caller_arn=OPS_CALLER)
    assert f"the caller {OPS_CALLER} lacks the `MemoryAdminAssume` statement" in reason
    assert "EC2 stack updated to the current template" in reason
    assert "grant sts:AssumeRole" not in reason


def test_assume_denied_reason_for_a_non_host_caller_asks_for_a_grant():
    """An off-host operator (IAM user, root) has no EC2 stack whose template carries its
    policy — "update the EC2 stack" was a dead end; the reason names the grant instead."""
    user = "arn:aws-cn:iam::111:user/operator"
    role = ADMIN_ARN.rsplit("/", 1)[-1]
    reason = mb.assume_denied_reason(ADMIN_ARN, caller_arn=user)
    assert reason == (
        f"cannot assume the memory admin role {ADMIN_ARN} (AccessDenied on sts:AssumeRole) — "
        f"the caller {user} is not a host role: grant sts:AssumeRole on role/{role} to {user} "
        "(deploy.md §8.3)"
    )
    assert "EC2 stack" not in reason
    # root is not a role either
    assert "grant sts:AssumeRole" in mb.assume_denied_reason(
        ADMIN_ARN, caller_arn="arn:aws-cn:iam::111:root"
    )
    # no caller known → the host wording (the stage's historical default)
    assert "this host's ops role" in mb.assume_denied_reason(ADMIN_ARN)


def test_precheck_denied_for_a_user_caller_carries_the_grant_wording():
    user = "arn:aws-cn:iam::111:user/operator"
    status, detail = _check(_Sts(error_code="AccessDenied"), _Iam(), domain_exists=True,
                            caller=user)
    assert status == mb.PRECHECK_DENIED
    assert detail == mb.assume_denied_reason(ADMIN_ARN, caller_arn=user)
    assert "grant sts:AssumeRole" in detail and "EC2 stack" not in detail
