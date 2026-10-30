"""The fail-early guard (design §1.3 B): the pipeline's input-only refusals happen at
creation, and no new one can be added to a stage without being classified.

Two halves:

1. **The moved conditions as data** (``CASES``): every late-failure site of
   ``app/deployer/*.py`` whose condition is decidable from the request plus the
   workspace's resource map (design §0.3 — 6 pure-input sites + the gateway / artifacts
   bucket resource checks) is driven through ``POST /api/agents`` and must answer the
   declared creation-time code with **no agent row and no job written**. The redeploy
   route and the architect approval share the validator; one case each pins that.

2. **The AST reverse guard**: every ``raise HarnessRenderError(`` / ``raise RuntimeError(``
   in the deployer modules must be listed in ``tests/fail_early_registry.py`` as *moved*
   or *late*; an unlisted site fails the test naming ``file:line`` and its message, a
   stale registry row fails it too.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

import app.deployer as deployer_pkg
import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.models.ledger import Agent, Job, Workspace
from app.services import identity_providers
from tests.fail_early_registry import REGISTRY, Site

ACCOUNT = "111122223333"
REGION = "cn-northwest-1"
GW_ID = "launchpad-gw-a1b2c3d4e5"
READY = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "gateway_id": GW_ID,
    "gateway_url": f"https://{GW_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp",
    "memory_id": "mem-1",
    "execution_role_arn": f"arn:aws-cn:iam::{ACCOUNT}:role/launchpad-agent-execution-role",
    "codebuild_project": "launchpad-agent-builder",
}
GATEWAY_TOOL = {"type": "gateway", "name": "hr-tools",
                "config": {"record_id": "rec-1", "gateway_id": GW_ID, "tools": ["hr-tools___x"]}}
AUTH = {"connection": "conn-a", "kind": "oauth2", "mode": "as_agent"}


def _spec(method: str = "harness", **over: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {"name": "fe-guard", "method": method,
                            "system_prompt": "Answer concisely."}
    if method == "byoc":
        spec["byoc"] = {"artifact_kind": "code_zip", "upload_id": "up-1"}
    spec.update(over)
    return spec


def _image(account: str = ACCOUNT, region: str = REGION) -> str:
    return f"{account}.dkr.ecr.{region}.amazonaws.com.cn/launchpad-agents:v1"


# (id, spec, workspace resources (None = READY), expected status, expected code,
#  detail members that must match)
# CONTRACT for every code / detail below: docs/api.md "Creation-time validation (round 16)"
# and docs/cn/interfaces/testing.md §6.3; pydantic refusals are the FastAPI envelope the live
# console answers (designs/seams-20261019/console-api/agents_create_pydantic_422.json).
# The "no row / no job" invariant is the same paragraph's "before any agent row, job or AWS
# call". Nothing here is read off the implementation.
CASES: list[tuple[str, dict[str, Any], dict[str, Any] | None, int, str, dict[str, Any]]] = [
    # harness_local.py:272 — a builtin the method does not render
    ("builtin_unknown_on_harness",
     _spec(tools=[{"type": "builtin", "name": "video-renderer"}]), None,
     422, "agent.method_unsupported", {"method": "harness", "field": "tools.builtin",
                                       "value": "video-renderer"}),
    ("builtin_on_zip_runtime",
     _spec("zip_runtime", tools=[{"type": "builtin", "name": "code-interpreter"}]), None,
     422, "agent.method_unsupported", {"method": "zip_runtime", "field": "tools.builtin",
                                       "value": "code-interpreter", "supported": []}),
    # N16-obs80 (live, t1 aaacb0a6): an UNKNOWN builtin on zip_runtime answered 202 and the
    # deploy succeeded with the tool silently dropped — an empty declared set means none
    ("builtin_unknown_on_zip_runtime",
     _spec("zip_runtime", tools=[{"type": "builtin", "name": "no_such_builtin_seam"}]), None,
     422, "agent.method_unsupported", {"method": "zip_runtime", "field": "tools.builtin",
                                       "value": "no_such_builtin_seam", "supported": []}),
    ("builtin_on_studio",
     _spec("studio", tools=[{"type": "builtin", "name": "browser"}], code="x = 1\n"), None,
     422, "agent.method_unsupported", {"field": "tools.builtin", "supported": []}),
    # harness_local.py:186 — the Harness mounts unauthenticated remote MCP only
    ("harness_mcp_config_auth",
     _spec(tools=[{"type": "mcp", "name": "docs",
                   "config": {"url": "https://mcp.example.com/mcp", "auth": "oauth2"}}]), None,
     422, "agent.method_unsupported", {"field": "tools.auth", "value": "oauth2",
                                       "supported": []}),
    # zip_runtime / studio render no unauthenticated MCP client at all
    ("zip_runtime_unauthenticated_mcp",
     _spec("zip_runtime", tools=[{"type": "mcp", "name": "docs",
                                  "config": {"url": "https://mcp.example.com/mcp"}}]), None,
     422, "agent.method_unsupported", {"field": "tools.type", "value": "mcp"}),
    # harness_local.py:192 — mcp needs an http(s) URL
    ("mcp_url_not_http",
     _spec(tools=[{"type": "mcp", "name": "docs", "config": {"url": "ftp://x"}}]), None,
     422, "validation.invalid_request", {}),
    ("mcp_url_missing",
     _spec(tools=[{"type": "mcp", "name": "docs"}]), None,
     422, "validation.invalid_request", {}),
    # harness_local.py:172 — a Gateway tool needs the workspace Gateway
    ("gateway_tool_without_gateway_id_harness",
     _spec(tools=[GATEWAY_TOOL]), {**READY, "gateway_id": None, "gateway_url": None},
     409, "workspace.resource_missing", {"resource": "gateway_id"}),
    ("gateway_tool_without_gateway_id_zip_runtime",
     _spec("zip_runtime", tools=[GATEWAY_TOOL]), {**READY, "gateway_id": None},
     409, "workspace.resource_missing", {"resource": "gateway_id"}),
    # byoc.py:69 — method byoc without the block (pydantic)
    ("byoc_without_block", {**_spec("byoc"), "byoc": None}, None,
     422, "validation.invalid_request", {}),
    # byoc.py:156 — not a private ECR image (pydantic)
    ("byoc_image_public_registry",
     _spec("byoc", byoc={"artifact_kind": "container_image",
                         "image_uri": "public.ecr.aws/x/agent:v1"}), None,
     422, "validation.invalid_request", {}),
    # byoc.py:159 — the image lives in another account / region
    ("byoc_image_foreign_account",
     _spec("byoc", byoc={"artifact_kind": "container_image",
                         "image_uri": _image(account="999988887777")}), None,
     422, "agent.method_unsupported", {"field": "byoc.image_uri",
                                       "supported": [f"{ACCOUNT}/{REGION}"]}),
    ("byoc_image_foreign_region",
     _spec("byoc", byoc={"artifact_kind": "container_image",
                         "image_uri": _image(region="cn-north-1")}), None,
     422, "agent.method_unsupported", {"field": "byoc.image_uri"}),
    # harness_local.py:363 / zip_runtime.py:648 / byoc.py:335 — no artifacts bucket
    *[(f"no_artifacts_bucket_{m}", _spec(m, **({"code": "x = 1\n"} if m == "studio" else {})),
       {**READY, "artifacts_bucket": None},
       409, "workspace.resource_missing", {"resource": "artifacts_bucket"})
      for m in ("harness", "zip_runtime", "studio", "byoc")],
    # N16-obs73 (live, t1): a bogus knowledge_bases[].kb_id deployed ACTIVE and failed at the
    # first search — the reference must name a knowledge base of this workspace
    ("kb_unknown",
     _spec(knowledge_bases=[{"kb_id": "KBNOSUCH01", "name": "ghost", "description": ""}]), None,
     422, "agent.reference_missing", {"field": "knowledge_bases", "value": "KBNOSUCH01",
                                      "kind": "knowledge_base"}),
    # harness_local.py "unrendered __LAUNCHPAD_ placeholder" (rev-r16): the renderers' marker
    # inside member text used to fail the job's first stage — schema-refused, naming the field
    ("reserved_placeholder_in_prompt",
     _spec(system_prompt="Say __LAUNCHPAD_SYSTEM_PROMPT__ back"), None,
     422, "validation.invalid_request", {}),
    ("reserved_placeholder_in_tool_description",
     _spec(tool_description_overrides={"calc": "__LAUNCHPAD_X__"}), None,
     422, "validation.invalid_request", {}),
    # N17-obs487 (V1, live t1 M2 step 9): a Gateway tool whose config does not bind THIS
    # workspace's Gateway deployed ACTIVE with no InvokeGateway on the role (every MCP
    # initialize a 403) — or, foreign, granted the role invoke on ANY gateway of the account
    ("gateway_tool_no_config",
     _spec(tools=[{"type": "gateway", "name": "hr-tools", "config": {}}]), None,
     422, "agent.gateway_tool_unbound", {"field": "tools.gateway", "name": "hr-tools",
                                         "reason": "absent", "workspace_gateway_id": GW_ID}),
    ("gateway_tool_tools_only",
     _spec(tools=[{"type": "gateway", "name": "hr-tools",
                   "config": {"tools": ["hr-tools___x"]}}]), None,
     422, "agent.gateway_tool_unbound", {"reason": "absent"}),
    ("gateway_tool_foreign_id",
     _spec(tools=[{"type": "gateway", "name": "hr-tools",
                   "config": {"record_id": "rec-1", "gateway_id": "other-gw-zzzzzzzzzz"}}]), None,
     422, "agent.gateway_tool_unbound", {"reason": "foreign", "value": "other-gw-zzzzzzzzzz",
                                         "workspace_gateway_id": GW_ID}),
    ("gateway_tool_malformed_id",
     _spec(tools=[{"type": "gateway", "name": "hr-tools",
                   "config": {"record_id": "rec-1", "gateway_id": "gw/*"}}]), None,
     422, "agent.gateway_tool_unbound", {"reason": "malformed", "value": "gw/*"}),
    ("gateway_tool_record_unknown",
     _spec(tools=[{"type": "gateway", "name": "hr-tools", "config": {"record_id": "rec-nope"}}]),
     None, 422, "agent.reference_missing", {"field": "tools.gateway.record_id",
                                            "value": "rec-nope", "kind": "gateway_record"}),
    # N17-obs496 (V2): an a2a peer not bound to an APPROVED record / active A2A agent of this
    # workspace — the role would be granted InvokeAgentRuntime on an arbitrary runtime
    ("a2a_peer_record_unknown",
     _spec("zip_runtime", tools=[{"type": "a2a", "name": "peer",
                                  "config": {"record_id": "rec-nope", "runtime_arn":
                                             f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/peer-abc123"}}]),
     None, 422, "agent.reference_missing", {"field": "tools.a2a", "kind": "a2a_peer"}),
    # rev-r16 re-check: the three strings the Harness embeds outside the prompt / descriptions
    # (mcp_client/client.py: mcp URL + gateway tool patterns; main.py: skills URIs)
    ("reserved_placeholder_in_mcp_url",
     _spec(tools=[{"type": "mcp", "name": "docs",
                   "config": {"url": "https://mcp.example.com/__LAUNCHPAD_Z__"}}]), None,
     422, "validation.invalid_request", {}),
    ("reserved_placeholder_in_gateway_tool_pattern",
     _spec(tools=[{**GATEWAY_TOOL, "config": {**GATEWAY_TOOL["config"],
                                             "tools": ["hr-tools___x__LAUNCHPAD_Z__"]}}]), None,
     422, "validation.invalid_request", {}),
    ("reserved_placeholder_in_skill_uri",
     _spec(skills=["s3://launchpad-skills/__LAUNCHPAD_Z__/"]), None,
     422, "validation.invalid_request", {}),
    # image_build.py "artifacts_bucket/codebuild_project missing" (rev-r16): a container_source
    # upload is built by the workspace's CodeBuild project — the same conditional-resource
    # shape as gateway_id
    ("container_source_without_codebuild_project",
     _spec("byoc", byoc={"artifact_kind": "container_source", "upload_id": "up-1"}),
     {**READY, "codebuild_project": None},
     409, "workspace.resource_missing", {"resource": "codebuild_project"}),
    # an acting mode the method's identity channel does not implement
    ("obo_mode_on_zip_runtime",
     _spec("zip_runtime", tools=[{"type": "rest", "name": "weather",
                                  "config": {"url": "https://api.example.com"},
                                  "auth": {**AUTH, "mode": "obo"}}]), None,
     422, "agent.method_unsupported", {"field": "tools.auth.mode", "value": "obo",
                                       "supported": ["as_agent", "as_user"]}),
]


@pytest.fixture
def world(client, monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    monkeypatch.setattr(identity_providers, "validate_tool_auth", lambda control, spec: None)
    return client


def _set_resources(resources: dict[str, Any]) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        row.account_id, row.region = ACCOUNT, REGION
        row.resources = {k: v for k, v in resources.items() if v is not None}
        db.commit()
    finally:
        db.close()


def _row_counts() -> tuple[int, int]:
    db = SessionLocal()
    try:
        return db.query(Agent).count(), db.query(Job).count()
    finally:
        db.close()


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_moved_condition_is_refused_before_any_row(world, case):
    name, spec, resources, status, code, detail = case
    _set_resources(READY if resources is None else resources)
    res = world.post("/api/agents", json=spec)
    body = res.json()
    assert res.status_code == status, f"{name}: {res.status_code} {body}"
    assert set(body) == {"code", "message", "detail"}, f"{name}: envelope {body}"
    assert body.get("code") == code, f"{name}: {body}"
    got = body.get("detail") or {}
    for key, value in detail.items():
        assert got.get(key) == value, f"{name}: detail.{key} = {got.get(key)!r}, wanted {value!r}"
    assert _row_counts() == (0, 0), f"{name}: refused but rows were written"


def test_redeploy_runs_the_same_validator(world):
    """A re-publish with an edited spec is a creation for the pipeline: same refusal,
    the stored spec and status untouched."""
    _set_resources(READY)
    created = world.post("/api/agents", json=_spec()).json()
    agent_id = created["agent"]["id"]
    # the deploy stub leaves the row "deploying"; a re-publish needs it settled
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = "active"
        db.commit()
    finally:
        db.close()
    res = world.post(f"/api/agents/{agent_id}/redeploy",
                     json=_spec(tools=[{"type": "builtin", "name": "video-renderer"}]))
    assert res.status_code == 422 and res.json()["code"] == "agent.method_unsupported", res.text
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        assert row.status == "active" and not row.spec.get("tools")
        assert db.query(Job).count() == 1  # the create job only
    finally:
        db.close()


# ─── N17-obs487 / N17-obs496: bindings that MUST resolve (record-only Gateway ref, a2a peer) ──

PEER_ARN = f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/peer-abc123"


def _seed_gateway_record(record_id: str = "rec-gw", url: str | None = None) -> None:
    import json as _json

    from app.models.ledger import RegistryRecord

    db = SessionLocal()
    try:
        db.add(RegistryRecord(
            id=record_id, workspace_id="default", record_type="MCP", name="hr-tools",
            record_version="1", status="APPROVED",
            descriptors={"mcp": {"server": {"inlineContent": _json.dumps(
                {"remotes": [{"url": url if url is not None else READY["gateway_url"]}]})}}}))
        db.commit()
    finally:
        db.close()


def _seed_peer(record_id: str = "rec-peer", *, workspace_id: str = "default",
               record_status: str = "APPROVED", agent_status: str = "active",
               arn: str = PEER_ARN) -> None:
    from app.models.ledger import RegistryRecord

    db = SessionLocal()
    try:
        if workspace_id != "default" and db.get(Workspace, workspace_id) is None:
            # (account, region) is unique per workspace: the other workspace is another account
            db.add(Workspace(id=workspace_id, name=workspace_id, account_id="444455556666",
                             region=REGION, resources=dict(READY)))
        db.add(RegistryRecord(id=record_id, workspace_id=workspace_id, record_type="A2A",
                              name="peer", record_version="1", status=record_status,
                              descriptors={}))
        db.add(Agent(name=f"peer-{record_id}", method="zip_runtime", workspace_id=workspace_id,
                     status=agent_status, arn=arn, registry_record_id=record_id,
                     spec={"name": f"peer-{record_id}", "method": "zip_runtime",
                           "system_prompt": "x", "protocol": "a2a"}))
        db.commit()
    finally:
        db.close()


def _a2a_spec(record_id: str, arn: str = PEER_ARN) -> dict[str, Any]:
    return _spec("zip_runtime", tools=[{"type": "a2a", "name": "peer",
                                        "config": {"record_id": record_id, "runtime_arn": arn}}])


def test_gateway_record_only_ref_resolves_to_the_workspace_gateway(world):
    """V1 happy path: a record-only Gateway ref (what a client that mirrors the Registry may
    send) is resolved against the workspace's MCP record and the STORED spec is normalised
    with ``gateway_id`` — so ``agent_iam`` grants InvokeGateway on the Gateway the runtime
    calls."""
    _set_resources(READY)
    _seed_gateway_record("rec-gw")
    res = world.post("/api/agents", json=_spec(tools=[
        {"type": "gateway", "name": "hr-tools",
         "config": {"record_id": "rec-gw", "tools": ["hr-tools___x"]}}]))
    assert res.status_code == 202, res.text
    stored = res.json()["agent"]["spec"]["tools"][0]["config"]
    assert stored["gateway_id"] == GW_ID and stored["record_id"] == "rec-gw"
    from app.schemas.agent import AgentSpec
    from app.services import agent_iam
    assert agent_iam._gateway_ids(AgentSpec.model_validate(res.json()["agent"]["spec"])) == [GW_ID]


def test_gateway_record_of_another_gateway_is_refused(world):
    _set_resources(READY)
    other = f"https://other-gw-zzzzzzzzzz.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp"
    _seed_gateway_record("rec-other", url=other)
    res = world.post("/api/agents", json=_spec(tools=[
        {"type": "gateway", "name": "hr-tools", "config": {"record_id": "rec-other"}}]))
    assert res.status_code == 422 and res.json()["code"] == "agent.gateway_tool_unbound", res.text
    assert res.json()["detail"]["reason"] == "foreign"
    assert _row_counts() == (0, 0)


def test_architect_path_without_ledger_refuses_record_only_gateway_ref():
    """The architect approval validates without a db: a record-only ref cannot be resolved
    there, so it is refused instead of deploying without a grant."""
    from types import SimpleNamespace

    from app.core.errors import AppError
    from app.schemas.agent import AgentSpec
    from app.services import method_validation

    ws = SimpleNamespace(id="default", account_id=ACCOUNT, region=REGION, resources=dict(READY))
    spec = AgentSpec.model_validate(_spec(tools=[
        {"type": "gateway", "name": "hr-tools", "config": {"record_id": "rec-gw"}}]))
    with pytest.raises(AppError) as exc:
        method_validation.require_method_support(spec, ws)
    assert exc.value.code == "agent.gateway_tool_unbound"
    ok = AgentSpec.model_validate(_spec(tools=[GATEWAY_TOOL]))
    method_validation.require_method_support(ok, ws)  # bound → passes without a ledger


def test_gateway_id_equal_to_the_workspace_gateway_is_bound_whatever_its_shape(world):
    """The rule is equality with ``resources.gateway_id``, not the AWS id shape: stubs and
    test worlds store short ids (``gw-1``), bootstrap stores whatever AWS returned."""
    url = f"https://gw-1.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp"
    _set_resources({**READY, "gateway_id": "gw-1", "gateway_url": url})
    bound = {"type": "gateway", "name": "hr-tools",
             "config": {"record_id": "rec-1", "gateway_id": "gw-1"}}
    res = world.post("/api/agents", json=_spec(tools=[bound]))
    assert res.status_code == 202, res.text
    other = {**bound, "config": {"record_id": "rec-1", "gateway_id": "gw-2"}}
    res = world.post("/api/agents", json=_spec(name="fe-guard-2", tools=[other]))
    assert res.status_code == 422 and res.json()["detail"]["reason"] == "malformed", res.text


def test_a2a_peer_bound_to_an_active_server_of_this_workspace_deploys(world):
    _set_resources(READY)
    _seed_peer("rec-peer")
    res = world.post("/api/agents", json=_a2a_spec("rec-peer"))
    assert res.status_code == 202, res.text


@pytest.mark.parametrize("name, seed, arn", [
    ("foreign_account", {},
     f"arn:aws-cn:bedrock-agentcore:{REGION}:999988887777:runtime/peer-abc123"),
    ("foreign_partition", {},
     f"arn:aws:bedrock-agentcore:us-west-2:{ACCOUNT}:runtime/peer-abc123"),
    ("other_runtime_same_account", {},
     f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/not-the-peer"),
    ("other_workspace", {"workspace_id": "other"}, PEER_ARN),
    ("draft_record", {"record_status": "DRAFT"}, PEER_ARN),
    ("peer_not_active", {"agent_status": "deploying"}, PEER_ARN),
])
def test_a2a_peer_not_bound_to_this_workspace_is_refused(world, name, seed, arn):
    """V2: the IAM grant on the peer's Runtime ARN must never land outside this workspace /
    account, nor on a peer that is not an active, approved A2A server here."""
    _set_resources(READY)
    _seed_peer("rec-peer", **seed)
    res = world.post("/api/agents", json=_a2a_spec("rec-peer", arn))
    body = res.json()
    assert res.status_code == 422 and body["code"] == "agent.reference_missing", (name, body)
    assert body["detail"]["kind"] == "a2a_peer" and body["detail"]["field"] == "tools.a2a", name
    db = SessionLocal()
    try:  # the seeded peer is the only agent row; the refused consumer wrote nothing
        assert db.query(Agent).filter(Agent.name == "fe-guard").count() == 0, name
        assert db.query(Job).count() == 0, name
    finally:
        db.close()


# ─── the AST reverse guard ──────────────────────────────────────────────────

GUARDED_EXCEPTIONS = ("HarnessRenderError", "RuntimeError")
DEPLOYER_DIR = Path(deployer_pkg.__file__).parent


def _message_prefix(node: ast.AST | None) -> str:
    """The constant part of a raise message: a str literal, the literal parts of an
    f-string (placeholders dropped), the left-most literal of a ``+`` chain."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value for v in node.values if isinstance(v, ast.Constant))
    if isinstance(node, ast.BinOp):
        return _message_prefix(node.left)
    return "<dynamic>"


def raise_sites() -> list[tuple[str, int, str, str]]:
    """(file, line, exception, message prefix) of every guarded raise in the deployer."""
    out = []
    for path in sorted(DEPLOYER_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            exc = getattr(node, "exc", None)
            if not isinstance(node, ast.Raise) or not isinstance(exc, ast.Call):
                continue
            func = exc.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in GUARDED_EXCEPTIONS:
                arg = exc.args[0] if exc.args else None
                out.append((path.name, node.lineno, name, _message_prefix(arg)))
    return out


def _match(site: Site, file: str, message: str) -> bool:
    return site.file == file and message.startswith(site.message)


def test_every_deployer_raise_site_is_classified():
    """CONTRACT docs/cn/interfaces/testing.md §6.3 — differential over the source tree: the
    set of raise sites and the registry must agree in both directions."""
    sites = raise_sites()
    assert sites, "no guarded raise found — did the deployer move?"
    unlisted = [f"{f}:{line} raise {exc}({msg[:70]!r})"
                for f, line, exc, msg in sites
                if not any(_match(s, f, msg) for s in REGISTRY)]
    assert not unlisted, (
        "new late-failure site(s) in the deploy pipeline — classify each in "
        "tests/fail_early_registry.py as `moved` (and refuse the input at creation) or "
        "`late` (with the reason the request alone cannot decide it):\n  "
        + "\n  ".join(unlisted)
    )
    stale = [f"{s.file}: {s.message!r}" for s in REGISTRY
             if not any(_match(s, f, msg) for f, _l, _e, msg in sites)]
    assert not stale, ("registry rows without a raise site (edit or remove):\n  "
                       + "\n  ".join(stale))


def test_moved_sites_keep_their_last_line_of_defence():
    """Design §1.2: the renderer checks are NOT deleted when their condition moves to
    creation time — a stored spec that predates the validator still fails loudly."""
    moved = [s for s in REGISTRY if s.disposition == "moved"]
    assert len(moved) >= 11, [s.message for s in moved]
    sites = raise_sites()
    for site in moved:
        assert any(_match(site, f, msg) for f, _l, _e, msg in sites), site


def test_every_moved_code_has_a_case():
    """Each creation-time code a moved site names is exercised by ``CASES``."""
    codes = {s.code for s in REGISTRY if s.disposition == "moved"}
    exercised = {c[4] for c in CASES}
    assert codes <= exercised, codes - exercised
