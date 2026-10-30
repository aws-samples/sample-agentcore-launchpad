"""The creation matrix — the permission matrix's sibling for the agent creation form.

Design: `plans/CAPABILITY-MATRIX-DESIGN.md` (N15-obs16 → round 16 channel A). One question
answered mechanically: **can every combination the console / API lets a member select
either deploy, or is it refused at creation time?** The third outcome — the API accepts
the spec (202, agent row + job written) and the method's own generate stage then fails
on that very input — is the N15-obs16 class of bug (office-agent picked ``browser`` on a
Harness; the job died at ``generate`` with ``HarnessRenderError``). Every cell here fails
the test on that outcome, naming the cell.

Dimensions (``DIMENSIONS``): method × builtin × tool_type × protocol × memory × kb × native
× byoc_kind. Coverage: every pair of values of every two dimensions appears in at least one
cell (``pairwise`` — a small greedy generator, no dependency) plus every single-factor cell
(each value alone on each method's baseline). Hermetic: deploy start is stubbed, the
Harness renders the vendored fixture template, no AWS client is built.

Per cell, exactly one of:

* ``POST /api/agents`` → 202 **and** the method's generate stage dry-runs clean
  (``harness``: ``render_harness_bundle``; ``zip_runtime`` / ``studio``:
  ``_generate_code`` + ``_method_requirements``; ``byoc``: ``_config`` + the static half
  of ``describe_image``) — the spec is deployable;
* ``POST /api/agents`` → 4xx with no agent row and no job — the spec is refused at the
  boundary. When the code is ``agent.method_unsupported`` the ``detail.supported`` set
  must equal the method's declaration (``pipeline.method_capabilities``).

A cell that lands in neither bucket (202 + dry-run error, or any 5xx) is a product bug;
``KNOWN_RED`` lists the ones found and tracked (strict xfail — a fix must remove its row).

Oracle: DIFFERENTIAL, never an expected value copied from the implementation — the request
boundary (``POST /api/agents``) and the method's own generate stage are two independent
paths that must agree on every cell; the only contract facts asserted are the two error
codes and their ``detail`` shape (CONTRACT: docs/api.md "Creation-time validation",
docs/cn/interfaces/testing.md §6.2) and the error envelope ``{code, message, detail}`` (the
live console envelope, designs/seams-20261019/console-api/agents_create_pydantic_422.json).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from itertools import combinations, product
from pathlib import Path
from typing import Any

import pytest

import app.routers.agents as agents_router
from app.core.db import SessionLocal
from app.deployer import byoc, harness_local, zip_runtime
from app.deployer.pipeline import method_capabilities
from app.models.ledger import Agent, Job, KnowledgeBase, Workspace
from app.schemas.agent import AgentSpec
from app.services import identity_providers
from app.services.workspace import context_for_workspace

FIXTURE_TEMPLATE = Path(__file__).parent / "fixtures" / "harness_runtime_min"
ACCOUNT = "111122223333"
REGION = "cn-northwest-1"
GW_ID = "launchpad-gw-a1b2c3d4e5"
RESOURCES = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "gateway_id": GW_ID,
    "gateway_url": f"https://{GW_ID}.gateway.bedrock-agentcore.{REGION}.amazonaws.com.cn/mcp",
    "memory_id": "mem-1",
    "execution_role_arn": f"arn:aws-cn:iam::{ACCOUNT}:role/launchpad-agent-execution-role",
    "codebuild_project": "launchpad-agent-builder",
}
IMAGE_URI = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com.cn/launchpad-agents:v1"
PEER_ARN = f"arn:aws-cn:bedrock-agentcore:{REGION}:{ACCOUNT}:runtime/peer-abc123"
CONNECTION = {"connection": "conn-a", "kind": "oauth2", "mode": "as_agent"}
KB_ID = "KBDOCS0001"  # the one knowledge base the world seeds (an ACTIVE ledger row)

# ─── dimensions ──────────────────────────────────────────────────────────────

DIMENSIONS: dict[str, tuple[str, ...]] = {
    "method": ("harness", "zip_runtime", "studio", "byoc"),
    # "video-renderer" is a builtin NO method renders (N16-obs80: an unknown builtin on
    # zip_runtime was accepted live and the deploy succeeded with the tool silently dropped)
    "builtin": ("none", "code-interpreter", "browser", "video-renderer"),
    "tool_type": ("none", "gateway", "mcp", "mcp_auth", "rest", "a2a"),
    "protocol": ("http", "a2a"),
    "memory": ("on", "off"),
    # "missing" names a KB no ledger row has (N16-obs73: a bogus kb_id deployed ACTIVE live)
    "kb": ("none", "one", "missing"),
    "native": ("none", "shell"),
    "byoc_kind": ("-", "code_zip", "container_source", "container_image"),
}
BASELINE = {"builtin": "none", "tool_type": "none", "protocol": "http", "memory": "on",
            "kb": "none", "native": "none"}
# The workspace the spec is created in — cross-cutting, so it is not a pairwise dimension:
# every spec cell runs on a bootstrapped workspace (``ready``); the two degraded worlds run
# on each method's baseline (``no_bucket``) and on the gateway cells (``no_gateway``).
WORLDS: dict[str, dict[str, Any]] = {
    "ready": {},
    "no_bucket": {"artifacts_bucket": None},
    "no_gateway": {"gateway_id": None, "gateway_url": None},
}
STUDIO_CODE = ("from strands import Agent\n\nagent = Agent()\n\n\n"
               "def run(prompt):\n    return str(agent(prompt))\n")

Cell = dict[str, str]


def valid(cell: Cell) -> bool:
    """byoc_kind exists only for the byoc method (``-`` = not applicable)."""
    return (cell["method"] == "byoc") == (cell["byoc_kind"] != "-")


def cell_id(cell: Cell) -> str:
    world = cell.get("world", "ready")
    return (f"{cell['method']}|b={cell['builtin']}|t={cell['tool_type']}|p={cell['protocol']}"
            f"|m={cell['memory']}|kb={cell['kb']}|n={cell['native']}|byoc={cell['byoc_kind']}"
            + (f"|ws={world}" if world != "ready" else ""))


def pairwise(
    dimensions: dict[str, tuple[str, ...]], is_valid: Callable[[Cell], bool],
) -> list[Cell]:
    """Greedy pairwise (2-way) covering array over the valid rows of the full product.

    Every pair of values of every two dimensions that occurs in some valid row is covered
    by at least one chosen row; rows are chosen by most newly covered pairs, ties by
    product order, so the result is deterministic.
    """
    names = list(dimensions)
    rows = [dict(zip(names, values, strict=True)) for values in product(*dimensions.values())]
    rows = [r for r in rows if is_valid(r)]
    pairs_of = [frozenset(combinations(sorted(r.items()), 2)) for r in rows]
    uncovered: set[tuple[tuple[str, str], tuple[str, str]]] = set().union(*pairs_of)
    chosen: list[Cell] = []
    while uncovered:
        best = max(range(len(rows)), key=lambda i: (len(pairs_of[i] & uncovered), -i))
        if not pairs_of[best] & uncovered:  # pragma: no cover — defensive
            break
        chosen.append(rows[best])
        uncovered -= pairs_of[best]
    return chosen


def single_factor() -> list[Cell]:
    """Each value of each dimension alone on each method's baseline."""
    cells: list[Cell] = []
    for method in DIMENSIONS["method"]:
        base = {"method": method, **BASELINE,
                "byoc_kind": "code_zip" if method == "byoc" else "-"}
        cells.append(base)
        for dim, values in DIMENSIONS.items():
            if dim == "method":
                continue
            for value in values:
                cell = {**base, dim: value}
                if cell != base and valid(cell):
                    cells.append(cell)
    return cells


def degraded_worlds() -> list[Cell]:
    """Each method's baseline on a workspace without an artifacts bucket; the gateway
    cell of each gateway-rendering method on a workspace without a Gateway."""
    cells: list[Cell] = []
    for method in DIMENSIONS["method"]:
        base = {"method": method, **BASELINE,
                "byoc_kind": "code_zip" if method == "byoc" else "-"}
        cells.append({**base, "world": "no_bucket"})
        if method != "byoc":
            cells.append({**base, "tool_type": "gateway", "world": "no_gateway"})
    return cells


def all_cells() -> list[Cell]:
    seen: dict[str, Cell] = {}
    for cell in pairwise(DIMENSIONS, valid) + single_factor() + degraded_worlds():
        seen.setdefault(cell_id(cell), cell)
    return [seen[k] for k in sorted(seen)]


CELLS = all_cells()

# Cells the matrix found red on the current code — product bugs tracked in
# docs/cn/reports/bugs-round-16.md. Strict: a fixed cell must be removed here.
KNOWN_RED: dict[str, str] = {
    # N16-obs1..5 (13 cells on the unmodified code) all turned green once
    # schemas.agent.validate_against_method ran at creation — rows removed (strict xfail)
}

# ─── spec construction ───────────────────────────────────────────────────────


def spec_for(cell: Cell, index: int) -> dict[str, Any]:
    method = cell["method"]
    spec: dict[str, Any] = {"name": f"mx-{index}", "method": method,
                            "system_prompt": "Answer concisely."}
    tools: list[dict[str, Any]] = []
    if cell["builtin"] != "none":
        tools.append({"type": "builtin", "name": cell["builtin"]})
    tool = cell["tool_type"]
    if tool == "gateway":
        tools.append({"type": "gateway", "name": "hr-tools",
                      "config": {"record_id": "rec-1", "gateway_id": GW_ID,
                                 "tools": ["hr-tools___lookup"]}})
    elif tool in ("mcp", "mcp_auth"):
        ref: dict[str, Any] = {"type": "mcp", "name": "docs",
                               "config": {"url": "https://mcp.example.com/mcp"}}
        if tool == "mcp_auth" or method == "byoc":  # byoc tools are auth declarations only
            ref["auth"] = dict(CONNECTION)
        tools.append(ref)
    elif tool == "rest":
        ref = {"type": "rest", "name": "weather", "config": {"url": "https://api.example.com"}}
        if method == "byoc":
            ref["auth"] = dict(CONNECTION)
        tools.append(ref)
    elif tool == "a2a":
        tools.append({"type": "a2a", "name": "peer",
                      "config": {"record_id": "rec-2", "runtime_arn": PEER_ARN}})
    if tools:
        spec["tools"] = tools
    if cell["protocol"] != "http":
        spec["protocol"] = cell["protocol"]
        spec["a2a_skills"] = [{"id": "answer", "name": "Answer", "description": "d"}]
    on = cell["memory"] == "on"
    spec["memory"] = {"short_term": on, "long_term": on}
    if cell["kb"] != "none":
        kb_id = KB_ID if cell["kb"] == "one" else "KBMISSING1"
        spec["knowledge_bases"] = [{"kb_id": kb_id, "name": "Docs", "description": "d"}]
    if cell["native"] != "none":
        spec["native_tools"] = [cell["native"]]
    if method == "studio":
        spec["code"] = STUDIO_CODE
    if method == "byoc":
        kind = cell["byoc_kind"]
        spec["byoc"] = ({"artifact_kind": kind, "image_uri": IMAGE_URI} if kind == "container_image"
                        else {"artifact_kind": kind, "upload_id": "up-1"})
    return spec


# ─── dry-run of the method's generate stage (no AWS) ─────────────────────────


class _FakeEcr:
    def describe_images(self, **kw):
        return {"imageDetails": [{"imageDigest": "sha256:" + "0" * 64, "imagePushedAt": "now"}]}


def dry_run(spec: AgentSpec) -> None:
    """The method's generate stage on ``spec`` plus the package stage's static
    precondition (an artifacts bucket), without a single AWS call. A rendered artifact
    must also carry EVIDENCE of every tool the spec mounts (``_require_rendered``): a
    tool the generator silently drops is as much a creation-time lie as a render error."""
    ws = context_for_workspace("default")
    rendered = ""
    if spec.method == "harness":
        bundle = harness_local.render_harness_bundle(spec, ws)
        rendered = "\n".join(bundle.files.values())
    elif spec.method in ("zip_runtime", "studio"):
        rendered, _ = zip_runtime._generate_code(spec)
        zip_runtime._method_requirements(spec)
    elif spec.method == "byoc":
        cfg = byoc._config(spec)
        if cfg.artifact_kind == "container_image":
            byoc.describe_image(ws, cfg.image_uri or "", ecr_client=_FakeEcr())
            return  # the package stage is skipped for an existing image
    else:  # pragma: no cover
        raise AssertionError(f"no dry-run for method {spec.method!r}")
    if not ws.resources.get("artifacts_bucket"):
        raise RuntimeError("package stage: artifacts_bucket missing from the workspace")
    if spec.method != "byoc":  # byoc tools are outbound-auth declarations for the member's code
        _require_rendered(spec, rendered)


def _require_rendered(spec: AgentSpec, rendered: str) -> None:
    missing = []
    for tool in spec.tools:
        if tool.type == "mcp" and tool.auth is None:
            needle = str((tool.config or {}).get("url") or tool.name)
        elif tool.type == "a2a":
            needle = f"ask_{tool.name}"
        elif tool.type == "builtin":
            needle = tool.name.replace("-", "_")
        else:
            continue  # gateway / rest / authenticated mcp reach the artifact through env + IAM
        if needle not in rendered:
            missing.append(f"{tool.type}:{tool.name}")
    if missing:
        raise RuntimeError(
            f"generate stage silently dropped tool(s) {', '.join(missing)} — the rendered "
            "artifact never mentions them"
        )


# ─── the world ───────────────────────────────────────────────────────────────


@pytest.fixture
def world(client, monkeypatch, cell):
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", FIXTURE_TEMPLATE)
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    # tool auth names a live Connection in AWS — out of scope here (the matrix is about
    # the method's rendering ability, not the vault)
    monkeypatch.setattr(identity_providers, "validate_tool_auth", lambda control, spec: None)
    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        resources = {**RESOURCES, **WORLDS[cell.get("world", "ready")]}
        row.account_id, row.region = ACCOUNT, REGION
        row.resources = {k: v for k, v in resources.items() if v is not None}
        db.add(KnowledgeBase(kb_id=KB_ID, workspace_id="default", name="docs", status="ACTIVE"))
        # N17-obs496: the a2a cells name a peer that must be an APPROVED A2A record of this
        # workspace owned by an ACTIVE protocol=a2a agent whose Runtime ARN is PEER_ARN
        from app.models.ledger import RegistryRecord
        if db.get(RegistryRecord, "rec-2") is None:
            db.add(RegistryRecord(id="rec-2", workspace_id="default", record_type="A2A",
                                  name="peer", record_version="1", status="APPROVED",
                                  descriptors={}))
            db.add(Agent(name="mx-peer-server", method="zip_runtime", workspace_id="default",
                         status="active", arn=PEER_ARN, registry_record_id="rec-2",
                         spec={"name": "mx-peer-server", "method": "zip_runtime",
                               "system_prompt": "x", "protocol": "a2a"}))
        db.commit()
    finally:
        db.close()
    return client


def _rows(name: str) -> tuple[list[Agent], list[Job]]:
    db = SessionLocal()
    try:
        agents = db.query(Agent).filter(Agent.name == name).all()
        jobs = db.query(Job).all()
        return agents, jobs
    finally:
        db.close()


@dataclass
class Outcome:
    status: int
    code: str | None
    detail: Any
    dry_run_error: str | None
    body: dict[str, Any] = field(default_factory=dict)


def run_cell(client, cell: Cell, index: int) -> Outcome:
    spec = spec_for(cell, index)
    res = client.post("/api/agents", json=spec)
    body = res.json() if res.content else {}
    code = body.get("code") if isinstance(body, dict) else None
    if res.status_code == 202:
        agents, _ = _rows(spec["name"])
        assert len(agents) == 1, f"{cell_id(cell)}: 202 but {len(agents)} agent rows"
        try:
            dry_run(AgentSpec(**agents[0].spec))
        except Exception as exc:  # noqa: BLE001 — the dry-run's failure IS the finding
            return Outcome(res.status_code, None, None, f"{type(exc).__name__}: {exc}")
        return Outcome(res.status_code, None, None, None)
    return Outcome(res.status_code, code, body.get("detail"), None, body)


def _ids(cells: Iterable[Cell]) -> list[Any]:
    out = []
    for cell in cells:
        cid = cell_id(cell)
        marks = [pytest.mark.xfail(strict=True, reason=KNOWN_RED[cid])] if cid in KNOWN_RED else []
        out.append(pytest.param(cell, id=cid, marks=marks))
    return out


def _report(name: str, outcome: Outcome) -> None:
    """Append one JSON line per cell when ``CREATION_MATRIX_REPORT`` names a file — the
    round's register quotes the resulting distribution (not part of the assertion)."""
    import json
    import os

    path = os.environ.get("CREATION_MATRIX_REPORT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"cell": name, "status": outcome.status, "code": outcome.code,
                             "dry_run_error": outcome.dry_run_error}) + "\n")


@pytest.mark.parametrize("cell", _ids(CELLS))
def test_every_cell_deploys_or_is_refused_at_creation(world, cell):
    outcome = run_cell(world, cell, CELLS.index(cell))
    name = cell_id(cell)
    _report(name, outcome)
    if outcome.status == 202:
        assert outcome.dry_run_error is None, (
            f"{name}: POST /api/agents accepted the spec (202, agent + job written) but the "
            f"{cell['method']} generate stage refuses it — {outcome.dry_run_error}. "
            "Creation-time validation must refuse this combination (or the method must "
            "render it)."
        )
        return
    assert 400 <= outcome.status < 500, f"{name}: unexpected {outcome.status} {outcome.code}"
    agents, jobs = _rows(f"mx-{CELLS.index(cell)}")
    assert not agents and not jobs, f"{name}: refused ({outcome.code}) but rows were written"
    # the error envelope the live console answers (console-api/agents_create_pydantic_422.json)
    assert set(outcome.body) == {"code", "message", "detail"}, f"{name}: envelope {outcome.body}"
    if outcome.code == "agent.reference_missing":
        # CONTRACT docs/api.md "Creation-time validation" (N16-obs73): a reference to a row of
        # this workspace that does not exist — only the "missing" KB value may produce it
        assert cell["kb"] == "missing" and outcome.detail.get("value") == "KBMISSING1", name
    if outcome.code == "agent.method_unsupported":
        # CONTRACT docs/api.md "Creation-time validation": detail {method, field, value, supported}
        assert set(outcome.detail) - {"legacy_code"} == {"method", "field", "value", "supported"}, (
            outcome.detail)
        caps = method_capabilities(cell["method"])
        detail = outcome.detail or {}
        field = detail.get("field")
        declared = _declared(caps, field)
        assert declared is not None, f"{name}: unknown field {field!r} in detail"
        assert sorted(detail.get("supported") or []) == sorted(declared), (
            f"{name}: detail.supported {detail.get('supported')} ≠ declared {sorted(declared)}"
        )


def _declared(caps, field: str | None) -> list[str] | None:
    table = {
        "tools.builtin": getattr(caps, "builtin_tools", None),
        "tools.type": getattr(caps, "tool_types", None),
        "tools.auth.mode": getattr(caps, "tool_auth_modes", None),
        "native_tools": getattr(caps, "native_tools", None),
        "protocol": getattr(caps, "protocols", None),
        "byoc.artifact_kind": getattr(caps, "byoc_kinds", None),
    }
    value = table.get(field or "")
    return sorted(value) if value is not None else None


def test_matrix_shape():
    """Every dimension value appears, every pair is covered, and the matrix stays small
    enough for the gate (CONTRACT docs/cn/interfaces/testing.md §6.2; design §3: ≤ 60 s
    added to `make verify`)."""
    for dim, values in DIMENSIONS.items():
        for value in values:
            assert any(c[dim] == value for c in CELLS), f"{dim}={value} never appears"
    names = list(DIMENSIONS)
    for a, b in combinations(names, 2):
        wanted = {(c[a], c[b]) for c in product(*DIMENSIONS.values())
                  for c in [dict(zip(names, c, strict=True))] if valid(c)}
        covered = {(c[a], c[b]) for c in CELLS}
        assert wanted <= covered, f"pair {a}×{b} missing {wanted - covered}"
    assert len(CELLS) <= 200, len(CELLS)
