"""Hub + workspace bootstrap of the self-hosted Memory/KB substrate (N6, rulings R2/R13).

Distribution extension: `memory_domain_mode=dedicated` selects an environment-owned
OpenSearch domain, admin role and discovery path. The D17 shared behavior described
below remains the default for existing configurations; see docs/cn/distribution.md.

Reads the per-environment ``launchpad-memory[-env]`` CDK stack outputs into the resource
keys the memory and knowledge-base backends (and the runtime library,
`docs/cn/interfaces/memory-runtime.md`) expect, and prepares this environment's slice of
the **shared** OpenSearch domain (D17 — one `launchpad-memory-domain` stack per
account+region, used by every EnvName environment): the three fixed indices
(`naming.memory_index()` / `jobs_index()` / `kb_docs_index()`, i.e. `launchpad-memory`,
`launchpad-jobs`, `launchpad-kb-docs` in the default environment and
`launchpad-<env>-…` otherwise — per-KB indices are the knowledge backend's own concern)
plus the per-environment FGAC security role `launchpad_memory_rw[_<env>]`, whose index
permissions are limited to `naming.index_patterns()`, and its role mapping (ruling R13).
Every OpenSearch write is idempotent — a PUT only happens when the current GET differs
from the desired body — so a second bootstrap pass reports everything `reused`.
"""

from collections.abc import Callable, Mapping
from typing import Any

from botocore.exceptions import ClientError

from app.core import naming
from app.core.naming import resource_name
from app.services import embedding_backend, opensearch_ensure
from app.services import opensearch_http as core

# Base name; `memory_stack_name()` applies `Settings.env_name` (N11): the per-environment
# stack (SQS, extractor Lambda, IAM; re-exports the shared domain's outputs).
MEMORY_STACK_NAME = "launchpad-memory"
# D17: the shared domain stack is deliberately NOT suffixed — one per account+region.
MEMORY_DOMAIN_STACK_NAME = "launchpad-memory-domain"
MEMORY_RESOURCE_KEYS = (
    "memory_id",
    "memory_bucket",
    "memory_prefix",
    "memory_os_endpoint",
    "memory_os_domain_arn",
    "memory_os_domain_name",
    "memory_queue_url",
    "memory_queue_arn",
    "memory_extractor_role_arn",
    "memory_admin_role_arn",
    "memory_index",
)


def memory_stack_name() -> str:
    return resource_name(MEMORY_STACK_NAME)


def memory_domain_stack_name() -> str:
    """Unsuffixed on purpose: every environment of the account+region shares it."""
    return naming.suffixed(MEMORY_DOMAIN_STACK_NAME, naming.memory_domain_env())


def security_role() -> str:
    return naming.security_role_name()


def _role_path() -> str:
    return f"/_plugins/_security/api/roles/{security_role()}"


def _mapping_path() -> str:
    return f"/_plugins/_security/api/rolesmapping/{security_role()}"


def memory_resources(outputs: Mapping[str, str], *, bucket: str, store_id: str) -> dict[str, Any]:
    """Every `MEMORY_RESOURCE_KEYS` entry derived from the per-environment memory stack's
    `CfnOutput`s (which re-export the shared domain's endpoint/ARN/admin role — D17), plus
    a cleared `memory_unavailable_reason` (ruling R2/R21)."""
    return {
        "memory_id": store_id,
        "memory_bucket": bucket,
        "memory_prefix": "memory",
        "memory_os_endpoint": outputs["MemoryDomainEndpoint"],
        "memory_os_domain_arn": outputs["MemoryDomainArn"],
        "memory_os_domain_name": outputs.get("MemoryDomainName", ""),
        "memory_queue_url": outputs["MemoryQueueUrl"],
        "memory_queue_arn": outputs["MemoryQueueArn"],
        "memory_extractor_role_arn": outputs.get("MemoryExtractorRoleArn", ""),
        "memory_admin_role_arn": outputs.get("MemoryAdminRoleArn", ""),
        "memory_index": naming.memory_index(),
        "memory_unavailable_reason": "",
    }


def unavailable(reason: str) -> dict[str, str]:
    return {"memory_unavailable_reason": reason}


# ── N11-obs44a: the memory admin role and the one policy statement that grants it ──
# The shared domain's FGAC master is the stack-owned role `launchpad-memory-admin-<region>`
# (infra/stacks/memory_domain_stack.py::admin_role_name — mirrored here, test-enforced);
# the host's ops role may assume it only through the `MemoryAdminAssume` statement of
# infra/ec2/launchpad-cn-ec2.yaml. A host created from a pre-D17 template lacks the
# statement, and STS answers a plain AccessDenied on sts:AssumeRole — which is not IAM
# propagation and not an OpenSearch failure, so it is named as what it is.
MEMORY_ADMIN_ASSUME_SID = "MemoryAdminAssume"
MEMORY_ADMIN_ROLE_PREFIX = "launchpad-memory-admin-"


def memory_admin_role_name(region: str) -> str:
    return naming.suffixed(f"{MEMORY_ADMIN_ROLE_PREFIX}{region}", naming.memory_domain_env())


def assume_denied_reason(role_arn: str, *, caller_arn: str = "") -> str:
    """Operator-facing text for an AccessDenied on sts:AssumeRole of the memory admin
    role: the role, who was refused, the policy statement that is missing and where it
    is added. Never carries the STS message body. A caller that is not an IAM role (an
    off-host operator, a user) has no EC2 stack to update — the fix there is a grant on
    the caller's own policy (r8-nits, reviewer nit A on N11-obs44a)."""
    if caller_arn and caller_role_name(caller_arn) is None:
        role = role_arn.rsplit("/", 1)[-1] if role_arn else memory_admin_role_name("<region>")
        return (
            f"cannot assume the memory admin role {role_arn or role} (AccessDenied on "
            f"sts:AssumeRole) — the caller {caller_arn} is not a host role: grant "
            f"sts:AssumeRole on role/{role} to {caller_arn} (deploy.md §8.3)"
        )
    who = f"the caller {caller_arn}" if caller_arn else "this host's ops role"
    return (
        f"cannot assume the memory admin role {role_arn or memory_admin_role_name('<region>')} "
        f"(AccessDenied on sts:AssumeRole) — {who} lacks the `{MEMORY_ADMIN_ASSUME_SID}` "
        f"statement (sts:AssumeRole on role/{MEMORY_ADMIN_ROLE_PREFIX}*, "
        "infra/ec2/launchpad-cn-ec2.yaml); a host created from a pre-D17 template needs its "
        "EC2 stack updated to the current template before `--with-cdk` (deploy.md §8.3)"
    )


# ── N11-obs44b: the --with-cdk precondition ─────────────────────────────────────
# `cdk deploy` of a D17 migration takes ~16 min and ends in a bootstrap memory stage that
# can only degrade when the ops role cannot assume the admin role (N11-obs44a). The check
# runs BEFORE the deploy (scripts/bootstrap.py, scripts/host-update.sh --with-cdk) and
# fails fast with the same reason. Two shapes, because the admin role is created by the
# domain stack itself: when the stack exists the role is assumed once for real; when it
# does not exist yet (the first migration in this account+region) the caller role's
# inline policies are inspected for the `MemoryAdminAssume` statement — the ops role has
# iam:ListRolePolicies/GetRolePolicy on role/launchpad-* (EC2 template). Anything the
# check cannot decide (a user, not a role; no IAM read permission; an STS error that is
# not a denial) is `unknown`: the callers warn and continue, never block on a guess.
PRECHECK_OK = "ok"
PRECHECK_DENIED = "denied"
PRECHECK_UNKNOWN = "unknown"
_STS_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException"})
_ASSUME_ROLE_ACTIONS = frozenset({"sts:AssumeRole", "sts:*", "*"})


def caller_role_name(caller_arn: str) -> str | None:
    """`…:role/<name>` or `…:assumed-role/<name>/<session>` → `<name>`; None otherwise
    (an IAM user, root, or an empty ARN)."""
    resource = caller_arn.rsplit(":", 1)[-1] if caller_arn else ""
    parts = resource.split("/")
    if len(parts) >= 2 and parts[0] in ("role", "assumed-role") and parts[1]:
        return parts[1]
    return None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def statement_grants_memory_admin_assume(statement: Mapping[str, Any]) -> bool:
    """True when one IAM statement is the `MemoryAdminAssume` grant: the Sid itself, or an
    Allow of sts:AssumeRole whose resource covers `role/launchpad-memory-admin-*`."""
    if statement.get("Sid") == MEMORY_ADMIN_ASSUME_SID:
        return True
    if statement.get("Effect") != "Allow":
        return False
    actions = {str(a) for a in _as_list(statement.get("Action"))}
    if not actions & _ASSUME_ROLE_ACTIONS:
        return False
    for resource in _as_list(statement.get("Resource")):
        text = str(resource)
        if text == "*" or text.endswith(f"role/{MEMORY_ADMIN_ROLE_PREFIX}*") or text.endswith(
            "role/launchpad-*"
        ):
            return True
    return False


def _inline_statements(iam: Any, role_name: str) -> list[Mapping[str, Any]]:
    names: list[str] = []
    kwargs: dict[str, Any] = {"RoleName": role_name}
    while True:
        page = iam.list_role_policies(**kwargs)
        names.extend(page.get("PolicyNames", []))
        if not page.get("IsTruncated") or not page.get("Marker"):
            break
        kwargs["Marker"] = page["Marker"]
    statements: list[Mapping[str, Any]] = []
    for name in names:
        document = iam.get_role_policy(RoleName=role_name, PolicyName=name).get(
            "PolicyDocument"
        ) or {}
        statements.extend(
            stmt for stmt in _as_list(document.get("Statement")) if isinstance(stmt, Mapping)
        )
    return statements


def check_memory_admin_assume(
    sts: Any,
    iam: Any,
    *,
    admin_role_arn: str,
    caller_arn: str,
    domain_exists: bool,
) -> tuple[str, str]:
    """``(status, detail)`` — `ok` / `denied` / `unknown` (see the block comment above).
    `denied` carries `assume_denied_reason(...)`, the text the bootstrap memory stage
    would otherwise print 16 minutes later."""
    if domain_exists:
        try:
            sts.assume_role(
                RoleArn=admin_role_arn,
                RoleSessionName="launchpad-memory-admin-precheck",
                DurationSeconds=900,
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "Unknown")
            if code in _STS_DENIED_CODES:
                return PRECHECK_DENIED, assume_denied_reason(admin_role_arn, caller_arn=caller_arn)
            return PRECHECK_UNKNOWN, (
                f"sts:AssumeRole on {admin_role_arn} failed with {code} (not a denial) — "
                "the precondition cannot be decided"
            )
        return PRECHECK_OK, f"assumed {admin_role_arn}"
    role_name = caller_role_name(caller_arn)
    if role_name is None:
        return PRECHECK_UNKNOWN, (
            f"{memory_domain_stack_name()} is not deployed yet and the caller "
            f"{caller_arn or '<unknown>'} is not an IAM role — its policies are not inspected; "
            f"an off-host operator needs sts:AssumeRole on role/{MEMORY_ADMIN_ROLE_PREFIX}*"
        )
    try:
        statements = _inline_statements(iam, role_name)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code") or "Unknown")
        return PRECHECK_UNKNOWN, (
            f"{memory_domain_stack_name()} is not deployed yet and the inline policies of "
            f"{role_name} could not be read ({code}) — the precondition cannot be decided"
        )
    if any(statement_grants_memory_admin_assume(stmt) for stmt in statements):
        return PRECHECK_OK, (
            f"{role_name} carries the `{MEMORY_ADMIN_ASSUME_SID}` grant "
            f"({memory_domain_stack_name()} not deployed yet — checked by policy, not by assuming)"
        )
    return PRECHECK_DENIED, assume_denied_reason(admin_role_arn, caller_arn=caller_arn)


# Which shared memory a workspace resource map carries, if any (N-ARCH-bug1). The
# architect assistant, the proposal contract and the console all ask this one
# predicate instead of testing the Global build's `memory_arn` — which the CN
# self-hosted substrate never writes.
MEMORY_KIND_SELFHOSTED = "selfhosted"
MEMORY_KIND_AGENTCORE = "agentcore"


def shared_memory_kind(resources: Mapping[str, Any] | None) -> str | None:
    """``"selfhosted"`` when the CN substrate is configured — the same two keys the
    deployer (`deployer/environment.py`) and `memory_selfhosted.has_substrate` bind on
    (`memory_os_endpoint` + `memory_id`; `memory_index` always rides along but is not
    what gates a deploy) and no `memory_unavailable_reason` left by a failed bootstrap;
    ``"agentcore"`` when the Global build's `memory_arn` is present; else ``None``.

    Accepts either the workspace's raw resource map or the architect catalog's compact
    ``resources`` snapshot: the latter carries the kind already resolved under
    ``memory_kind`` (it has no substrate keys), so an explicit value wins."""
    r = resources or {}
    explicit = r.get("memory_kind")
    if explicit in (MEMORY_KIND_SELFHOSTED, MEMORY_KIND_AGENTCORE):
        return explicit
    if (
        r.get("memory_os_endpoint")
        and r.get("memory_id")
        and not r.get("memory_unavailable_reason")
    ):
        return MEMORY_KIND_SELFHOSTED
    if r.get("memory_arn"):
        return MEMORY_KIND_AGENTCORE
    return None


def workspace_has_shared_memory(resources: Mapping[str, Any] | None) -> bool:
    """True when a proposal / spec may bind ``memory: workspace`` here."""
    return shared_memory_kind(resources) is not None


def security_role_body() -> dict[str, Any]:
    """FGAC security role granted this environment's principals (ruling R13): full
    access to this environment's indices only (`naming.index_patterns()` — D17: the
    domain is shared, so `launchpad-*` would expose every other environment's data),
    plus cluster monitoring/snapshot ops."""
    return {
        "cluster_permissions": ["cluster_composite_ops", "cluster_monitor"],
        "index_permissions": [
            {"index_patterns": naming.index_patterns(), "allowed_actions": ["indices_all"]}
        ],
        "tenant_permissions": [],
    }


def role_mapping_body(
    exec_role_arn: str, extractor_role_arn: str, agent_role_pattern: str
) -> dict[str, list[str]]:
    return {"users": [exec_role_arn, extractor_role_arn, agent_role_pattern]}


def _security_get(os_http: Any, path: str) -> dict[str, Any] | None:
    try:
        return os_http.request("GET", path).get(security_role())
    except core.OpenSearchError as exc:
        if exc.status == 404:
            return None
        raise


def _is_covered(desired: dict[str, Any], live: dict[str, Any]) -> bool:
    """True when every entry `desired` wants is already present in `live`.

    Used for the role mapping only: `map_agent_role` (R13a fallback) appends agent
    roles one at a time outside `ensure_memory_substrate`, so the live mapping is
    allowed to carry *more* than the three base entries without counting as a
    mismatch — the fixed `security_role_body` still needs exact equality, since a
    permissions body silently drifting wider would be a privilege escalation.
    """
    for key, value in desired.items():
        live_value = live.get(key)
        if isinstance(value, list):
            if not isinstance(live_value, list) or not set(value) <= set(live_value):
                return False
        elif live_value != value:
            return False
    return True


def _normalized_permission_entries(
    entries: list[dict[str, Any]], pattern_key: str
) -> frozenset[tuple[frozenset[str], frozenset[str]]]:
    """Reduce a security-role permissions list (`index_permissions`/
    `tenant_permissions`) to its two meaningful fields, order-independent and
    hashable — the live GET response adds server-normalized keys (`dls`, `fls`,
    `masked_fields`) to each entry that are not a real permissions change."""
    return frozenset(
        (frozenset(entry.get(pattern_key) or []), frozenset(entry.get("allowed_actions") or []))
        for entry in entries
    )


def _security_role_matches(desired: dict[str, Any], live: dict[str, Any]) -> bool:
    """Normalized equality for the FGAC security role (the `superset=False` branch
    of `_ensure_security`). A live `GET _plugins/_security/api/roles/<role>` is not
    byte-identical to the PUT body even when nothing changed: OpenSearch adds
    `reserved`/`hidden`/`static` at the top level and `dls`/`fls`/`masked_fields` to
    every `index_permissions` entry, and may reorder lists. None of that is a
    permissions change, so: only `desired`'s own keys are ever compared (extra
    top-level keys on `live` are ignored outright); `cluster_permissions` compares
    as a set; `index_permissions` entries compare by `index_patterns` +
    `allowed_actions` only, order-independent. A real drift — a missing/extra
    action or pattern — still fails the comparison and triggers a PUT.
    """
    for key, value in desired.items():
        live_value = live.get(key)
        if key == "cluster_permissions":
            if set(value or []) != set(live_value or []):
                return False
        elif key == "index_permissions":
            if _normalized_permission_entries(
                value or [], "index_patterns"
            ) != _normalized_permission_entries(live_value or [], "index_patterns"):
                return False
        elif live_value != value:
            return False
    return True


def _unioned(desired: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """`live` extended with whatever `desired` list entries it is missing.

    The role-mapping PUT body is always this union, never `desired` alone, so a
    re-run can never *drop* an agent role `map_agent_role` added since the last
    `ensure_memory_substrate` pass.
    """
    merged = dict(live)
    for key, value in desired.items():
        if isinstance(value, list):
            current = list(merged.get(key) or [])
            merged[key] = current + [item for item in value if item not in current]
        else:
            merged[key] = value
    return merged


def _ensure_security(
    os_http: Any,
    label: str,
    path: str,
    body: dict[str, Any],
    created: list[str],
    reused: list[str],
    *,
    superset: bool = False,
) -> None:
    """PUT-if-differs, idempotent either way: `superset=False` (the FGAC role)
    requires normalized equality — ignoring the server-added keys a live GET
    echoes back (see `_security_role_matches`) — `superset=True` (the role
    mapping) only requires `live` to already cover every entry `body` wants, and
    PUTs the union of the two rather than `body` alone when it does not (see
    `_is_covered`/`_unioned`)."""
    live = _security_get(os_http, path)
    if superset:
        if live and _is_covered(body, live):
            reused.append(f"{label} {security_role()}")
            return
        os_http.request("PUT", path, _unioned(body, live or {}))
        created.append(f"{label} {security_role()}")
        return
    if live and _security_role_matches(body, live):
        reused.append(f"{label} {security_role()}")
        return
    os_http.request("PUT", path, body)
    created.append(f"{label} {security_role()}")


def fixed_indices() -> tuple[tuple[str, dict[str, Any]], ...]:
    """This environment's three fixed indices with their bodies (per-KB indices are the
    knowledge backend's own concern)."""
    return (
        # D21: the memory index's k-NN dimension follows the registry's active embedding row
        (naming.memory_index(), embedding_backend.vector_index_body(core.memory_index_body())),
        (naming.jobs_index(), core.jobs_index_body()),
        (naming.kb_docs_index(), core.kb_docs_index_body()),
    )


def ensure_memory_substrate(
    os_http: Any,
    *,
    exec_role_arn: str,
    extractor_role_arn: str,
    agent_role_pattern: str,
    log: Callable[[str], None] = lambda _m: None,
) -> dict[str, list[str]]:
    """Idempotent: the three fixed indices, the FGAC security role and its role
    mapping (ruling R13). A PUT only happens when the GET differs from the desired
    body, so a second pass over an already-provisioned domain creates nothing."""
    created: list[str] = []
    reused: list[str] = []
    for name, body in fixed_indices():
        # WB-007: race-safe — a concurrent creator's index counts as reused when its live
        # mapping is compatible; an incompatible one fails the stage with the conflicts.
        made = opensearch_ensure.ensure_index(os_http, name, body)
        (created if made else reused).append(f"index {name}")
        if not made and name == naming.memory_index():
            # r14-a (N14-obs1): a pre-v2 memory index gains the additive record fields
            # (superseded_by, updated_at, confidence, …) in place — idempotent, no reindex.
            added = opensearch_ensure.ensure_mapping(
                os_http, name, core.MEMORY_RECORD_V2_PROPERTIES
            )
            (created if added else reused).append(
                f"mapping {name} v2 fields" + (f" +{','.join(added)}" if added else "")
            )
    _ensure_security(os_http, "security role", _role_path(), security_role_body(), created, reused)
    _ensure_security(
        os_http,
        "role mapping",
        _mapping_path(),
        role_mapping_body(exec_role_arn, extractor_role_arn, agent_role_pattern),
        created,
        reused,
        superset=True,
    )
    for line in created:
        log(f"{line} created")
    return {"created": created, "reused": reused}


PROBE_HEALTHY_STATUSES = ("green", "yellow")


def probe_memory_substrate(os_http: Any) -> tuple[bool, str]:
    """Cheap post-failure health probe (N-SCN-obs6): ``(healthy, detail)``.

    Healthy ⇔ `_cluster/health` answers ``green`` or ``yellow`` AND every fixed index
    exists AND the FGAC security role GET answers with the role present AND the FGAC
    role mapping GET answers with the mapping present — i.e. everything
    `ensure_memory_substrate` provisions. The bootstrap memory stage runs it only after
    `ensure_memory_substrate` raised, to tell a transient failure of one idempotent step
    on an already-provisioned domain (clear `memory_unavailable_reason`) from a
    substrate that is really unusable (record the reason). Never raises: any error is
    reported as unhealthy with the exception type only (no response body)."""
    try:
        status = str(os_http.request("GET", "/_cluster/health").get("status") or "").lower()
        if status not in PROBE_HEALTHY_STATUSES:
            return False, f"cluster status {status or 'unknown'}"
        missing = [name for name, _body in fixed_indices() if not os_http.index_exists(name)]
        if missing:
            return False, f"cluster {status}; missing indices: {', '.join(missing)}"
        if _security_get(os_http, _role_path()) is None:
            return False, f"cluster {status}; security role {security_role()} absent"
        if _security_get(os_http, _mapping_path()) is None:
            return False, f"cluster {status}; role mapping {security_role()} absent"
    except Exception as exc:  # noqa: BLE001 — a probe reports, it never raises
        return False, f"probe failed ({type(exc).__name__})"
    return (
        True,
        f"cluster {status}; {len(fixed_indices())} indices present; "
        f"security role and role mapping {security_role()} present",
    )


def map_agent_role(os_http: Any, role_arn: str) -> None:
    """R13a fallback, in case the OpenSearch security plugin rejects a wildcard
    inside `rolesmapping.users`: append one agent role's exact ARN, once."""
    live = _security_get(os_http, _mapping_path()) or {"users": []}
    users = list(live.get("users") or [])
    if role_arn in users:
        return
    os_http.request("PUT", _mapping_path(), {"users": users + [role_arn]})
