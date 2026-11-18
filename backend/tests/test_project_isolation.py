"""Project isolation end to end (design projects §9.3-3).

One workspace holds projects alpha and beta, and every project-carrying table
(`ProjectOwned` / `ProjectShared`) gets a row in each — seeded generically from the
mappers, so a new project-owned table is covered the day it ships. A member of
alpha only (and, for the administrator routes, an administrator inside alpha) then
calls every workspace- and project-scoped `GET /api` route in `ROUTE_POLICY` with
its path and id-like query parameters filled from **beta's** rows, and:

* no response body names a beta row (its id or any of its seeded strings), whatever
  the status — a list stays empty of beta, a by-id read never echoes a beta object;
* AWS is never asked about a beta resource (the boundary refuses before the call);
* a beta id answers 404, or exactly like an id that never existed (same status and
  error code), so a route cannot even confirm that another project's row exists;
* `X-Project: beta` answers 404 `project.not_found` on every route;
* alpha's answers are byte-for-byte the same before and after beta's rows appear
  (counts and aggregates carry no id to scan for).

Controls keep those passes honest: alpha's own ids answer 200 on the by-id routes, an
administrator inside beta sees beta's rows on the list routes, and the same scan with
the ORM boundary switched off reports the leaks.

AWS is a botocore stub that refuses every call (403 AccessDenied) and records what was
sent. Nothing is skipped for calling AWS: a route that answers only from AWS still has
to keep beta's ids out of its calls and its body. The only skips are `BROKEN` (a route
that cannot be called at all today) and `ADMIN_SEES_ALL` (an administrator's read of
workspace infrastructure). A self-check fails when a project-scoped GET route grows a
`{param}` the fixture cannot fill. `/v1` (alpha's API key) and `/share/*` (alpha's
links, and a beta link aimed at an alpha object) are exercised the same way.
"""

import re
import socket
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args, get_origin

import boto3
import pytest
from botocore.awsrequest import AWSResponse
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.testclient import TestClient
from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, select

import app.routers.public_api as public_api
from app.core import route_policy
from app.core.db import DEFAULT_WORKSPACE_ID, Base, SessionLocal
from app.core.project_scope import ProjectShared, _ProjectColumn, project_context
from app.models.ledger import (
    Agent,
    IdentityProvider,
    ManagedMemory,
    ResourceCreator,
    ShareLink,
)
from app.models.network import NetworkProfile, ProjectNetworkProfile
from app.services import api_keys as api_keys_service
from app.services import aws_clients, creators, review_links, share_links
from tests.test_project_scope import (  # noqa: F401
    ADMIN,
    PEER,
    _member_of,
    _project,
    _user,
    app,
)

WS = DEFAULT_WORKSPACE_ID
ALPHA = {"X-Project": "alpha"}
BETA = {"X-Project": "beta"}
PARAM = re.compile(r"\{(\w+)(?::\w+)?\}")


# ── the routes under test ─────────────────────────────────────────────────────


def _walk(routes) -> list:
    found = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            found += _walk(route.original_router.routes)
        else:
            found.append(route)
    return found


def _scoped_gets(app) -> dict[str, Any]:  # noqa: F811
    """path_format → route for every workspace- and project-scoped GET under /api."""
    out = {}
    for route in _walk(app.routes):
        path = getattr(route, "path_format", None) or ""
        if not path.startswith("/api/") or "GET" not in (getattr(route, "methods", None) or ()):
            continue
        if route_policy.required_role("GET", path) is None:
            continue  # the drift test owns unclassified routes
        if route_policy.is_workspace_scoped("GET", path) and route_policy.is_project_scoped(
            "GET", path
        ):
            out[path] = route
    return out


# ── seeding: one row of every project table per project ───────────────────────

# a column naming its parent row → the parent's table (the parent is seeded first)
PARENT_COLUMNS = {
    "agent_id": "agents", "champion_agent_id": "agents", "challenger_agent_id": "agents",
    "target_id": "agents", "conversation_id": "assistant_conversations",
    "run_id": "eval_runs", "task_id": "annotation_tasks", "set_id": "criteria_sets",
    "criteria_set_id": "criteria_sets", "key_id": "api_keys", "bundle_id": "release_bundles",
    "taskset_id": "skill_lab_tasksets", "plan_id": "assistant_evaluation_plans",
    "proposal_id": "assistant_proposals", "dataset_id": "eval_datasets",
}
PARENTS_FIRST = [
    "agents", "assistant_conversations", "assistant_proposals", "assistant_evaluation_plans",
    "eval_datasets", "eval_runs", "criteria_sets", "annotation_tasks", "api_keys",
    "release_bundles", "skill_lab_tasksets",
]


def _project_models() -> list[type]:
    rank = {t: i for i, t in enumerate(PARENTS_FIRST)}
    models = [m.class_ for m in Base.registry.mappers if issubclass(m.class_, _ProjectColumn)]
    return sorted(models, key=lambda c: (rank.get(c.__tablename__, 99), c.__tablename__))


def _overrides(pid: str, table: str, ids: dict[str, Any]) -> dict[str, Any]:
    """Realistic values where a route reads more than the required columns."""
    tag = f"zz{pid}"
    agent = ids.get("agents")
    return {
        "agents": {
            "name": f"{tag}-agent", "method": "harness", "status": "active",
            "spec": {"name": f"{tag}-agent", "method": "harness", "system_prompt": "p"},
            "arn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/{tag}",
            "resource_id": f"{tag}-runtime", "registry_record_id": f"{tag}rec0001",
        },
        "eval_datasets": {"name": f"{tag}-dataset", "items": [{"prompt": "hi"}],
                          "cloud": {"dataset_id": f"{tag}cloudds"}},
        "eval_runs": {"agent_name": f"{tag}-agent", "mode": "dataset", "status": "completed"},
        "criteria_sets": {"agent_id": agent, "kind": "agent", "version": 1},
        "spec_snapshots": {"seq": 1, "spec": {"name": f"{tag}-agent"}},
        "share_links": {"kind": share_links.KIND_CHAT},
        "online_eval_configs": {"config_id": f"{tag}onlinecfg"},
        "skill_lab_tasksets": {"mode": "auto"},
        # shared, so a boundary failure would show it to any caller (not only its owner)
        "assistant_conversations": {"shared": True, "owner_principal": f"user:{tag}-owner"},
    }.get(table, {})


def _fill(pid: str, table: str, col, ids: dict[str, Any]) -> Any:
    if col.name in ("workspace_id", "target_workspace_id"):
        return WS
    if col.name in PARENT_COLUMNS and PARENT_COLUMNS[col.name] in ids:
        return ids[PARENT_COLUMNS[col.name]]
    if col.name == "session_id":
        return ids["session_id"]
    kind = col.type
    if isinstance(kind, (String, Text)):
        return f"zz{pid}-{table}-{col.name}-{uuid.uuid4().hex[:6]}"
    if isinstance(kind, Boolean):
        return False
    if isinstance(kind, Integer):
        return 1
    if isinstance(kind, Float):
        return 0.5
    if isinstance(kind, DateTime):
        return datetime.now(UTC) + timedelta(days=30)  # expiries stay in the future
    if isinstance(kind, JSON):
        return {}
    return None


def _seed(pid: str) -> dict[str, Any]:
    """One row per project table for ``pid`` (+ a few side tables); returns the ids."""
    ids: dict[str, Any] = {"session_id": f"zz{pid}-session-{uuid.uuid4().hex[:12]}"}
    db = SessionLocal()
    try:
        with project_context(pid):
            for model in _project_models():
                table = model.__tablename__
                values = {}
                for col in model.__table__.columns:
                    if col.name == "project_id" or (col.primary_key and (
                            col.default is not None or col.autoincrement is True)):
                        continue
                    needed = (not col.nullable and col.default is None
                              and col.server_default is None) or col.name in (
                        "workspace_id", "name", "session_id", *PARENT_COLUMNS)
                    if needed and (value := _fill(pid, table, col, ids)) is not None:
                        values[col.key] = value
                values.update(_overrides(pid, table, ids))
                row = model(**{k: v for k, v in values.items() if hasattr(model, k)})
                db.add(row)
                db.flush()
                ids.setdefault(table, getattr(row, "id", None))
            tag = f"zz{pid}"
            # a golden set beside the scratch dataset, and the per-kind resources
            # the KB / evaluator / memory routes look up by their AWS ids
            from app.evaluation.models import EvalDataset

            golden = EvalDataset(workspace_id=WS, name=f"{tag}-golden", role="golden",
                                 items=[{"prompt": "hi"}])
            db.add(golden)
            for kind, rid in ((creators.KB, f"{tag.upper()}KB01"),
                              (creators.EVALUATOR, f"{tag}-evaluator-0001")):
                db.add(ResourceCreator(workspace_id=WS, kind=kind, resource_id=rid))
            db.add(ManagedMemory(workspace_id=WS, memory_id=f"{tag}_memory-0001"))
            db.add(IdentityProvider(workspace_id=WS, name=f"{tag}-conn", kind="oauth2"))
            db.flush()
            from app.models.dlc import CriteriaSet

            ids["lineage"] = db.get(CriteriaSet, ids["criteria_sets"]).lineage_id
            ids.update(golden=golden.id, kb=f"{tag.upper()}KB01",
                       evaluator=f"{tag}-evaluator-0001", memory=f"{tag}_memory-0001",
                       connection=f"{tag}-conn")
            db.commit()
        with project_context(None):  # network profiles are workspace rows + an allowed set
            profile = NetworkProfile(id=f"np{pid}{uuid.uuid4().hex[:8]}", workspace_id=WS,
                                     name=f"{tag}-net", vpc_id="vpc-1")
            db.add(profile)
            db.add(ProjectNetworkProfile(project_id=pid, profile_id=profile.id,
                                         workspace_id=WS))
            db.commit()
            ids["profile"] = profile.id
        return ids
    finally:
        db.close()


def _markers(pid: str) -> set[str]:
    """Every string that identifies one of ``pid``'s rows: ids and seeded values.
    Platform rows (``ProjectShared`` with NULL, e.g. the governance decision log) are
    readable from every project by design and so excluded."""
    found: set[str] = set()
    db = SessionLocal()
    try:
        with project_context(None):
            for model in _project_models():
                for row in db.scalars(select(model).where(model.project_id == pid)):
                    for col in model.__table__.columns:
                        value = getattr(row, col.key, None)
                        if isinstance(value, str) and len(value) >= 8 and (
                                col.primary_key or f"zz{pid}" in value):
                            found.add(value)
            found |= {p.id for p in db.scalars(select(NetworkProfile).where(
                NetworkProfile.name == f"zz{pid}-net"))}
    finally:
        db.close()
    return found


# ── the AWS stub ──────────────────────────────────────────────────────────────


class _Denied:
    """A raw HTTP body botocore parses as AccessDeniedException (JSON protocols; the
    XML ones see a bare 403 — a ClientError all the same)."""

    BODY = b'{"__type": "AccessDeniedException", "message": "stubbed AWS"}'

    def stream(self, **_kw):
        yield self.BODY

    def read(self, *_a, **_kw):
        return self.BODY


@pytest.fixture
def no_aws(monkeypatch):
    """Refuse every AWS call; yield what each call carried (URL + body), so a test can
    tell whether another project's id reached AWS at all."""
    sent: list[str] = []

    def refuse(request, **_kw):
        body = request.body
        if isinstance(body, bytes):
            body = body.decode(errors="replace")
        sent.append(f"{request.url} {body}")
        return AWSResponse(request.url, 403, {"x-amzn-ErrorType": "AccessDeniedException"},
                           _Denied())

    def session(account_id, region, *_a, **_kw):
        stub = boto3.Session(aws_access_key_id="AKIASTUBSTUBSTUB", aws_secret_access_key="x",
                             region_name=region)
        stub.events.register("before-send", refuse)
        return stub

    def no_network(self, *args, **_kw):
        raise OSError(f"network connect attempted during a hermetic test: {args}")

    monkeypatch.setattr(aws_clients, "get_session", session)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    return sent


# ── the world ─────────────────────────────────────────────────────────────────


def _client(app, username: str, password: str) -> TestClient:  # noqa: F811
    client = TestClient(app, client=PEER, raise_server_exceptions=False)
    res = client.post("/api/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, res.text
    return client


def _alpha_member(app) -> TestClient:  # noqa: F811
    """alma: a developer in alpha, with no workspace grant of her own."""
    from app.services import users as users_service

    uid = _user("alma", grant=False)
    _member_of("alpha", uid, "developer")
    db = SessionLocal()
    try:
        users_service.find_by_username(db, "alma").expires_at = datetime.now(UTC) + timedelta(7)
        db.commit()
    finally:
        db.close()
    return _client(app, "alma", "sufficient-pass")


@pytest.fixture
def world(app, no_aws):  # noqa: F811
    _project("alpha")
    _project("beta")
    alpha, beta = _seed("alpha"), _seed("beta")
    return {
        "alpha": alpha, "beta": beta, "beta_markers": _markers("beta"),
        "alpha_markers": _markers("alpha"),
        "member": _alpha_member(app),
        "admin": _client(app, ADMIN["username"], ADMIN["password"]),
        "aws": no_aws,
    }


# ── filling a route's parameters from one project's rows ──────────────────────

# path / id-like query parameter → the seeded id it names. Every `{param}` of a
# project-scoped GET route must be here or in ROUTE_PARAMS (test_every_param_is_filled).
PARAMS: dict[str, Any] = {
    "agent_id": lambda i: i["agents"],
    "insight_id": lambda i: f"zz{i['_pid']}insight",
    "candidate_id": lambda i: i["admission_candidates"],
    "task_id": lambda i: i["annotation_tasks"],
    "key_id": lambda i: i["api_keys"],
    "conversation_id": lambda i: i["assistant_conversations"],
    "operation_id": lambda i: i["evaluation_asset_operations"],
    "run_id": lambda i: i["eval_runs"],
    "session_id": lambda i: i["session_id"],
    "lineage_id": lambda i: i["lineage"],
    "cloud_id": lambda i: f"zz{i['_pid']}cloudds",
    "evaluator_id": lambda i: i["evaluator"],
    "config_id": lambda i: f"zz{i['_pid']}onlinecfg",  # the AWS config id the ledger row keeps
    "pipeline_id": lambda i: i["eval_pipelines"],
    "exp_id": lambda i: i["experiments"],
    "dataset_id": lambda i: i["golden"],
    "issue_id": lambda i: i["issues"],
    "job_id": lambda i: i["jobs"],
    "kb_id": lambda i: i["kb"],
    "review_id": lambda i: i["mcp_tool_reviews"],
    "tool_id": lambda i: i["lambda_tool_targets"],
    # workspace infrastructure; a project sees it only through its allowed set
    "storage_id": lambda i: f"zz{i['_pid']}store",
    "memory_id": lambda i: i["memory"],
    "profile_id": lambda i: i["profile"],
    "promotion_id": lambda i: i["promotions"],
    "bundle_id": lambda i: i["release_bundles"],
    "record_id": lambda i: i["release_records"],
    "canary_id": lambda i: i["runtime_canaries"],
    "taskset_id": lambda i: i["skill_lab_tasksets"],
    "connection": lambda i: i["connection"],
    "name": lambda i: i["connection"],
    "kind": lambda i: "oauth2",
    "seq": lambda i: 1,
    "criterion_key": lambda i: "J1",
    # ids that live in AWS or in memory only — any well-formed value
    "upload_id": lambda i: "a" * 32,
    "turn_id": lambda i: "t" * 32,
    "trace_id": lambda i: "1" * 32,
    "gateway_id": lambda i: "gw-shared-0001",
    "generation_id": lambda i: "gen-0001",
    "batch_id": lambda i: "batch-0001",
    "ds_id": lambda i: "DS00000001",
    # Agent IDE repos are private to their owner (never project rows): any
    # well-formed id another member holds reads as missing
    "repo_id": lambda i: "c" * 32,
    # a COA namespace (UUID, digits only so the ghost id stays well-formed) — the
    # workspace's context layer, read through the platform's COA connection
    "namespace_id": lambda i: "00000000-0000-4000-8000-000000000001",
}

# (path, param) → a value where one name means different rows on different routes
ROUTE_PARAMS: dict[tuple[str, str], Any] = {
    ("/api/skill-lab/jobs/{job_id}", "job_id"): lambda i: i["skill_lab_jobs"],
    ("/api/governance/operations/{operation_id}", "operation_id"): lambda i: "f" * 32,
    ("/api/registry/records/{record_id}", "record_id"): lambda i: f"zz{i['_pid']}rec0001",
    ("/api/registry/records/{record_id}/live-agent-card", "record_id"):
        lambda i: f"zz{i['_pid']}rec0001",
}
for _sub in ("artifacts", "artifacts/raw", "diff", "log", "results", "train-summary"):
    ROUTE_PARAMS[(f"/api/skill-lab/jobs/{{job_id}}/{_sub}", "job_id")] = (
        lambda i: i["skill_lab_jobs"])


def _value(path: str, name: str, ids: dict[str, Any]) -> Any:
    fn = ROUTE_PARAMS.get((path, name)) or PARAMS.get(name)
    return fn(ids) if fn else None


def _default(field) -> Any:
    """A valid value for a required non-id query parameter."""
    annotation = field.field_info.annotation
    if get_origin(annotation) is Literal:
        return get_args(annotation)[0]
    args = [a for a in get_args(annotation) if a is not type(None)] or [annotation]
    if get_origin(args[0]) is Literal:
        return get_args(args[0])[0]
    if args[0] is int:
        return 1
    if args[0] is bool:
        return "false"
    return "x"


# required query parameters with a meaning beyond "some id"
QUERY_VALUES: dict[str, Any] = {
    "target_workspace_id": lambda i: WS,
    "path": lambda i: "SKILL.md",
    "a": lambda i: 1,
    "b": lambda i: 1,
    "q": lambda i: "zz",
}


def _url(path: str, route, ids: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    url = PARAM.sub(lambda m: str(_value(path, m.group(1), ids)), path)
    query: dict[str, Any] = {}
    for field in get_flat_dependant(route.dependant).query_params:
        name = field.alias or field.name
        if name in QUERY_VALUES:
            if field.field_info.is_required():
                query[name] = QUERY_VALUES[name](ids)
        elif (value := _value(path, name, ids)) is not None and (
                name.endswith("_id") or field.field_info.is_required()):
            query[name] = value  # id-like filters always point at the other project
        elif field.field_info.is_required():
            query[name] = _default(field)
    return url, query


def _sent(url: str, query: dict[str, Any]) -> set[str]:
    """Values the request itself carried — an echo of them is not a leak."""
    return {s for s in re.split(r"[/?&=]", url) if s} | {str(v) for v in query.values()}


# ── the enumeration ───────────────────────────────────────────────────────────


def test_every_param_is_filled(app):  # noqa: F811
    """A new `{param}` the fixture cannot fill would let its route bypass the scan."""
    missing = sorted(
        f"{path} {{{name}}}"
        for path in _scoped_gets(app)
        for name in PARAM.findall(path)
        if (path, name) not in ROUTE_PARAMS and name not in PARAMS
    )
    assert not missing, "teach test_project_isolation.PARAMS these: " + ", ".join(missing)
    live = _scoped_gets(app)
    stale = sorted(p for p in (*BROKEN, *ADMIN_SEES_ALL) if p not in live)
    assert not stale, f"the skip lists name routes that no longer exist: {stale}"


def _ghost(value: Any) -> Any:
    """A never-seeded id of the same shape (character class by character class)."""
    if not isinstance(value, str):
        return value
    out = []
    for ch in value:
        if ch.isdigit():
            out.append(str((int(ch) + 3) % 10))
        elif ch.islower():
            out.append(chr((ord(ch) - 97 + 7) % 26 + 97))
        elif ch.isupper():
            out.append(chr((ord(ch) - 65 + 7) % 26 + 65))
        else:
            out.append(ch)
    return "".join(out)


def _code(res) -> str | None:
    try:
        body = res.json()
    except ValueError:
        return None
    return body.get("code") if isinstance(body, dict) else None


# Routes on which an administrator reads every project's rows by design.
ADMIN_SEES_ALL = {
    "/api/network-profiles": "an admin manages every profile of the workspace (§12.3)",
    "/api/network-profiles/{profile_id}": "an admin manages every profile of the workspace",
}

# Routes that cannot be exercised today, with why (see the report of P1H-iso).
BROKEN: dict[str, str] = {}


def _scan(world, client: TestClient, headers: dict, *, admin: bool = False) -> list[str]:
    """Call every scoped GET with beta's ids (and with ghost ids of the same shape);
    return what leaks."""
    beta = {**world["beta"], "_pid": "beta"}
    ghost = {k: _ghost(v) for k, v in beta.items()}
    markers = world["beta_markers"]
    problems = []
    for path, route in sorted(_scoped_gets(client.app).items()):
        if path in BROKEN or (admin and path in ADMIN_SEES_ALL):
            continue
        url, query = _url(path, route, beta)
        world["aws"].clear()
        res = client.get(url, params=query, headers=headers)
        leaked = sorted(m for m in markers - _sent(url, query) if m in res.text)
        if leaked:
            problems.append(f"LEAK {res.status_code} GET {path}: {leaked[:3]}")
        # the boundary must refuse before AWS is asked about another project's resource
        reached = sorted({m for m in markers for call in world["aws"] if m in call})
        if reached:
            problems.append(f"AWS-REACH GET {path}: {reached[:3]}")
        if res.status_code == 422:
            problems.append(f"UNFILLED GET {path}: {res.text[:200]}")
            continue
        if not PARAM.search(path) and not any(k.endswith("_id") for k in query):
            continue
        # another project's id answers 404, or exactly like an id that never existed
        # (an unknown id may reach AWS, which the stub refuses: 403 / 502 / 503)
        g_url, g_query = _url(path, route, ghost)
        missing = client.get(g_url, params=g_query, headers=headers)
        if res.status_code != 404 and (
                (res.status_code, _code(res)) != (missing.status_code, _code(missing))):
            problems.append(
                f"ORACLE GET {path}: beta id → {res.status_code} {_code(res)}, "
                f"unknown id → {missing.status_code} {_code(missing)}"
            )
    return problems


def test_a_member_of_alpha_reaches_nothing_of_beta(world):
    problems = _scan(world, world["member"], ALPHA)
    assert not problems, "\n".join(problems)


def test_an_admin_inside_alpha_reaches_nothing_of_beta(world):
    problems = _scan(world, world["admin"], ALPHA, admin=True)
    assert not problems, "\n".join(problems)



def test_the_scan_finds_leaks_when_the_boundary_is_off(world, monkeypatch):
    """Self-check: with the request's project never published to the ORM, the same
    scan reports beta's rows — so the passes above come from the boundary."""
    import app.core.project_scope as project_scope

    monkeypatch.setattr(project_scope, "set_current_project", lambda _pid: None)
    problems = _scan(world, world["member"], ALPHA)
    leaks = {p.split(" GET ", 1)[1].split(":", 1)[0] for p in problems if p.startswith("LEAK")}
    assert {"/api/agents", "/api/issues", "/api/eval/datasets", "/api/apikeys"} <= leaks
    assert len(leaks) >= 20, sorted(leaks)


def test_every_project_table_is_seeded(world):
    """A new project-owned table gets beta rows without anyone editing this file; this
    fails only if the generic seeder cannot build one (teach `_overrides`)."""
    db = SessionLocal()
    try:
        with project_context(None):
            unseeded = []
            for model in _project_models():
                rows = db.scalars(select(model).where(model.workspace_id == WS)).all()
                in_beta = [r for r in rows if r.project_id == "beta"]
                platform = [r for r in rows if r.project_id is None
                            and isinstance(r, ProjectShared) and r.project_platform_row()]
                if not in_beta and not platform:
                    unseeded.append(model.__tablename__)
    finally:
        db.close()
    assert not unseeded, unseeded


def test_another_projects_header_is_not_found(world):
    """`X-Project: beta` from a member of alpha only: 404 project.not_found, never 403."""
    member = world["member"]
    alpha = {**world["alpha"], "_pid": "alpha"}
    wrong = []
    for path, route in sorted(_scoped_gets(member.app).items()):
        url, query = _url(path, route, alpha)
        res = member.get(url, params=query, headers=BETA)
        expected = (404, "project.not_found")
        if route_policy.required_role("GET", path) == route_policy.ADMIN:
            expected = (403, "auth.forbidden")  # the role is checked before the project
        if (res.status_code, _code(res)) != expected:
            wrong.append(f"{path}: {res.status_code} {_code(res)}")
    assert not wrong, "\n".join(wrong)


# by-id reads a member of alpha gets for alpha's own rows (the 404s above are not
# "nothing is readable"), and the list routes an administrator inside beta sees beta on
ALPHA_READS = [
    "/api/agents/{agent_id}", "/api/admission/{candidate_id}",
    "/api/annotation-tasks/{task_id}", "/api/criteria-sets/{lineage_id}",
    "/api/eval/pipelines/{pipeline_id}", "/api/eval/runs/{run_id}",
    "/api/experiments/{exp_id}", "/api/golden-sets/{dataset_id}", "/api/issues/{issue_id}",
    "/api/jobs/{job_id}", "/api/network-profiles/{profile_id}",
    "/api/promotions/{promotion_id}", "/api/release-bundles/{bundle_id}",
    "/api/release-records/{record_id}", "/api/runtime-canaries/{canary_id}",
    "/api/skill-lab/jobs/{job_id}", "/api/skill-lab/tasksets/{taskset_id}",
]
BETA_LISTS = [
    "/api/admission", "/api/agents", "/api/alerts", "/api/annotation-tasks", "/api/apikeys",
    "/api/criteria-sets", "/api/eval/datasets", "/api/eval/pipelines", "/api/experiments",
    "/api/feedback", "/api/golden-sets", "/api/issues", "/api/network-profiles",
    "/api/promotions", "/api/release-records", "/api/resource-mappings",
    "/api/runtime-canaries", "/api/skill-lab/jobs", "/api/skill-lab/tasksets",
]


def test_each_project_reads_its_own_rows(world):
    routes = _scoped_gets(world["member"].app)
    alpha = {**world["alpha"], "_pid": "alpha"}
    beta = {**world["beta"], "_pid": "beta"}
    wrong = []
    for path in ALPHA_READS:
        url, query = _url(path, routes[path], alpha)
        res = world["member"].get(url, params=query, headers=ALPHA)
        if res.status_code != 200 or not any(m in res.text for m in world["alpha_markers"]):
            wrong.append(f"alpha {path}: {res.status_code} {res.text[:120]}")
    for path in BETA_LISTS:
        url, query = _url(path, routes[path], beta)
        res = world["admin"].get(url, params=query, headers=BETA)
        if res.status_code != 200 or not any(m in res.text for m in world["beta_markers"]):
            wrong.append(f"beta {path}: {res.status_code} {res.text[:120]}")
    assert not wrong, "\n".join(wrong)


# ── /v1 with alpha's API key ──────────────────────────────────────────────────


def test_an_alpha_key_cannot_reach_beta_agents(world, monkeypatch):
    api_keys_service.reset_rate_limits()
    monkeypatch.setattr(public_api, "invoke_agent_text", lambda agent, prompt, **_kw: {
        "text": f"hi from {agent.name}", "session_id": "s" * 40})

    def stream(agent, prompt, **_kw):
        yield {"event": "done", "data": {"text": agent.name}}

    monkeypatch.setattr(public_api, "chat_stream", stream)
    minted = world["member"].post("/api/apikeys", json={"name": "k"}, headers=ALPHA)
    assert minted.status_code == 201, minted.text
    v1 = TestClient(world["member"].app, client=PEER, raise_server_exceptions=False)
    key = {"X-Api-Key": minted.json()["key"]}

    listed = v1.get("/v1/agents", headers=key)
    assert listed.status_code == 200, listed.text
    assert {a["id"] for a in listed.json()["agents"]} == {world["alpha"]["agents"]}
    assert not [m for m in world["beta_markers"] if m in listed.text]
    for suffix in ("invoke", "invoke-stream"):
        res = v1.post(f"/v1/agents/{world['beta']['agents']}/{suffix}", headers=key,
                      json={"prompt": "hi"})
        assert res.status_code == 404, (suffix, res.text)
        assert not [m for m in world["beta_markers"] - {world["beta"]["agents"]}
                    if m in res.text]
        ok = v1.post(f"/v1/agents/{world['alpha']['agents']}/{suffix}", headers=key,
                     json={"prompt": "hi"})
        assert ok.status_code == 200, (suffix, ok.text)
    # the key itself never lists or meters in beta
    beta_keys = world["admin"].get("/api/apikeys", headers=BETA).json()["keys"]
    assert minted.json()["id"] not in {k["id"] for k in beta_keys}
    api_keys_service.reset_rate_limits()


# ── /share with alpha's links ─────────────────────────────────────────────────


def _chat_link(agent_id: str, kind: str = share_links.KIND_CHAT) -> tuple[str, str]:
    db = SessionLocal()
    try:
        with project_context(None):
            link, raw = share_links.create_link(
                db, workspace_id=WS, agent=db.get(Agent, agent_id), label="l",
                created_by="river", expires_in_days=None, kind=kind,
            )
            return link.id, raw
    finally:
        db.close()


def _annotation_link(pid: str) -> str:
    from app.dlc import calibration as cal
    from app.models.dlc import AnnotationTask
    from app.services import annotation_links

    db = SessionLocal()
    try:
        with project_context(pid):
            task = cal.create_task(
                db, workspace_id=WS, agent_id=world_agent(db, pid), criteria_set_id=None,
                criterion_key="J1", purpose="judge_calibration",
                items=[{"ref": "i0", "input": "q", "answer": "a", "judge_label": "pass"}],
                annotators=["alice", "bob"], adjudicator="alice", actor="root", run_id=None,
                dataset_id=None,
            )
            db.commit()
        with project_context(None):
            _link, raw = annotation_links.create(
                db, task=db.get(AnnotationTask, task.id), workspace=None, label="Wang",
                created_by="root", expires_in_days=None,
            )
            db.commit()
            return raw
    finally:
        db.close()


def world_agent(db, pid: str) -> str:
    return db.scalar(select(Agent.id).where(Agent.name == f"zz{pid}-agent"))


@pytest.fixture
def links(world):
    for limiter in (share_links.limiter, review_links.review_limiter):
        limiter.reset()
    yield
    for limiter in (share_links.limiter, review_links.review_limiter):
        limiter.reset()


def test_alpha_links_show_nothing_of_beta(world, links):
    alpha_agent = world["alpha"]["agents"]
    _chat_id, chat = _chat_link(alpha_agent)
    _review_id, review = _chat_link(alpha_agent, review_links.KIND_REVIEW)
    annotate = _annotation_link("alpha")
    public = TestClient(world["member"].app, raise_server_exceptions=False)
    for path in (f"/share/{chat}", f"/share/review/{review}", f"/share/annotate/{annotate}"):
        res = public.get(path)
        assert res.status_code == 200, (path, res.text)
        assert not [m for m in world["beta_markers"] if m in res.text], path
    # an alpha link cannot rate beta's answer
    beta_message = _beta_message_id()
    res = public.post(f"/share/{chat}/feedback", json={
        "session_id": world["beta"]["session_id"], "message_id": beta_message,
        "verdict": "up"})
    assert res.status_code == 404, res.text
    from app.models.ledger import ChatFeedback

    db = SessionLocal()
    try:
        with project_context(None):
            assert not db.scalars(select(ChatFeedback).where(
                ChatFeedback.message_id == beta_message, ChatFeedback.verdict == "up")).all()
    finally:
        db.close()


def _beta_message_id() -> int:
    from app.models.ledger import ChatMessage

    db = SessionLocal()
    try:
        with project_context(None):
            return db.scalar(select(ChatMessage.id).where(ChatMessage.project_id == "beta"))
    finally:
        db.close()


def test_a_beta_link_never_opens_an_alpha_object(world, links):
    """A link row filed in beta but aimed at alpha's agent (a forged or stale row) is
    the same opaque 404 as an unknown token, on every public route."""
    for kind, prefix in ((share_links.KIND_CHAT, "/share/"),
                         (review_links.KIND_REVIEW, "/share/review/")):
        link_id, raw = _chat_link(world["alpha"]["agents"], kind)
        db = SessionLocal()
        try:
            with project_context(None):
                db.get(ShareLink, link_id).project_id = "beta"
                db.commit()
        finally:
            db.close()
        public = TestClient(world["member"].app, raise_server_exceptions=False)
        res = public.get(f"{prefix}{raw}")
        assert res.status_code == 404 and _code(res) == "share.not_found", res.text
        if kind == share_links.KIND_CHAT:
            res = public.post(f"/share/{raw}/chat", json={"prompt": "hi"})
            assert res.status_code == 404 and _code(res) == "share.not_found", res.text



# Read by every project of the workspace by design (platform rows, design §4.2).
WORKSPACE_LOGS = {"/api/governance/decisions": "the shared gateway's one decision log"}
# timestamps, ages and what a background scheduler tick writes meanwhile (a slow run
# lets the watch / alert sweep run between two snapshots) are not data of beta's
_VOLATILE = re.compile(
    r"\d{4}-\d\d-\d\dT[\d:.+]+Z?|\"(generated_at|checked_at|now|oldest_open_hours"
    r"|hours_to_close|last_checked_at|next_due_at|last_status|last_detail)\":"
    r"(\"(?:[^\"\\\\]|\\\\.)*\"|[^,}]+)"
)


def test_alpha_reads_the_same_with_or_without_beta(app, no_aws):  # noqa: F811
    """Counts, aggregates and lists carry no id to scan for: alpha's answers must not
    change at all when beta's rows appear (timestamps aside)."""
    _project("alpha")
    _project("beta")
    alpha = {**_seed("alpha"), "_pid": "alpha"}
    member = _alpha_member(app)
    routes = _scoped_gets(app)

    def snapshot() -> dict[str, tuple[int, str]]:
        out = {}
        for path, route in sorted(routes.items()):
            url, query = _url(path, route, alpha)
            res = member.get(url, params=query, headers=ALPHA)
            out[path] = (res.status_code, _VOLATILE.sub("T", res.text))
        return out

    before, again = snapshot(), snapshot()
    stable = {p for p in before if before[p] == again[p] and p not in WORKSPACE_LOGS}
    assert len(stable) >= 100, sorted(set(before) - stable)  # most answers are stable
    _seed("beta")
    after = snapshot()
    changed = sorted(p for p in stable if after[p] != before[p])
    assert not changed, "\n".join(f"{p}:\n  {before[p]}\n  {after[p]}" for p in changed)
