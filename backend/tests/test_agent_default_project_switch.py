"""The agent-project switch (services/agent_project_settings): off by default — the
rule stays exactly as before — and, when an administrator turns it on, a workspace
`default` project becomes a valid home for a new agent on every path: the row rule,
the picker's eligible list, the ORM stamp guard and POST /api/agents. Console beats
env; only an administrator reads or flips it, and each flip is journaled."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

import app.routers.agents as agents_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.core.project_scope import ProjectStampError, project_context
from app.main import create_app
from app.models.ledger import Agent, AuditEvent
from app.models.platform_settings import PlatformSetting
from app.models.projects import ProjectMember
from app.services import access, agent_project, agent_project_settings
from tests.test_project_scope import ADMIN, _login, _project, _user

ENV = "LAUNCHPAD_AGENT_ALLOW_DEFAULT_PROJECT"


@pytest.fixture(autouse=True)
def switch_off(monkeypatch):
    """Every test starts from env-less settings and leaves the cached object clean."""
    monkeypatch.delenv(ENV, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _on(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(get_settings(), "agent_allow_default_project", value)


def _check(pid: str) -> str:
    db = SessionLocal()
    try:
        return agent_project.check_agent_project(db, pid, DEFAULT_WORKSPACE_ID)
    finally:
        db.close()


class _Admin:
    user_id, role, username = None, "admin", "operator"


def _eligible(identity, permission: str = "agents.deploy") -> list[str]:
    db = SessionLocal()
    try:
        return [p["id"] for p in agent_project.eligible_projects(
            db, identity, DEFAULT_WORKSPACE_ID, permission)]
    finally:
        db.close()


def _stamp_agent(pid: str) -> str:
    db = SessionLocal()
    try:
        with project_context(pid):
            row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="stamped", method="harness",
                        status="deploying", spec={"name": "stamped"})
            db.add(row)
            db.commit()
            return row.project_id
    finally:
        db.close()


# ── off (the default): nothing changes ────────────────────────────────────────


def test_the_switch_is_off_by_default():
    assert get_settings().agent_allow_default_project is False
    assert agent_project_settings.allowed() is False


def test_off_the_default_project_is_refused_hidden_and_never_stamped():
    _project("default")
    with pytest.raises(AppError) as err:
        _check("default")
    assert err.value.code == agent_project.PROJECT_DEFAULT
    assert "default" not in _eligible(_Admin())
    with pytest.raises(ProjectStampError):
        _stamp_agent("default")


# ── on: the default project is a valid home ───────────────────────────────────


def test_on_the_default_project_passes_the_row_rule_without_a_department(monkeypatch):
    _project("default")
    _on(monkeypatch)
    assert _check("default") == "default"
    # every other project keeps the full rule
    _project("nodept", department_id=None)
    with pytest.raises(AppError) as err:
        _check("nodept")
    assert err.value.code == agent_project.PROJECT_NO_DEPARTMENT


def test_on_an_archived_default_project_is_still_refused(monkeypatch):
    _project("default")
    _on(monkeypatch)
    db = SessionLocal()
    db.execute(text("UPDATE projects SET status = 'archived' WHERE id = 'default'"))
    db.commit()
    db.close()
    with pytest.raises(AppError) as err:
        _check("default")
    assert err.value.code == agent_project.PROJECT_ARCHIVED


def test_on_an_unbound_default_project_is_not_found_and_not_listed(monkeypatch):
    # conftest leaves `default` unbound from the `default` workspace
    _on(monkeypatch)
    with pytest.raises(AppError) as err:
        _check("default")
    assert err.value.code == agent_project.PROJECT_NOT_FOUND
    assert "default" not in _eligible(_Admin())


def test_on_the_eligible_list_carries_the_default_project_for_an_admin(monkeypatch):
    _project("default")
    _on(monkeypatch)
    db = SessionLocal()
    try:
        rows = agent_project.eligible_projects(db, _Admin(), DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    default = next(r for r in rows if r["id"] == "default")
    assert default["is_default"] is True and default["department_id"] is None
    business = next(r for r in rows if r["id"] != "default")
    assert business["is_default"] is False


def _inherit_member(username: str, *, deny: str | None = None) -> str:
    uid = _user(username)
    db = SessionLocal()
    try:
        if db.get(ProjectMember, ("default", uid)) is None:
            db.add(ProjectMember(project_id="default", user_id=uid, role_key="inherit"))
        if deny:
            db.execute(text("UPDATE users SET permissions = :p WHERE id = :id"),
                       {"p": f'{{"{deny}": false}}', "id": uid})
        access.bump_epoch(db)
        db.commit()
    finally:
        db.close()
    return uid


class _Member:
    role = "member"

    def __init__(self, uid: str):
        self.user_id = uid


def test_on_an_inherit_member_with_the_key_sees_the_default_project(monkeypatch):
    _project("default")
    uid = _inherit_member("river2")
    assert "default" not in _eligible(_Member(uid))  # off: never
    _on(monkeypatch)
    access.reset_cache_for_tests()
    assert "default" in _eligible(_Member(uid))
    assert "default" in _eligible(_Member(uid), "agents.import")


def test_on_an_inherit_member_without_the_key_does_not(monkeypatch):
    _project("default")
    uid = _inherit_member("nodeploy", deny="agents.deploy")
    _on(monkeypatch)
    assert "default" not in _eligible(_Member(uid))
    assert "default" in _eligible(_Member(uid), "agents.import")


def test_on_the_stamp_guard_files_an_agent_under_default(monkeypatch):
    _project("default")
    _on(monkeypatch)
    assert _stamp_agent("default") == "default"


def test_on_post_agents_creates_in_the_default_project(client, monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    _project("default")
    spec = {"name": "default-home", "method": "harness", "system_prompt": "Hi.",
            "project_id": "default"}
    refused = client.post("/api/agents", json=spec)
    assert refused.status_code == 422
    assert refused.json()["code"] == agent_project.PROJECT_DEFAULT
    _on(monkeypatch)
    res = client.post("/api/agents", json=spec)
    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        with project_context(None):
            row = db.get(Agent, res.json()["agent"]["id"])
            assert row.project_id == "default"
    finally:
        db.close()


def test_the_eligible_route_reports_the_switch(client, monkeypatch):
    assert client.get("/api/agents/eligible-projects").json()["default_project_allowed"] is False
    _on(monkeypatch)
    assert client.get("/api/agents/eligible-projects").json()["default_project_allowed"] is True


# ── the settings routes ───────────────────────────────────────────────────────


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


def _admin_client(app) -> TestClient:
    client = TestClient(app, client=("127.0.0.1", 4321))
    assert client.post("/api/auth/login", json=ADMIN).status_code == 200
    return client


def test_only_an_administrator_reads_or_flips_it(gated):
    _user("plainmember")
    member = _login(gated, "plainmember")
    assert member.get("/api/settings/agent-projects").status_code == 403
    put = member.put("/api/settings/agent-projects", json={"allow_default_project": True})
    assert put.status_code == 403
    assert agent_project_settings.allowed() is False


def test_an_admin_flip_applies_now_is_journaled_and_clears_back(gated):
    admin = _admin_client(gated)
    view = admin.get("/api/settings/agent-projects").json()
    assert view["allow_default_project"] is False and view["source"] == "default"
    on = admin.put("/api/settings/agent-projects", json={"allow_default_project": True})
    assert on.status_code == 200, on.text
    assert on.json()["allow_default_project"] is True and on.json()["source"] == "console"
    assert on.json()["updated_by"] == ADMIN["username"]
    assert agent_project_settings.allowed() is True  # no restart
    db = SessionLocal()
    try:
        events = db.scalars(select(AuditEvent).where(
            AuditEvent.action == agent_project_settings.AUDIT_ACTION)).all()
        assert len(events) == 1 and "effective=false->true" in events[0].target
    finally:
        db.close()
    cleared = admin.put("/api/settings/agent-projects", json={"allow_default_project": None})
    assert cleared.json()["allow_default_project"] is False
    assert cleared.json()["source"] == "default" and cleared.json()["console_value"] is None


def test_the_console_value_beats_env(monkeypatch):
    db = SessionLocal()
    db.add(PlatformSetting(key=agent_project_settings.SETTING, value="false"))
    db.commit()
    db.close()
    monkeypatch.setenv(ENV, "true")
    get_settings.cache_clear()
    assert get_settings().agent_allow_default_project is True  # env alone
    create_app()  # startup applies the console row onto the cached Settings
    assert agent_project_settings.allowed() is False
    db = SessionLocal()
    try:
        view = agent_project_settings.view(db)
    finally:
        db.close()
    assert view["source"] == "console" and view["env_value"] == "true"


def test_clearing_the_override_falls_back_to_env(monkeypatch):
    monkeypatch.setenv(ENV, "true")
    get_settings.cache_clear()

    class _Actor:
        username, user_id = "operator", None

    db = SessionLocal()
    try:
        off = agent_project_settings.set_allowed(db, False, _Actor())
        assert off["allow_default_project"] is False
        back = agent_project_settings.set_allowed(db, None, _Actor())
    finally:
        db.close()
    assert back["allow_default_project"] is True and back["source"] == "env"


def test_env_alone_turns_it_on_for_every_path(monkeypatch):
    """The prod rollout path: LAUNCHPAD_AGENT_ALLOW_DEFAULT_PROJECT=true, no console row."""
    monkeypatch.setenv(ENV, "true")
    get_settings.cache_clear()
    app = create_app()  # startup finds no console row and leaves the env value alone
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    _project("default")
    assert agent_project_settings.allowed() is True
    assert _check("default") == "default"
    assert "default" in _eligible(_Admin())
    assert _stamp_agent("default") == "default"
    client = TestClient(app)
    listed = client.get("/api/agents/eligible-projects").json()
    assert listed["default_project_allowed"] is True
    assert "default" in [p["id"] for p in listed["projects"]]
    res = client.post("/api/agents", json={"name": "env-home", "method": "harness",
                                           "system_prompt": "Hi.", "project_id": "default"})
    assert res.status_code == 202, res.text
    db = SessionLocal()
    try:
        assert agent_project_settings.view(db)["source"] == "env"
    finally:
        db.close()


def test_a_console_value_survives_a_runtime_cache_clear(gated):
    """model_prices.refresh / a bootstrap call get_settings.cache_clear(): the rebuilt
    Settings keeps the console value (core/config overlay), source stays console."""
    admin = _admin_client(gated)
    assert admin.put("/api/settings/agent-projects",
                     json={"allow_default_project": True}).status_code == 200
    get_settings.cache_clear()
    assert agent_project_settings.allowed() is True
    assert admin.get("/api/settings/agent-projects").json()["source"] == "console"
    admin.put("/api/settings/agent-projects", json={"allow_default_project": None})
    get_settings.cache_clear()
    assert agent_project_settings.allowed() is False


def test_the_overlay_does_not_leak_between_tests_part_one():
    from app.core.config import set_console_override

    set_console_override("agent_allow_default_project", True)
    assert agent_project_settings.allowed() is True


def test_the_overlay_does_not_leak_between_tests_part_two():
    # conftest's clean_tables reset the overlay after part one
    from app.core.config import console_overrides

    assert console_overrides() == {}
    assert agent_project_settings.allowed() is False
