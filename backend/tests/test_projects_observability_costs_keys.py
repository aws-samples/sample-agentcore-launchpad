"""Projects phase 1 (design §5.5, §5.9, §5.13, §6.6) for observability, overview, costs,
alerts and the fleet: account-wide telemetry is narrowed by an allow-set of the current
project's agents, caches never answer one project with another's entry, AWS-only ids of
another project read as missing, and a single-project workspace behaves as before."""

from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import NotFoundError
from app.core.project_scope import current_project, project_context
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, AlertRule, ChatSession, ResourceCreator, SharedTemplate
from app.routers import alerts as alerts_router
from app.routers import fleet as fleet_router
from app.routers import observability as obs_router
from app.services import alerts as alert_service
from app.services import costs as cost_service
from app.services import fleet as fleet_service
from app.services import observability, visibility
from app.services.observability import ProjectAgents
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext
from tests.conftest import TEST_PROJECT
from tests.test_project_scope import _login, _member_of, _project, _user, app  # noqa: F401

WS = DEFAULT_WORKSPACE_ID
PRICES = {"claude-sonnet": {"input": 3.0, "output": 15.0}}


@pytest.fixture
def ctx():
    return WorkspaceContext(account_id="111122223333", region="us-west-2", resources={},
                            id=WS)


def _runtime(name: str, pid: str | None, *, system_key: str | None = None) -> str:
    """An agent whose runtime `<name>_rt-<suffix>` emits spans as `<name>_rt.DEFAULT`."""
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=WS, name=name, method="zip_runtime", status="active",
                        spec={"name": name}, system_key=system_key,
                        resource_id=f"{name}_rt-AbC123", registry_record_id=f"rec-{name}")
            db.add(row)
            db.commit()
            return row.id
    finally:
        db.close()


def _chat(agent_id: str, session_id: str, actor: str = "mei") -> None:
    db = SessionLocal()
    try:
        with project_context(None):
            db.add(ChatSession(workspace_id=WS, agent_id=agent_id, session_id=session_id,
                               actor_id=actor))
            db.commit()
    finally:
        db.close()


def _allow(pid: str | None, *, bypass: bool = False) -> ProjectAgents:
    return ProjectAgents(SessionLocal(), WS, pid, bypass=bypass)


# ── the allow-set (§5.9) ──────────────────────────────────────────────────────


def test_a_single_project_workspace_keeps_every_row():
    _runtime("solo", TEST_PROJECT)
    rows = [{"service": "solo_rt.DEFAULT", "session_id": None},
            {"service": "nobody.DEFAULT", "session_id": "s" * 40}]
    allow = _allow(TEST_PROJECT)
    assert not allow
    assert allow.keep_rows(rows) == rows
    assert allow.service_filter() == ""
    assert allow.owns_session("x" * 40)


def test_rows_are_kept_by_project_preset_or_admin_only():
    _project("alpha")
    mine = _runtime("mine", "alpha")
    theirs = _runtime("theirs", TEST_PROJECT)
    _runtime("preset", None, system_key="aws-agent-solution-architect")
    _chat(theirs, "t" * 40)
    _chat(mine, "m" * 40)
    rows = [
        {"service": "mine_rt.DEFAULT", "session_id": None},
        {"service": "theirs_rt.DEFAULT", "session_id": None},
        {"service": "preset_rt.DEFAULT", "session_id": None},
        {"service": "unknown.DEFAULT", "session_id": None},
        # the ledger session decides over the runtime
        {"service": "mine_rt.DEFAULT", "session_id": "t" * 40},
        {"service": "unknown.DEFAULT", "session_id": "m" * 40},
    ]
    kept = [r["service"] + str(r["session_id"]) for r in _allow("alpha").keep_rows(rows)]
    assert kept == ["mine_rt.DEFAULTNone", "preset_rt.DEFAULTNone", "unknown.DEFAULT" + "m" * 40]
    admin = _allow("alpha", bypass=True).keep_rows(rows)
    assert {"service": "unknown.DEFAULT", "session_id": None} in admin
    assert {"service": "theirs_rt.DEFAULT", "session_id": None} not in admin


def test_session_ownership_reads_every_ledger_source():
    _project("alpha")
    theirs = _runtime("theirs", TEST_PROJECT)
    _chat(theirs, "c" * 40)
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            db.add(EvalRun(workspace_id=WS, agent_id=theirs, agent_name="theirs",
                           mode="dataset", status="completed", session_ids=["e" * 40]))
            db.commit()
    finally:
        db.close()
    allow = _allow("alpha")
    assert allow.session_verdict("c" * 40) is False
    assert allow.session_verdict("e" * 40) is False
    assert allow.session_verdict("u" * 40) is None
    assert _allow(TEST_PROJECT).session_verdict("e" * 40) is True


def test_the_dashboard_filter_names_the_projects_runtimes():
    _project("alpha")
    assert _allow("alpha").service_filter() is None  # no runtime: nothing to count
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    line = _allow("alpha").service_filter()
    assert "mine_rt" in line and "theirs_rt" not in line
    assert line.startswith("\n| filter resource.attributes.service.name like /")


def test_the_dashboard_is_narrowed_and_cached_per_project(monkeypatch, ctx):
    _project("alpha")
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    observability.reset_cache()
    seen: list[str] = []

    def fake_run(queries, hours, **kw):
        seen.append(queries["totals"])
        return {key: [] for key in queries}

    monkeypatch.setattr(observability, "run_insights_queries", fake_run)
    monkeypatch.setattr(observability, "query_token_usage_metrics", lambda *a, **k: [])
    observability.get_dashboard("24h", ctx)  # no project: the whole account
    with project_context("alpha"):
        observability.get_dashboard("24h", ctx)
        observability.get_dashboard("24h", ctx)  # cached per project
    with project_context(TEST_PROJECT):
        observability.get_dashboard("24h", ctx)
    assert len(seen) == 3
    assert "service.name like" not in seen[0]
    assert "mine_rt" in seen[1] and "theirs_rt" not in seen[1]
    assert "theirs_rt" in seen[2] and "mine_rt" not in seen[2]
    observability.reset_cache()


def test_view_caches_carry_the_project(monkeypatch, ctx):
    observability.reset_cache()
    calls: list[str | None] = []

    def fake_run(queries, hours, **kw):
        calls.append(current_project())
        return {"aggregates": [], "roots": []}

    monkeypatch.setattr(observability, "run_insights_queries", fake_run)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            observability.list_traces("24h", db, ctx)
        with project_context(TEST_PROJECT):
            observability.list_traces("24h", db, ctx)
            observability.list_traces("24h", db, ctx)
    finally:
        db.close()
    assert calls == ["alpha", TEST_PROJECT]
    observability.reset_cache()


def test_the_resolver_can_attribute_across_projects():
    _project("alpha")
    _runtime("theirs", TEST_PROJECT)
    db = SessionLocal()
    try:
        with project_context("alpha"):
            assert observability.build_agent_resolver(db, WS)("theirs_rt.DEFAULT") is None
            found = observability.build_agent_resolver(db, WS, all_projects=True)(
                "theirs_rt.DEFAULT")
            assert found is not None and found.project_id == TEST_PROJECT
    finally:
        db.close()


# ── the API: another project's sessions and traces read as missing ────────────


def _alpha_member(app):  # noqa: F811
    _project("alpha")
    uid = _user("ana", grant=False)
    _member_of("alpha", uid)
    return _login(app, "ana")


def test_a_member_never_sees_another_projects_sessions(app, monkeypatch):  # noqa: F811
    mine = _runtime("mine", "alpha")
    theirs = _runtime("theirs", TEST_PROJECT)
    _chat(theirs, "t" * 40)
    _chat(mine, "m" * 40)
    ana = _alpha_member(app)
    listed = {"range": "24h", "count": 3, "limit": 500, "sessions": [
        {"session_id": "t" * 40, "service": "theirs_rt.DEFAULT"},
        {"session_id": "m" * 40, "service": "mine_rt.DEFAULT"},
        {"session_id": "u" * 40, "service": "nobody.DEFAULT"},
    ]}
    monkeypatch.setattr(obs_router.observability, "list_sessions", lambda *a, **k: listed)
    res = ana.get("/api/observability/sessions")
    assert res.status_code == 200, res.text
    assert [r["session_id"] for r in res.json()["sessions"]] == ["m" * 40]

    def no_query(*a, **k):
        raise AssertionError("another project's session must 404 before any query")

    monkeypatch.setattr(obs_router.observability, "get_session", no_query)
    monkeypatch.setattr(obs_router.observability, "get_session_transcript", no_query)
    for path in (f"/api/observability/sessions/{'t' * 40}",
                 f"/api/observability/sessions/{'t' * 40}/transcript",
                 # unattributed, no agent of ours named: never probe memory
                 f"/api/observability/sessions/{'u' * 40}/transcript",
                 f"/api/observability/sessions/{'u' * 40}/transcript?agent_id={theirs}"):
        res = ana.get(path)
        assert res.status_code == 404 and res.json()["code"] == "observability.session_not_found"
    monkeypatch.setattr(obs_router.observability, "get_session_transcript",
                        lambda sid, db, ws, agent_id=None, **_kw: {"session_id": sid,
                                                                   "transcript": {}})
    ok = ana.get(f"/api/observability/sessions/{'u' * 40}/transcript?agent_id={mine}")
    assert ok.status_code == 200, ok.text
    res = ana.post(f"/api/observability/sessions/{'t' * 40}/evaluate",
                   json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert res.status_code == 404


def test_an_unattributed_session_detail_is_decided_by_its_spans(app, monkeypatch):  # noqa: F811
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    ana = _alpha_member(app)

    def session(service):
        return lambda sid, *a, **k: {"session_id": sid, "traces": [{"service": service}],
                                     "transcript": {}}

    monkeypatch.setattr(obs_router.observability, "get_session", session("theirs_rt.DEFAULT"))
    assert ana.get(f"/api/observability/sessions/{'v' * 40}").status_code == 404
    monkeypatch.setattr(obs_router.observability, "get_session", session("mine_rt.DEFAULT"))
    assert ana.get(f"/api/observability/sessions/{'v' * 40}").status_code == 200


def test_another_projects_custom_evaluator_reads_as_missing():
    _project("alpha")
    db = SessionLocal()
    try:
        with project_context(TEST_PROJECT):
            db.add(ResourceCreator(workspace_id=WS, kind="evaluator", resource_id="theirs-ev"))
            db.commit()
        with project_context("alpha"):
            with pytest.raises(ClientError):
                obs_router._refuse_foreign_evaluators(db, WS, ["theirs-ev"])
            # AWS-managed evaluators belong to every project
            obs_router._refuse_foreign_evaluators(db, WS, ["Builtin.Helpfulness"])
        with project_context(TEST_PROJECT):
            obs_router._refuse_foreign_evaluators(db, WS, ["theirs-ev"])
    finally:
        db.close()


# ── visibility: the project layer of AWS-only ids (§5.5, §5.13) ───────────────


def test_hidden_aws_ids_hide_another_projects_ids_for_every_viewer():
    _project("alpha")
    db = SessionLocal()
    try:
        with project_context("alpha"):
            db.add(ResourceCreator(workspace_id=WS, kind="knowledge_base", resource_id="kb-a"))
            db.commit()
        ids = ["kb-a", "kb-legacy", "Builtin.X"]
        with project_context("alpha"):
            # no row = the workspace default project's
            assert visibility.hidden_aws_ids(Viewer.ALL, db, WS, "knowledge_base", ids) == {
                "kb-legacy"}
        with project_context(TEST_PROJECT):
            assert visibility.hidden_aws_ids(Viewer.ALL, db, WS, "knowledge_base", ids) == {
                "kb-a"}
        assert visibility.hidden_aws_ids(Viewer.ALL, db, WS, "knowledge_base", ids) == set()
    finally:
        db.close()


def test_the_viewer_cache_key_carries_the_project():
    assert Viewer(user_id=None, filters=False).principal_key() == "all"
    assert Viewer(user_id=None, filters=False, project_id="a").principal_key() == "p:a|all"
    assert Viewer(user_id="u", filters=True, project_id="b").principal_key() == "p:b|u:u"


# ── costs: workspace-wide cache, narrowed by ?project= (§5.9) ─────────────────


_SERVICE_ROWS = [
    {"service": "mine_rt.DEFAULT", "tokens_in": "1000000", "tokens_out": "0",
     "model": "claude-sonnet"},
    {"service": "theirs_rt.DEFAULT", "tokens_in": "0", "tokens_out": "1000000",
     "model": "claude-sonnet"},
    {"service": "ghost.DEFAULT", "tokens_in": "1000000", "tokens_out": "0",
     "model": "claude-sonnet"},
]


def _stub_costs(monkeypatch):
    monkeypatch.setattr(cost_service, "logs_client", lambda workspace: object())
    monkeypatch.setattr(cost_service, "run_insights_queries", lambda q, hours, logs=None, **k: {
        "by_service": _SERVICE_ROWS,
        # the per-model query groups by service too: the same rows serve it
        "by_model": _SERVICE_ROWS,
        "by_session": [
            {"session_id": "m" * 40, "tokens_in": "1000000", "model": "claude-sonnet"},
            {"session_id": "t" * 40, "tokens_out": "1000000", "model": "claude-sonnet"},
            {"session_id": "o" * 40, "tokens_in": "1000000", "model": "claude-sonnet"},
        ],
    })
    monkeypatch.setattr(cost_service, "get_settings",
                        lambda: SimpleNamespace(model_prices=PRICES))
    monkeypatch.setattr(cost_service, "cached", lambda key, force, build: build())


def test_costs_narrow_to_one_project(monkeypatch, ctx):
    _project("alpha")
    mine = _runtime("mine", "alpha")
    theirs = _runtime("theirs", TEST_PROJECT)
    _chat(mine, "m" * 40, actor="ana")
    _chat(theirs, "t" * 40, actor="bo")
    _stub_costs(monkeypatch)
    db = SessionLocal()
    try:
        with project_context("alpha"):  # whatever is current, the build is workspace-wide
            whole = cost_service.cost_report(db, ctx)
        alpha = cost_service.cost_report(db, ctx, project="alpha")
    finally:
        db.close()
    assert whole["total_est_cost_usd"] == pytest.approx(3 + 15 + 3)
    assert {r["actor"] for r in whole["by_actor"]} == {"ana", "bo", "—"}
    assert [r["agent_id"] for r in alpha["by_agent"]] == [mine]
    assert alpha["total_est_cost_usd"] == pytest.approx(3.0)
    assert [r["actor"] for r in alpha["by_actor"]] == ["ana"]  # no unattributed bucket


def test_month_to_date_per_project(monkeypatch, ctx):
    _project("alpha")
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    _stub_costs(monkeypatch)
    db = SessionLocal()
    try:
        assert cost_service.month_to_date_usd(db, ctx) == pytest.approx(21.0)
        assert cost_service.month_to_date_usd(db, ctx, project="alpha") == pytest.approx(3.0)
        assert cost_service.month_to_date_usd(db, ctx, project=TEST_PROJECT) == pytest.approx(15.0)
    finally:
        db.close()


def test_costs_refuse_a_project_not_bound_here():
    db = SessionLocal()
    try:
        with pytest.raises(NotFoundError) as err:
            alerts_router._bound_project(db, SimpleNamespace(id=WS), "nope")
        assert err.value.code == "project.not_found"
        bound = alerts_router._bound_project(db, SimpleNamespace(id=WS), TEST_PROJECT)
        assert bound == TEST_PROJECT
    finally:
        db.close()


# ── alerts: workspace or project rules, each measured as its own project ──────


def _create(req: alerts_router.RuleCreate) -> dict:
    db = SessionLocal()
    try:
        return alerts_router.create_alert(req, db, SimpleNamespace(id=WS),
                                          SimpleNamespace(username="operator"))
    finally:
        db.close()


def test_a_rule_is_workspace_level_unless_scoped_to_a_project():
    _project("alpha")
    with project_context("alpha"):
        workspace_rule = _create(alerts_router.RuleCreate(kind="error_rate", threshold=0.1))
        project_rule = _create(alerts_router.RuleCreate(kind="error_rate", threshold=0.1,
                                                        scope="project"))
        other = _create(alerts_router.RuleCreate(kind="error_rate", threshold=0.1,
                                                 scope="project", project_id=TEST_PROJECT))
    assert (workspace_rule["scope"], workspace_rule["project_id"]) == ("workspace", None)
    assert (project_rule["scope"], project_rule["project_id"]) == ("project", "alpha")
    assert other["project_id"] == TEST_PROJECT
    db = SessionLocal()
    try:
        with project_context(None):
            stored = dict(db.execute(select(AlertRule.id, AlertRule.project_id)).all())
        assert stored[workspace_rule["id"]] is None
    finally:
        db.close()


def test_a_rule_reads_its_value_as_its_own_project(monkeypatch, ctx):
    seen: list[str | None] = []
    monkeypatch.setattr(alert_service, "_dashboard_value",
                        lambda kind, window, db, ws: (seen.append(current_project()), "x"))
    with project_context("alpha"):  # a workspace rule ignores the caller's project
        alert_service.read_value(AlertRule(kind="error_rate", window="24h"), None, ctx)
    alert_service.read_value(
        AlertRule(kind="error_rate", window="24h", project_id="alpha"), None, ctx)
    assert seen == [None, "alpha"]


# ── fleet: per-project counts, and the marketplace's project boundary (§6.6) ──


def test_the_fleet_counts_per_project_and_filters():
    _project("alpha")
    _runtime("mine", "alpha")
    _runtime("theirs", TEST_PROJECT)
    _runtime("theirs2", TEST_PROJECT)
    db = SessionLocal()
    try:
        with project_context("alpha"):  # unscoped whatever the caller's console is in
            fleet = fleet_service.fleet_overview(db)
        row = next(r for r in fleet["workspaces"] if r["id"] == WS)
        assert row["agents_active"] == 3
        per = {p["id"]: p["agents_active"] for p in row["projects"]}
        assert per == {TEST_PROJECT: 2, "alpha": 1}
        narrowed = fleet_service.fleet_overview(db, project="alpha")
        row = next(r for r in narrowed["workspaces"] if r["id"] == WS)
        assert row["agents_active"] == 1 and [p["id"] for p in row["projects"]] == ["alpha"]
    finally:
        db.close()


def test_only_the_publishing_project_withdraws_a_template():
    _project("alpha")
    theirs = _runtime("theirs", TEST_PROJECT)
    db = SessionLocal()
    try:
        row = SharedTemplate(source_workspace_id=WS, agent_id=theirs, title="t")
        db.add(row)
        db.commit()
        member = SimpleNamespace(is_admin=False, username="ana")
        with project_context("alpha"):
            with pytest.raises(Exception) as err:
                fleet_router.unpublish_template(row.id, db, SimpleNamespace(id=WS), member)
            assert getattr(err.value, "code", None) == "marketplace.not_yours"
        with project_context(TEST_PROJECT):
            assert fleet_router.unpublish_template(
                row.id, db, SimpleNamespace(id=WS), member) == {"deleted": row.id}
    finally:
        db.close()
