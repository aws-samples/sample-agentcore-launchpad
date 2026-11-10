"""Cross-module consistency layer (round 16, r16-g): the console surface against the
server's declarations, in one file.

Every assertion is a *differential*: two sources that the contracts say must agree are
read independently and compared — nothing here copies a number or a list out of either.
The frontend sources are read as text (the backend suite has no TypeScript runtime), the
backend sources are imported.

Sections and the contract each one cites:

* (a) **navigation ↔ route policy ↔ capabilities** — `docs/cn/interfaces/features.md`
  ("Both nav tables carry `feature`; `FeatureGate` renders NotFound for a hidden module's
  URL", `featureForPath` mapping), `docs/cn/acceptance/E01-workbench.md` AC-E01-01 ("sidebar
  items equal the `features` array of `GET /api/capabilities`"), `core/route_policy.py`
  (`ADMIN` marks user / workspace management, announcement publication …).
* (b) **UI option lists ⊆ server-declared sets** — the frontend comments that name their
  backend twin ("mirrors backend …STAGE_ORDER exactly", "mirrors
  `opensearch_http.MERGE_SETTINGS_*`", "same rule as the backend's …"); a constant with no
  server twin is listed as such, so the review can see the whole inventory.
* (c) **`apiErrors.*` locale keys ↔ backend error codes** — `core/errors.py` ("the frontend
  translates [codes] into the active locale"), `lib/api.ts::localizedMessage`.
* (d) **locale keys used by the code vs present** — `scripts/i18n_check.py --strict`
  (key-usage audit), with i18next plural suffixes understood.

A failing differential is a FINDING (`N16-obs5x`, `docs/cn/reports/bugs-round-16.md`) kept
as `xfail(strict=True)` until the product catches up — never weakened to go green.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import errors as errors_mod
from app.core.config import get_settings
from app.core.features import FEATURES
from app.core.route_policy import ADMIN, MEMBER, PUBLIC, ROUTE_POLICY
from app.main import create_app

ROOT = Path(__file__).resolve().parents[3]
FRONTEND = ROOT / "frontend" / "src"
BACKEND_APP = ROOT / "backend" / "app"
LOCALES = FRONTEND / "locales"


# ─── frontend source readers (text, no TS runtime) ────────────────────────────


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def ts_string_array(rel: str, name: str) -> list[str]:
    """The string literals of `export const NAME(: T)? = [ ... ]`."""
    text = _strip_comments(_read(rel))
    match = re.search(rf"export const {name}\b[^=]*=\s*\[(.*?)\]", text, re.S)
    assert match is not None, f"{rel}: no `export const {name} = [...]`"
    return re.findall(r'"([^"]+)"', match.group(1))


def ts_record(rel: str, name: str) -> dict[str, Any]:
    """`export const NAME: Record<…> = { key: value, … }` with scalar / tuple values."""
    text = _strip_comments(_read(rel))
    match = re.search(rf"export const {name}\b[^=]*=\s*\{{(.*?)\n\}}", text, re.S)
    assert match is not None, f"{rel}: no `export const {name} = {{...}}`"
    out: dict[str, Any] = {}
    for key, raw in re.findall(r"([\w$.]+):\s*(\[[^\]]*\]|null|-?\d+(?:\.\d+)?)", match.group(1)):
        if raw == "null":
            out[key] = None
        elif raw.startswith("["):
            out[key] = tuple(int(v) for v in re.findall(r"-?\d+", raw))
        else:
            out[key] = float(raw) if "." in raw else int(raw)
    return out


def nav_items() -> list[dict[str, Any]]:
    """Every sidebar entry of `v2/nav.ts`: {to, feature, admin, group}."""
    text = _strip_comments(_read("v2/nav.ts"))
    body = text.split("export const V2_NAV", 1)[1].split("\n];", 1)[0]
    items: list[dict[str, Any]] = []
    group = None
    for match in re.finditer(r'key:\s*"(\w+)"|\{\s*to:\s*"([^"]+)"([^}]*)\}', body):
        if match.group(1):
            group = match.group(1)
            continue
        rest = match.group(3)
        feature = re.search(r'feature:\s*"(\w+)"', rest)
        items.append({
            "to": match.group(2),
            "feature": feature.group(1) if feature else None,
            "admin": "admin: true" in rest,
            "group": group,
        })
    assert items, "nav.ts parsed no entries"
    return items


def path_feature_map() -> dict[str, str]:
    """`lib/capabilities.ts::PATH_FEATURE` — the gate's first-segment → feature table."""
    text = _strip_comments(_read("lib/capabilities.ts"))
    block = text.split("const PATH_FEATURE", 1)[1].split("};", 1)[0]
    return dict(re.findall(r'"?([\w-]+)"?:\s*"(\w+)"', block))


def feature_for_path(path: str, table: dict[str, str]) -> str | None:
    """Re-implements `featureForPath` so the gate and the sidebar can be compared."""
    parts = [p for p in path.split("?", 1)[0].split("/") if p]
    if parts and parts[0] == "v2":
        parts = parts[1:]
    head = parts[0] if parts else None
    return table.get(head) if head else None


def routed_v2_paths() -> set[str]:
    app = _read("App.tsx")
    v2_block = app.split('path="v2"', 1)[1].split("<Route element={<ConsoleShell />}>", 1)[0]
    return {"/v2"} | {f"/v2/{p}" for p in re.findall(r'<Route path="([^"*]+)"', v2_block)}


def flatten(obj: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def locale_keys(lang: str = "en") -> dict[str, Any]:
    return flatten(json.loads((LOCALES / lang / "common.json").read_text(encoding="utf-8")))


def routes_under(prefix: str) -> dict[tuple[str, str], str]:
    return {
        key: role for key, role in ROUTE_POLICY.items()
        if key[1] == prefix or key[1].startswith(f"{prefix}/")
    }


# ─── (a) navigation ↔ route policy ↔ capabilities ─────────────────────────────

# The API surface each sidebar page is built on (E01–E22 §1 "Routes" of the acceptance
# sheets). The page → prefix relation is the only hand-written input of section (a); the
# roles and the feature flags are read from the two sources being compared.
NAV_API_PREFIXES: dict[str, tuple[str, ...]] = {
    "/v2": ("/api/overview", "/api/announcements"),
    "/v2/agents": ("/api/agents",),
    "/v2/assistant": ("/api/assistant",),
    "/v2/registry": ("/api/registry",),
    "/v2/knowledge-bases": ("/api/knowledge-bases",),
    "/v2/chat": ("/api/chat",),
    "/v2/memory": ("/api/memory",),
    "/v2/connections": ("/api/identity",),
    "/v2/governance": ("/api/governance",),
    "/v2/policy": ("/api/policy-engines",),
    "/v2/observability": ("/api/observability",),
    "/v2/eval/insights": ("/api/eval",),
    "/v2/eval/data": ("/api/eval",),
    "/v2/eval/tasks": ("/api/eval",),
    "/v2/eval/online": ("/api/eval",),
    "/v2/eval/evaluators": ("/api/eval",),
    "/v2/eval/experiments": ("/api/runtime-canaries",),
    "/v2/skill-lab": ("/api/skill-lab",),
    "/v2/workspaces": ("/api/workspaces",),
    "/v2/users": ("/api/users",),
    "/v2/settings/model-providers": ("/api/settings",),
    "/v2/videos": ("/api/videos",),
}


def test_every_nav_feature_is_a_declared_feature_and_the_two_enums_agree():
    """features.md: `FEATURES` is the closed set a console may show; the frontend's
    `ALL_FEATURES` filters the server's list, so a feature missing on either side is a
    module one side can never show."""
    frontend = ts_string_array("lib/capabilities.ts", "ALL_FEATURES")
    assert set(frontend) == set(FEATURES), {
        "frontend_only": sorted(set(frontend) - set(FEATURES)),
        "backend_only": sorted(set(FEATURES) - set(frontend)),
    }
    for item in nav_items():
        assert item["feature"] is None or item["feature"] in FEATURES, item


def test_every_nav_entry_is_routed_and_maps_to_a_classified_api_prefix():
    routed = routed_v2_paths()
    for item in nav_items():
        assert item["to"] in routed, f"{item['to']} is in the sidebar but not in App.tsx"
        assert item["to"] in NAV_API_PREFIXES, f"{item['to']}: add its API prefix above"
        for prefix in NAV_API_PREFIXES[item["to"]]:
            assert routes_under(prefix), f"{prefix} has no ROUTE_POLICY entry"


def test_admin_only_nav_entries_match_admin_only_write_routes():
    """route_policy.py: `ADMIN` marks user / workspace management (and the hub-global
    model registry, D21); the sidebar hides exactly those entries from members. Admin item
    ⇒ every write route under its API is ADMIN; member item ⇒ members can read it."""
    for item in nav_items():
        roles = {}
        for prefix in NAV_API_PREFIXES[item["to"]]:
            roles.update(routes_under(prefix))
        writes = {k: v for k, v in roles.items() if k[0] != "GET"}
        reads = {k: v for k, v in roles.items() if k[0] == "GET"}
        if item["admin"]:
            non_admin_writes = sorted(k for k, v in writes.items() if v != ADMIN)
            assert not non_admin_writes, (item["to"], non_admin_writes)
        else:
            member_reads = [k for k, v in reads.items() if v in (MEMBER, PUBLIC)
                            or v.startswith("perm:")]
            assert member_reads, (item["to"], "no member-readable GET under its API")


def test_sidebar_feature_flags_agree_with_the_route_gate():
    """features.md: the nav entry carries `feature`, `FeatureGate` maps the URL's first
    segment to a feature; the same URL must be gated identically by both. (Entries the
    gate never covers — `/v2`, model providers — carry no feature on the sidebar either.)"""
    table = path_feature_map()
    disagreements = {
        item["to"]: {"nav": item["feature"], "gate": feature_for_path(item["to"], table)}
        for item in nav_items()
        if item["to"] not in UNGATED_ENTRIES  # N16-obs51, strict xfail below
        and feature_for_path(item["to"], table) != item["feature"]
    }
    assert disagreements == {}, disagreements


# N16-obs51: sidebar entries whose URL the gate classifies under another feature (or none).
UNGATED_ENTRIES = {"/v2/assistant": "agents", "/v2/eval/experiments": "optimization"}


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs51: /v2/assistant (nav: agents) and /v2/eval/experiments (nav: "
    "optimization) are hidden from the sidebar when their feature is off but the URL gate "
    "classifies them as ungated / `evaluation` (PATH_FEATURE keys on the first segment only) "
    "— docs/cn/reports/bugs-round-16.md",
)
@pytest.mark.parametrize(("to", "nav_feature"), sorted(UNGATED_ENTRIES.items()))
def test_the_sidebar_entry_url_is_gated_like_its_sidebar_entry(to, nav_feature):
    table = path_feature_map()
    item = next(i for i in nav_items() if i["to"] == to)
    assert item["feature"] == nav_feature  # the sidebar side of the row, pinned
    assert feature_for_path(to, table) == nav_feature


def test_the_gate_table_names_only_declared_features():
    assert set(path_feature_map().values()) <= set(FEATURES)


def _app_without(monkeypatch, feature: str):
    kept = [f for f in FEATURES if f != feature]
    monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(kept))
    get_settings.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("feature", FEATURES)
def test_a_disabled_feature_is_absent_from_capabilities(monkeypatch, feature):
    """features.md: `GET /api/capabilities.features` lists the enabled modules — the one
    source both navigations render from (E01 AC-01, E22 AC-10)."""
    for client in _app_without(monkeypatch, feature):
        body = client.get("/api/capabilities").json()
        assert feature not in body["features"], body["features"]
        assert set(body["features"]) <= set(FEATURES)


# features.md "Backend boundaries": the router is mounted only with the feature
# (evaluation / optimization / policy → a hidden module answers 404), or its backend is
# registered only with the feature (memory / registry / knowledge_bases → 503
# `feature.unavailable`). The probe is the first GET under the module's prefix.
BACKEND_GATES: dict[str, tuple[str, int, str]] = {
    "evaluation": ("/api/eval", 404, "http.404"),
    "optimization": ("/api/runtime-canaries", 404, "http.404"),
    "policy": ("/api/policy-engines", 404, "http.404"),
    "memory": ("/api/memory", 503, "feature.unavailable"),
    "registry": ("/api/registry", 503, "feature.unavailable"),
    "knowledge_bases": ("/api/knowledge-bases", 503, "feature.unavailable"),
}


def _first_get(prefix: str) -> str:
    candidates = sorted(
        path for (method, path), _ in routes_under(prefix).items()
        if method == "GET" and "{" not in path
    )
    assert candidates, prefix
    return candidates[0]


@pytest.mark.parametrize("feature", sorted(BACKEND_GATES))
def test_a_disabled_feature_closes_its_backend_boundary(monkeypatch, feature):
    prefix, status, code = BACKEND_GATES[feature]
    for client in _app_without(monkeypatch, feature):
        res = client.get(_first_get(prefix))
        assert res.status_code == status, (feature, res.status_code, res.text)
        assert res.json()["code"] == code


# ─── (b) hard-coded UI option lists ⊆ server-declared sets ───────────────────


def _server_sets() -> dict[str, Any]:
    """The backend twins, imported — one lookup per frontend constant."""
    from typing import get_args

    from app import harness_tool_access
    from app.assistant import proposal
    from app.assistant.proposal import ProposalContent
    from app.evaluation import online_evaluators, run_coverage
    from app.routers import memory_resources
    from app.routers import tools as tools_router
    from app.schemas import agent as agent_schema
    from app.services import governance, opensearch_http, registry_local, workspace_bootstrap
    from app.services import users as users_service
    from app.services.strands_eval import evaluators as builtin_evaluators
    from app.skill_lab import jobs as skill_jobs
    from app.skill_lab import tasksets

    return {
        "NATIVE_HARNESS_TOOLS": set(harness_tool_access.NATIVE_HARNESS_TOOLS),
        "BUILTIN_TOOLS": {t["name"] for t in tools_router.BUILTIN_TOOLS},
        "SUPPORTED_ACTING_MODES": set(agent_schema.SUPPORTED_ACTING_MODES),
        "AGENT_SPEC_FIELDS": set(agent_schema.AgentSpec.model_fields),
        "TOOL_REF_FIELDS": set(agent_schema.ToolRef.model_fields),
        "PROPOSAL_FIELDS": set(ProposalContent.model_fields),
        "RATE_LIMIT_FIXED_KEYS": set(governance.RATE_LIMIT_FIXED_KEYS),
        "RATE_LIMIT_METRICS": set(governance.RATE_LIMIT_PERIODS),
        "ALLOWED_MODEL_IDS": set(agent_schema.allowed_model_ids()),
        "SKILL_LAB_TERMINAL": set(skill_jobs.TERMINAL_STATUSES),
        "ONLINE_EVAL_DEFAULT": list(online_evaluators.ONLINE_EVAL_DEFAULT),
        "BUILTIN_EVALUATORS": set(builtin_evaluators.LEVELS),
        "FAILURE_CLASSES": list(run_coverage.FAILURE_CLASSES),  # R3-f
        "BOOTSTRAP_STAGES": list(workspace_bootstrap.STAGE_ORDER),
        "EXTRACTED_STRATEGIES": set(opensearch_http.EXTRACTED_STRATEGIES),
        "MERGE_DEFAULTS": dict(opensearch_http.MERGE_SETTINGS_DEFAULTS),
        "MERGE_BOUNDS": dict(opensearch_http.MERGE_SETTINGS_BOUNDS),
        "REGISTRY_STATUSES": list(registry_local.STATUSES),
        "REGISTRY_TYPES": set(registry_local.DESCRIPTOR_KINDS),
        "BYOC_KINDS": set(get_args(agent_schema.ByocArtifactKind)),
        "BYOC_PYTHON_VERSIONS": set(get_args(agent_schema.ByocPythonVersion)),
        "REASONING_EFFORTS": set(get_args(agent_schema.ReasoningEffort)),
        "AGENT_PERMISSIONS": set(users_service.AGENT_PERMISSIONS),
        "FISHBONE_DIMENSIONS": list(proposal.FISHBONE_DIMENSIONS),
        "DEFAULT_MEMORY_STRATEGIES": list(
            memory_resources.CreateMemoryResourceRequest.model_fields["strategies"]
            .default_factory()  # type: ignore[misc]
        ),
        "SPLIT_ORDER": list(tasksets.SPLIT_KEYS),
        "KB_SUPPORTED_EXT": set(_lambda_literal("NATIVE_TEXT_EXT")) | {".pdf"},
    }


LAMBDA_HANDLER = ROOT / "backend" / "lambdas" / "memory_extractor" / "handler.py"


def _lambda_literal(name: str) -> Any:
    """A module-level literal of the ingestion Lambda, read as text (the handler is not an
    `app` module and its import pulls the Lambda-side dependencies)."""
    import ast

    text = LAMBDA_HANDLER.read_text(encoding="utf-8")
    match = re.search(rf"^{name}\s*=\s*(.+)$", text, re.M)
    assert match is not None, f"{LAMBDA_HANDLER}: no `{name} = …`"
    return ast.literal_eval(match.group(1))


def ts_number(rel: str, name: str) -> int:
    """`export const NAME = 2_000_000;` → 2000000."""
    text = _strip_comments(_read(rel))
    match = re.search(rf"export const {name}\b[^=]*=\s*([\d_]+)\s*;", text)
    assert match is not None, f"{rel}: no `export const {name} = <number>`"
    return int(match.group(1).replace("_", ""))


@pytest.mark.parametrize(
    ("ui_name", "lambda_name"),
    [("KB_MAX_DOC_BYTES", "MAX_DOC_BYTES"), ("KB_MAX_CHUNKS", "MAX_CHUNKS")],
)
def test_the_kb_form_states_the_lambda_limits(ui_name, lambda_name):
    """N17-obs239 / obs240: the data-source form tells the user the ingestion limits — the
    numbers it shows must be the Lambda's own (`docs/cn/interfaces/knowledge-backend.md`
    "Supported document types and limits")."""
    assert ts_number("lib/knowledgeBases.ts", ui_name) == _lambda_literal(lambda_name)


@pytest.fixture(scope="module")
def server():
    return _server_sets()


# (frontend file, constant) → how it relates to which server set. `None` = a UI-only
# vocabulary with no server twin by nature (URL parameter names, wizard steps, marketing
# placeholders): listed so the inventory is complete and reviewable.
INVENTORY: dict[tuple[str, str], tuple[str, str] | None] = {
    ("lib/agent-defaults.ts", "DEFAULT_HARNESS_NATIVE_TOOLS"): ("==", "NATIVE_HARNESS_TOOLS"),
    ("lib/api.ts", "HARNESS_NATIVE_TOOLS"): ("==", "NATIVE_HARNESS_TOOLS"),
    ("lib/agent-spec.ts", "BUILTIN_TOOLS"): ("==", "BUILTIN_TOOLS"),
    ("lib/agent-spec.ts", "SUPPORTED_ACTING_MODES"): ("==", "SUPPORTED_ACTING_MODES"),
    ("lib/agent-spec.ts", "REPUBLISH_CARRIED_FIELDS"): ("<=", "AGENT_SPEC_FIELDS"),
    ("lib/agent-spec.ts", "REPUBLISH_CARRIED_TOOL_FIELDS"): ("<=", "TOOL_REF_FIELDS"),
    ("lib/assistant.ts", "PROPOSAL_EDITED_KEYS"): ("<=", "PROPOSAL_FIELDS"),
    ("lib/assistant.ts", "PROPOSAL_CARRIED_KEYS"): ("<=", "PROPOSAL_FIELDS"),
    ("lib/governanceRateLimits.ts", "RATE_LIMIT_FIXED_KEYS"): ("==", "RATE_LIMIT_FIXED_KEYS"),
    ("lib/governanceRateLimits.ts", "RATE_LIMIT_METRICS"): ("==", "RATE_LIMIT_METRICS"),
    ("lib/skillLab.ts", "JUDGE_MODEL_SUGGESTIONS"): ("<=", "ALLOWED_MODEL_IDS"),
    ("lib/skillLab.ts", "RESUMABLE_STATUSES"): ("<=", "SKILL_LAB_TERMINAL"),
    ("lib/canary.ts", "ONLINE_EVAL_DEFAULT"): ("==list", "ONLINE_EVAL_DEFAULT"),
    ("lib/evaluation.ts", "DEFAULT_EVALUATORS"): ("<=", "BUILTIN_EVALUATORS"),
    # R3-f: the unscored-row causes, in the server's order
    ("lib/evaluation.ts", "FAILURE_CLASSES"): ("==", "FAILURE_CLASSES"),
    ("lib/workspaces.ts", "BOOTSTRAP_STAGES"): ("==list", "BOOTSTRAP_STAGES"),
    ("lib/memory.ts", "MEMORY_STRATEGY_KEYS"): ("==", "EXTRACTED_STRATEGIES"),
    ("lib/memory.ts", "MERGE_KEYS"): ("==", "MERGE_DEFAULTS"),
    ("lib/registry.ts", "REGISTRY_STATUSES"): ("==list", "REGISTRY_STATUSES"),
    # the console registers / filters the three GA kinds; `CUSTOM` (inline content) is
    # server-side only, so the UI list is a subset by design
    ("lib/registry.ts", "REGISTRY_TYPES"): ("<=", "REGISTRY_TYPES"),
    ("lib/agent-spec.ts", "BYOC_KINDS"): ("==", "BYOC_KINDS"),
    ("lib/agent-spec.ts", "BYOC_PYTHON_VERSIONS"): ("==", "BYOC_PYTHON_VERSIONS"),
    ("lib/models.ts", "REASONING_EFFORTS"): ("==", "REASONING_EFFORTS"),
    ("lib/api.ts", "AGENT_PERMISSIONS"): ("==", "AGENT_PERMISSIONS"),
    ("lib/api.ts", "FISHBONE_DIMENSIONS"): ("==list", "FISHBONE_DIMENSIONS"),
    ("lib/memory.ts", "DEFAULT_MEMORY_STRATEGIES"): ("==list", "DEFAULT_MEMORY_STRATEGIES"),
    ("lib/skillLabTasksets.ts", "SPLIT_ORDER"): ("==list", "SPLIT_ORDER"),
    ("lib/fishbone.ts", "FISHBONE_TOP"): ("<=", "FISHBONE_DIMENSIONS"),
    ("lib/fishbone.ts", "FISHBONE_BOTTOM"): ("<=", "FISHBONE_DIMENSIONS"),
    ("lib/capabilities.ts", "ALL_FEATURES"): None,  # covered by the enum test above
    ("lib/skillLab.ts", "LIVE_STATUSES"): None,  # the non-terminal half: no server tuple
    ("lib/workspaces.ts", "WORKSPACE_REGIONS"): None,  # covered by the partition test
    ("lib/evaluation-url.ts", "CANARY_PARAMS"): None,  # URL parameter names
    ("lib/assistant.ts", "CREATION_STEPS"): None,  # wizard steps
    # the picker's type list = the ingestion Lambda's native text set + `.pdf` (console upload
    # extracts the text into a `_text/` sidecar — N17-obs239 / obs240, cn/r17-af)
    ("lib/knowledgeBases.ts", "KB_SUPPORTED_EXT"): ("==", "KB_SUPPORTED_EXT"),
    ("lib/agent-spec.ts", "A2A_SKILL_SEEDS"): None,  # form seed rows (name / description)
    ("lib/agent-spec.ts", "FILESYSTEM_METHODS"): None,  # which methods show the card; no tuple
    ("lib/governance.ts", "POLICY_PREFILLS"): None,  # editor prefill templates
    # N17-obs271: the DISPLAY order of the ingestion statistic keys the extractor Lambda writes
    # (`handler.planner_counts` + the fan-out tallies) — no exported server tuple; an unknown
    # key still renders (humanized), so drift costs a label, never a value
    ("lib/knowledgeBases.ts", "STAT_ORDER"): None,
}


def test_the_inventory_covers_every_exported_string_array_in_lib():
    """Every `export const X = [ "…", … ]` under `lib/*.ts` must be classified above —
    a new hard-coded list cannot ship without naming its server twin (or its absence)."""
    found: set[tuple[str, str]] = set()
    for path in sorted((FRONTEND / "lib").glob("*.ts")):
        if path.name.endswith(".test.ts"):
            continue
        text = _strip_comments(path.read_text(encoding="utf-8"))
        for name, body in re.findall(r"export const (\w+)\b[^=\n]*=\s*\[([^\]]*)\]", text):
            if re.search(r'"[^"]+"', body) and not re.search(r"\w+\(", body):
                found.add((f"lib/{path.name}", name))
    unclassified = sorted(found - set(INVENTORY))
    assert not unclassified, unclassified
    stale = sorted(set(INVENTORY) - found)
    assert not stale, stale


def _relate(relation: str, ui: list[str], twin: Any) -> tuple[bool, Any]:
    if relation == "==":
        return set(ui) == set(twin), {"ui_only": sorted(set(ui) - set(twin)),
                                      "server_only": sorted(set(twin) - set(ui))}
    if relation == "==list":
        return list(ui) == list(twin), {"ui": list(ui), "server": list(twin)}
    if relation == "<=":
        return set(ui) <= set(twin), {"ui_not_on_server": sorted(set(ui) - set(twin))}
    raise ValueError(relation)


TWINNED = sorted(k for k, v in INVENTORY.items() if v is not None)
# Differentials that currently fail: each one is a register row, kept strict.
# (N16-obs50 — `lib/workspaces.ts::BOOTSTRAP_STAGES` lagging `STAGE_ORDER` by `skill-lab` and
# `policy` — was found by this test and fixed in the same branch; the dict is empty until the
# next drift.)
KNOWN_DRIFT: dict[tuple[str, str], str] = {}


@pytest.mark.parametrize(("rel", "name"), TWINNED, ids=[f"{r}:{n}" for r, n in TWINNED])
def test_hard_coded_ui_lists_match_their_server_twin(server, rel, name, request):
    relation, twin_name = INVENTORY[(rel, name)]  # type: ignore[misc]
    ui = ts_string_array(rel, name)
    ok, detail = _relate(relation, ui, server[twin_name])
    if (rel, name) in KNOWN_DRIFT:
        if ok:
            pytest.fail(f"{KNOWN_DRIFT[(rel, name)]} — now agrees: remove it from KNOWN_DRIFT")
        pytest.xfail(KNOWN_DRIFT[(rel, name)])
    assert ok, (rel, name, relation, twin_name, detail)


def test_proposal_editor_keys_plus_carried_keys_cover_the_whole_contract(server):
    """`lib/assistant.ts`: edited ∪ carried ∪ {tool_functions (narrowed), version (server
    default)} must be the proposal contract — a contract key outside both lists is one a
    member edit would silently drop (the N15-obs13 class)."""
    edited = set(ts_string_array("lib/assistant.ts", "PROPOSAL_EDITED_KEYS"))
    carried = set(ts_string_array("lib/assistant.ts", "PROPOSAL_CARRIED_KEYS"))
    assert not edited & carried
    uncovered = server["PROPOSAL_FIELDS"] - edited - carried - {"tool_functions", "version"}
    assert uncovered == set(), sorted(uncovered)


def test_the_fishbone_halves_partition_the_server_dimensions(server):
    """`lib/fishbone.ts` draws the five-dimension fishbone (proposal.py) in two halves."""
    top = ts_string_array("lib/fishbone.ts", "FISHBONE_TOP")
    bottom = ts_string_array("lib/fishbone.ts", "FISHBONE_BOTTOM")
    assert not set(top) & set(bottom)
    assert set(top) | set(bottom) == set(server["FISHBONE_DIMENSIONS"])


def test_memory_merge_defaults_and_bounds_match_the_backend(server):
    """`lib/memory.ts` says it mirrors `opensearch_http.MERGE_SETTINGS_*` (N14-obs1)."""
    assert ts_record("lib/memory.ts", "MERGE_DEFAULTS") == server["MERGE_DEFAULTS"]
    assert ts_record("lib/memory.ts", "MERGE_BOUNDS") == server["MERGE_BOUNDS"]


def test_skill_lab_status_vocabulary_is_closed():
    """`lib/skillLab.ts`: LIVE ∪ RESUMABLE ⊆ the job statuses the backend writes."""
    from app.skill_lab import jobs as skill_jobs

    ui = set(ts_string_array("lib/skillLab.ts", "LIVE_STATUSES")) | set(
        ts_string_array("lib/skillLab.ts", "RESUMABLE_STATUSES"))
    source = (BACKEND_APP / "skill_lab" / "jobs.py").read_text(encoding="utf-8")
    written = set(re.findall(r'status\s*=\s*"(\w+)"', source)) | set(skill_jobs.TERMINAL_STATUSES)
    assert ui <= written, sorted(ui - written)


def test_workspace_region_suggestions_are_china_partition_regions():
    """`lib/workspaces.ts::WORKSPACE_REGIONS` is a picker suggestion list; every entry must
    be a region the backend resolves to the `aws-cn` partition (partition.md)."""
    from app.services import aws_clients

    for region in ts_string_array("lib/workspaces.ts", "WORKSPACE_REGIONS"):
        assert aws_clients.partition_for_region(region) == "aws-cn", region


# ─── (c) apiErrors.* locale keys ↔ backend error codes ───────────────────────

_CODE = r"[a-z][a-z0-9_]*\.[a-z0-9_.]+"
# Codes the console synthesises itself (`lib/api.ts`): no backend emitter by design.
FRONTEND_ORIGIN_CODES = {"network", "http.invalid_json"}
FRONTEND_ORIGIN_PATTERNS = (re.compile(r"^http\.\d{3}$"),)


def backend_error_registry() -> tuple[set[str], list[re.Pattern[str]], set[str]]:
    """(static codes raised through AppError/NotFoundError/envelope, dynamic f-string
    code patterns, every dotted-code-shaped literal anywhere in app/)."""
    static: set[str] = set()
    dynamic: list[re.Pattern[str]] = []
    literals: set[str] = set()
    raise_re = re.compile(
        r'(?:AppError|NotFoundError|envelope|super\(\)\.__init__)\(\s*(f?)"('
        + _CODE + r'|[a-z_.]*\{[^"]*)"'
    )
    # `def _error(code, …): return AppError(f"skill_lab.{code}", …)` — the helper's callers
    # carry the real suffixes, so every `_error("suffix")` in app/ is a static code.
    helper_re = re.compile(
        r"def (\w+)\((\w+)[^)]*\)[^\n]*\n(?:[^\n]*\n){0,3}?"
        r"[^\n]*AppError\(\s*f\"([a-z_]+)\.\{\2\}\""
    )
    texts = {path: path.read_text(encoding="utf-8") for path in BACKEND_APP.rglob("*.py")}
    helpers: list[tuple[str, str]] = []
    for text in texts.values():
        literals |= set(re.findall(r'"(' + _CODE + r')"', text))
        for is_f, code in raise_re.findall(text):
            if is_f or "{" in code:
                parts = re.split(r"\{[^}]*\}", code)
                dynamic.append(re.compile("^" + "[a-z0-9_]+".join(map(re.escape, parts)) + "$"))
            else:
                static.add(code)
        helpers += [(name, prefix) for name, _, prefix in helper_re.findall(text)]
    for name, prefix in helpers:
        for text in texts.values():
            static |= {f"{prefix}.{suffix}"
                       for suffix in re.findall(rf'(?<![\w.]){name}\(\s*"([a-z0-9_]+)"', text)}
    static |= {code for _, code in errors_mod.AWS_ERROR_MAP.values()}
    static |= set(errors_mod.OPENSEARCH_ERROR_MESSAGES)
    static.add("validation.invalid_request")
    return static, dynamic, literals


def _emitted(code: str, static: set[str], dynamic: Iterable[re.Pattern[str]],
             literals: set[str]) -> bool:
    return code in static or code in literals or any(p.match(code) for p in dynamic)


def api_error_keys() -> set[str]:
    return {k.removeprefix("apiErrors.") for k in locale_keys() if k.startswith("apiErrors.")}


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs55: seven apiErrors.* keys name codes no backend path raises any more "
    "(C5 dead copy) — docs/cn/reports/bugs-round-16.md",
)
def test_every_api_error_locale_key_has_a_backend_emitter():
    """A key no backend code can produce is dead copy (C5); the console would never show it."""
    static, dynamic, literals = backend_error_registry()
    dead = sorted(
        key for key in api_error_keys()
        if key not in FRONTEND_ORIGIN_CODES
        and not any(p.match(key) for p in FRONTEND_ORIGIN_PATTERNS)
        and not _emitted(key, static, dynamic, literals)
        and not _emitted(re.sub(r"_(one|other)$", "", key), static, dynamic, literals)
    )
    assert dead == [], {"dead_apiErrors_keys": dead}


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs54: backend error codes raised through AppError without console copy — "
    "the toast falls back to the operator-facing English log message "
    "(docs/cn/reports/bugs-round-16.md)",
)
def test_every_backend_error_code_has_console_copy():
    static, _, _ = backend_error_registry()
    keys = api_error_keys()
    unmapped = sorted(code for code in static if code not in keys)
    assert unmapped == [], {"count": len(unmapped), "by_prefix": _by_prefix(unmapped)}


def _by_prefix(codes: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for code in codes:
        out[code.split(".", 1)[0]] = out.get(code.split(".", 1)[0], 0) + 1
    return dict(sorted(out.items()))


def test_the_console_copy_covers_the_codes_the_lower_risk_modules_emit():
    """E01 / E05 / E20 / E22 acceptance sheets name these codes as what the UI must
    translate (NC-E01-05/06, NC-E05-03/06/09, NC-E20-02/08, NC-E22-02/08)."""
    keys = api_error_keys()
    sheet_codes = {
        "announcements.conflict", "announcements.not_found",
        "videos.conflict", "videos.not_found",
        "observability.trace_not_found", "observability.session_not_found",
        "observability.session_spans_missing", "validation.invalid_request",
    }
    assert sheet_codes <= keys, sorted(sheet_codes - keys)


def test_the_e05_fault_codes_have_console_copy():
    """NC-E05-02 / NC-E05-09 name these codes; they had no console copy until r16-g
    (N16-obs54, partial fix: the two E05 codes gained copy, the rest stay registered)."""
    assert {"observability.query_failed", "observability.bad_id"} <= api_error_keys()


# ─── (d) locale keys used by the code vs present ──────────────────────────────

T_CALL_RE = re.compile(r"""[^\w.]t\(\s*(["'`])(.+?)\1""")
PLURAL_SUFFIXES = ("_zero", "_one", "_two", "_few", "_many", "_other")


def _source_files() -> list[Path]:
    return [p for p in sorted(FRONTEND.rglob("*.ts*"))
            if "node_modules" not in p.parts and not p.name.endswith((".test.ts", ".test.tsx"))]


def _usage() -> tuple[set[str], set[str], set[str]]:
    used: set[str] = set()
    dynamic_prefixes: set[str] = set()
    literals: set[str] = set()
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        literals |= set(re.findall(r"""["'`]([\w.\-]+)["'`]""", text))
        for _, key in T_CALL_RE.findall(text):
            if "${" in key:
                dynamic_prefixes.add(key.split("${", 1)[0])
            else:
                used.add(key)
    return used, dynamic_prefixes, literals


def _resolves(key: str, present: set[str]) -> bool:
    return key in present or any(f"{key}{s}" in present for s in PLURAL_SUFFIXES)


def test_every_static_t_key_resolves_in_both_locales():
    """Plural-aware twin of `scripts/i18n_check.py --strict`'s usage audit: a `t("k",
    {count})` call resolves through `k_one` / `k_other`."""
    used, _, _ = _usage()
    for lang in ("en", "zh-CN"):
        present = set(locale_keys(lang))
        missing = sorted(k for k in used if not _resolves(k, present))
        assert missing == [], {lang: missing}


def test_no_locale_key_is_dead():
    """A key no `t()` call, data literal or dynamic prefix can reach is dead copy (C5).
    Plural variants count as used through their base key."""
    used, prefixes, literals = _usage()
    present = set(locale_keys())
    reachable = used | literals

    def base(key: str) -> str:
        for suffix in PLURAL_SUFFIXES:
            if key.endswith(suffix):
                return key[: -len(suffix)]
        return key

    dead = sorted(
        k for k in present
        if base(k) not in reachable and not any(k.startswith(p) for p in prefixes)
    )
    assert dead == [], {"count": len(dead), "dead": dead[:80]}


def test_zh_cn_has_exactly_the_en_keys():
    assert set(locale_keys("zh-CN")) == set(locale_keys("en"))
