"""Online console update, the web half (online-update §2.1–§2.6, §3): fast-forward
check against a fixed source, the resumable prepare job, schedule / cancel, the
announcement, the `apply.request` hand-over, the `apply.result` read-back, rollback,
and the routes. Git runs for real against local temporary repositories (no network);
every build command and the port self-test are fakes."""

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.main import create_app
from app.models.announcement import Announcement
from app.models.ledger import AuditEvent, Job
from app.models.system_update import SystemUpdate
from app.services import inflight
from app.services import system_update as su


class _Admin:
    username = "operator"
    user_id = None
    is_admin = True


ADMIN = _Admin()


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit(repo: Path, message: str) -> str:
    (repo / "README.md").write_text(message, encoding="utf-8")
    sh("git", "add", "-A", cwd=repo)
    sh("git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m",
       message, cwd=repo)
    return sh("git", "rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def env(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=origin)
    c0, c1, c2 = commit(origin, "c0"), commit(origin, "c1"), commit(origin, "c2: the fix")
    live = tmp_path / "live"
    sh("git", "clone", "-q", str(origin), str(live), cwd=tmp_path)
    sh("git", "reset", "-q", "--hard", c1, cwd=live)

    monkeypatch.setattr(su, "REPO_DIR", live)
    monkeypatch.setattr(su, "UPDATES_DIR", tmp_path / "updates")
    updater = (tmp_path / "updater.path", tmp_path / "apply_update.sh")
    monkeypatch.setattr(su, "UPDATER_FILES", updater)
    # the host's real free space is not under test (a 3.8 GB tmpfs /tmp on the prod
    # boxes sits near the 3 GB floor); test_low_disk_is_refused sets its own
    monkeypatch.setattr(su, "disk_free_bytes", lambda _path: 100 * 1024**3)
    monkeypatch.setenv("LAUNCHPAD_UPDATE_REPO", str(origin))
    monkeypatch.setenv("LAUNCHPAD_UPDATE_BRANCH", "main")
    monkeypatch.delenv("LAUNCHPAD_UPDATE_ENABLED", raising=False)
    get_settings.cache_clear()
    su.reset_check_cache_for_tests()

    calls: list[dict] = []
    real = su.run_command

    def fake(argv, *, cwd=None, env=None, timeout=600):
        if argv[0] == "git":
            return real(argv, cwd=cwd, env=env, timeout=timeout)
        calls.append({"argv": list(argv), "cwd": str(cwd), "env": env or {}})
        if fail.get("argv") and fail["argv"] in " ".join(argv):
            return su.CommandResult(1, "", "boom: npm registry unreachable")
        if argv[-2:] == ["run", "build"]:
            (Path(cwd) / "dist").mkdir(parents=True, exist_ok=True)
            (Path(cwd) / "dist" / "index.html").write_text("built", encoding="utf-8")
        return su.CommandResult(0, "", "")

    fail: dict[str, str] = {}
    monkeypatch.setattr(su, "run_command", fake)
    probes: list[str] = []
    monkeypatch.setattr(su, "port_probe", lambda stage, sha, env: probes.append(sha))
    # the job runs inline so the tests read its outcome directly
    monkeypatch.setattr(su, "start_prepare", lambda update_id: su.run_prepare(update_id))
    yield {"origin": origin, "live": live, "c0": c0, "c1": c1, "c2": c2, "calls": calls,
           "fail": fail, "probes": probes, "updater": updater, "tmp": tmp_path}
    get_settings.cache_clear()
    su.reset_check_cache_for_tests()


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


def install_updater(env) -> None:
    for path in env["updater"]:
        path.write_text("", encoding="utf-8")


def ready(db, env) -> SystemUpdate:
    row = su.prepare(db, ADMIN)
    db.refresh(row)
    assert row.status == "ready", row.error
    return row


def audit_actions(db) -> list[str]:
    return [a.action for a in db.query(AuditEvent).order_by(AuditEvent.created_at).all()]


# ── check ──────────────────────────────────────────────────────────────────────


def test_check_reports_a_fast_forward_and_its_commits(env):
    info = su.check_remote(force=True)
    assert info["sha"] == env["c2"] and info["running_sha"] == env["c1"]
    assert info["fast_forward"] is True and info["behind"] == 1 and not info["up_to_date"]
    assert [c["title"] for c in info["commits"]] == ["c2: the fix"]
    assert info["repo"] == str(env["origin"]) and info["branch"] == "main"


def test_check_is_cached_for_ten_minutes(env):
    first = su.check_remote()
    commit(env["origin"], "c3")
    assert su.check_remote()["sha"] == first["sha"]
    assert su.check_remote(force=True)["behind"] == 2


def test_a_rewritten_branch_is_not_a_fast_forward_and_is_refused(env, db):
    sh("git", "checkout", "-q", "--orphan", "rewritten", cwd=env["origin"])
    commit(env["origin"], "rewritten")
    sh("git", "branch", "-q", "-f", "main", "rewritten", cwd=env["origin"])
    assert su.check_remote(force=True)["fast_forward"] is False
    with pytest.raises(su.AppError) as exc:
        su.prepare(db, ADMIN)
    assert exc.value.code == "update.not_fast_forward"
    assert db.query(SystemUpdate).count() == 0


def test_up_to_date_is_refused(env, db):
    sh("git", "reset", "-q", "--hard", env["c2"], cwd=env["live"])
    su.reset_check_cache_for_tests()
    with pytest.raises(su.AppError) as exc:
        su.prepare(db, ADMIN)
    assert exc.value.code == "update.up_to_date"


def test_disabled_turns_everything_off(env, db, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_UPDATE_ENABLED", "false")
    get_settings.cache_clear()
    install_updater(env)
    with pytest.raises(su.AppError) as exc:
        su.check_remote(force=True)
    assert exc.value.code == "update.disabled"
    assert su.can_schedule() is False and su.cached_check() is None


# ── prepare ────────────────────────────────────────────────────────────────────


def test_prepare_stages_builds_and_self_tests_the_target(env, db):
    row = ready(db, env)
    stage = su.stage_dir_for(env["c2"])
    assert row.kind == "update" and row.target_sha == env["c2"] and row.base_sha == env["c1"]
    assert row.behind == 1 and row.commits[0]["title"] == "c2: the fix"
    assert set(row.progress) == set(su.PREPARE_STEPS)
    assert all(v == "done" for v in row.progress.values())
    assert sh("git", "rev-parse", "HEAD", cwd=stage) == env["c2"]
    assert (stage / su.STAGE_MARKER).read_text().strip() == env["c2"]
    argvs = [c["argv"] for c in env["calls"]]
    # fixed commands only, the builds at low priority
    assert ["nice", "-n", "10", "uv", "sync", "--frozen"] in argvs
    assert ["nice", "-n", "10", "npm", "ci"] in argvs
    assert ["nice", "-n", "10", "npm", "run", "build"] in argvs
    assert ["uv", "run", "--no-sync", "python", "-c", "import app.main"] in argvs
    assert env["probes"] == [env["c2"]]
    # the self-test never touches the live ledger
    for call in env["calls"]:
        selftest = f"sqlite:///{stage / 'data' / 'selftest.db'}"
        assert call["env"]["LAUNCHPAD_DATABASE_URL"] == selftest
        assert "VIRTUAL_ENV" not in call["env"]
    assert "system.update.prepare" in audit_actions(db)
    # the running checkout is untouched
    assert sh("git", "rev-parse", "HEAD", cwd=env["live"]) == env["c1"]


def test_a_failed_build_is_prepare_failed_and_the_service_is_unaffected(env, db):
    env["fail"]["argv"] = "npm ci"
    row = su.prepare(db, ADMIN)
    db.refresh(row)
    assert row.status == "prepare_failed"
    assert "npm registry unreachable" in row.error
    assert row.progress == {"fetch": "done", "worktree": "done", "backend": "done"}
    events = su.log_events(row)
    assert events[-1]["level"] == "error"


def test_a_resumed_prepare_skips_the_finished_steps(env, db):
    env["fail"]["argv"] = "npm ci"
    row = su.prepare(db, ADMIN)
    env["fail"].clear()
    env["calls"].clear()
    db.refresh(row)
    row.status = "preparing"  # what a restart mid-build leaves
    db.commit()
    su.run_prepare(row.id)
    db.refresh(row)
    assert row.status == "ready", row.error
    argvs = [" ".join(c["argv"]) for c in env["calls"]]
    assert not any("uv sync" in a for a in argvs), "the backend step was done"
    assert any("npm ci" in a for a in argvs)


def test_only_one_update_at_a_time_and_a_new_prepare_supersedes_a_ready_one(env, db):
    first = ready(db, env)
    commit(env["origin"], "c3")
    su.reset_check_cache_for_tests()
    second = ready(db, env)
    db.refresh(first)
    assert first.status == "superseded" and second.target_sha != first.target_sha
    second.status = "scheduled"
    db.commit()
    with pytest.raises(su.AppError) as exc:
        su.prepare(db, ADMIN)
    assert exc.value.code == "update.in_progress"


def test_low_disk_is_refused(env, db, monkeypatch):
    monkeypatch.setattr(su, "disk_free_bytes", lambda _path: 1024)
    with pytest.raises(su.AppError) as exc:
        su.prepare(db, ADMIN)
    assert exc.value.code == "update.disk_low"


def test_a_stage_over_the_cap_fails(env, db, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_UPDATE_STAGE_MAX_GB", "0.000000001")
    get_settings.cache_clear()
    row = su.prepare(db, ADMIN)
    db.refresh(row)
    assert row.status == "prepare_failed" and row.error.startswith("update.disk_low")


def test_the_inventory_lists_a_preparing_update_as_resumed(env, db):
    db.add(SystemUpdate(target_sha=env["c2"], status="preparing"))
    db.commit()
    items = [i for i in inflight.collect_inflight(db) if i.kind == inflight.SYSTEM_UPDATE_PREPARE]
    assert len(items) == 1
    assert items[0].restart_outcome == inflight.RESUMES
    assert items[0].handled_by == "resume_system_updates"


# ── schedule / cancel ──────────────────────────────────────────────────────────


def test_schedule_needs_the_updater(env, db):
    row = ready(db, env)
    with pytest.raises(su.AppError) as exc:
        su.schedule(db, row.id, None, False, ADMIN)
    assert exc.value.code == "update.updater_missing"
    state = su.state(db)
    assert state["version"]["can_schedule"] is False
    assert any(f"git merge --ff-only {env['c2']}" == c for c in state["manual_commands"])


def test_schedule_now_counts_down_and_announces(env, db):
    install_updater(env)
    row = ready(db, env)
    before = datetime.now(UTC)
    su.schedule(db, row.id, None, False, ADMIN)
    db.refresh(row)
    assert row.status == "scheduled"
    delay = su._aware(row.scheduled_at) - before
    assert timedelta(seconds=55) < delay < timedelta(seconds=70)
    notice = db.get(Announcement, row.announcement_id)
    assert notice is not None and notice.published_content is not None
    assert "1–2" in notice.published_content["body"]
    assert "system.update.schedule" in audit_actions(db)


def test_schedule_window_is_seven_days(env, db):
    install_updater(env)
    row = ready(db, env)
    with pytest.raises(su.AppError) as exc:
        su.schedule(db, row.id, datetime.now(UTC) + timedelta(days=8), False, ADMIN)
    assert exc.value.code == "update.schedule_invalid"
    with pytest.raises(su.AppError):
        su.schedule(db, row.id, datetime.now(UTC) - timedelta(hours=1), False, ADMIN)


def test_interrupted_tasks_must_be_acknowledged(env, db):
    install_updater(env)
    row = ready(db, env)
    # a job type nothing resumes: the restart strands it
    db.add(Job(workspace_id="default", type="delete_agent", status="running"))
    db.commit()
    state = su.state(db)
    assert state["inflight"]["interrupted"] == 1
    with pytest.raises(su.AppError) as exc:
        su.schedule(db, row.id, None, False, ADMIN)
    assert exc.value.code == "update.inflight_unacknowledged"
    su.schedule(db, row.id, None, True, ADMIN)
    db.refresh(row)
    assert row.status == "scheduled"


def test_cancel_returns_to_ready_and_withdraws_the_notice(env, db):
    install_updater(env)
    row = ready(db, env)
    su.schedule(db, row.id, None, False, ADMIN)
    db.refresh(row)
    notice_id = row.announcement_id
    su.cancel(db, row.id, ADMIN)
    db.refresh(row)
    assert row.status == "ready" and row.scheduled_at is None
    assert db.get(Announcement, notice_id) is None
    row.status = "applying"
    db.commit()
    with pytest.raises(su.AppError) as exc:
        su.cancel(db, row.id, ADMIN)
    assert exc.value.code == "update.not_cancellable"


# ── hand-over and read-back ────────────────────────────────────────────────────


def test_the_due_update_is_handed_to_the_updater_and_its_result_read_back(env, db):
    install_updater(env)
    row = ready(db, env)
    su.schedule(db, row.id, None, False, ADMIN)
    assert su.tick(db)["handed_over"] == []  # not due yet
    out = su.tick(db, now=datetime.now(UTC) + timedelta(minutes=2))
    assert out["handed_over"] == [row.id]
    db.refresh(row)
    assert row.status == "applying"
    request = json.loads((su.updates_dir() / "apply.request").read_text())
    assert request == {"update_id": row.id, "sha": env["c2"], "action": "update",
                       "stage_dir": str(su.stage_dir_for(env["c2"]))}

    # the updater consumed the request and wrote its result
    (su.updates_dir() / "apply.request").unlink()
    (su.updates_dir() / "apply.result").write_text(json.dumps({
        "update_id": row.id, "sha": env["c2"], "previous_sha": env["c1"], "status": "done",
        "error": None, "log": ["ledger backed up", "health check passed"]}))
    assert su.tick(db)["result_read"] == row.id
    db.refresh(row)
    assert row.status == "done" and row.previous_sha == env["c1"] and row.finished_at
    assert any(e["message"] == "health check passed" for e in su.log_events(row))
    assert db.get(Announcement, row.announcement_id).published_content is None
    assert not (su.updates_dir() / "apply.result").exists()
    assert (su.updates_dir() / f"apply.result.{row.id}").exists()
    assert audit_actions(db).count("system.update.apply") == 2


def test_a_rolled_back_result(env, db):
    row = SystemUpdate(target_sha=env["c2"], status="applying")
    db.add(row)
    db.commit()
    su.updates_dir().mkdir(parents=True)
    (su.updates_dir() / "apply.result").write_text(json.dumps({
        "update_id": row.id, "status": "rolled_back", "previous_sha": env["c1"],
        "error": "update.apply_failed: health check timed out"}))
    su.read_result(db)
    db.refresh(row)
    assert row.status == "rolled_back" and "health check" in row.error


def test_the_result_is_read_at_startup(env, db):
    row = SystemUpdate(target_sha=env["c2"], status="applying")
    db.add(row)
    db.commit()
    su.updates_dir().mkdir(parents=True)
    (su.updates_dir() / "apply.result").write_text(json.dumps(
        {"update_id": row.id, "status": "done", "previous_sha": env["c1"]}))
    su.resume_system_updates()
    db.expire_all()
    assert db.get(SystemUpdate, row.id).status == "done"


def test_a_request_nobody_picks_up_fails(env, db):
    install_updater(env)
    row = SystemUpdate(target_sha=env["c2"], status="applying")
    db.add(row)
    db.commit()
    request = su.updates_dir() / "apply.request"
    request.parent.mkdir(parents=True)
    request.write_text("{}")
    old = datetime.now(UTC).timestamp() - 3600
    os.utime(request, (old, old))
    su.tick(db)
    db.refresh(row)
    assert row.status == "failed" and row.error.startswith("update.updater_timeout")
    assert not request.exists()


# ── rollback ───────────────────────────────────────────────────────────────────


def test_rollback_stages_the_commit_the_last_update_replaced(env, db):
    db.add(SystemUpdate(target_sha=env["c1"], status="done", previous_sha=env["c0"],
                        finished_at=datetime.now(UTC)))
    db.commit()
    row = su.rollback(db, ADMIN)
    db.refresh(row)
    assert row.kind == "rollback" and row.target_sha == env["c0"] and row.status == "ready"
    assert row.behind == 1 and row.commits[0]["title"] == "c1"
    assert "system.update.rollback" in audit_actions(db)


def test_rollback_needs_an_update_to_undo(env, db):
    with pytest.raises(su.AppError) as exc:
        su.rollback(db, ADMIN)
    assert exc.value.code == "update.no_previous"


# ── routes ─────────────────────────────────────────────────────────────────────


def test_routes(env):
    client = TestClient(create_app())
    version = client.get("/api/system/version").json()
    assert version["sha"] == env["c1"] and version["updater_installed"] is False
    assert client.get("/api/auth/status").json()["version"] == {"sha": env["c1"]}
    # the source is configuration: a body naming another repository is rejected
    assert client.post("/api/system/update/check", json={"repo": "https://evil.example"}
                       ).status_code == 422
    assert client.post("/api/system/update/check").json()["behind"] == 1
    state = client.get("/api/system/update").json()
    assert state["source"] == {"repo": str(env["origin"]), "branch": "main"}
    assert state["current"] is None and state["remote"]["sha"] == env["c2"]
    prepared = client.post("/api/system/update/prepare")
    assert prepared.status_code == 202, prepared.text
    update_id = prepared.json()["id"]
    log = client.get(f"/api/system/update/{update_id}/log").json()
    assert log["status"] == "ready" and log["events"]
    assert client.get("/api/system/update/nope/log").status_code == 404
    refused = client.post(f"/api/system/update/{update_id}/schedule", json={})
    assert refused.status_code == 409 and refused.json()["code"] == "update.updater_missing"
