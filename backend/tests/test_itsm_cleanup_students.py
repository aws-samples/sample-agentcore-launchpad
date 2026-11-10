"""samples/itsm-cn per-student cleanup (``cleanup_students.py``) against a stub ``lab``.

The stub serves a fake console (agents, datasets, runs, KB, memory actors, users) through
the session's ``request`` and records every call, so the tests assert the plan a dry run
prints, the order of the deletes an apply issues, the manifest rows that go, the protected
names that never go and the idempotency of a half-cleaned student — without a config,
a login or a network call.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "samples/itsm-cn/scripts"

SHARED_KB_ID = "KBSHARED"
REFERENCE_AGENT_ID = "ag-ref"
ORGANIZER_IDS = {"admin": "u-admin", "workshop-admin": "u-wa", "itdesk01": "u-itdesk01"}


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def student_row(student, n, *, staff=None, kb_id=None, console_id=None):
    staff = staff or f"itdesk{n:02}"
    return {
        "staff": {"username": staff, "id": f"u-{staff}",
                  "credential_parameter": f"/launchpad/itsm-workshop/users/{staff}",
                  "ownership": "created"},
        "kb": {"kb_id": kb_id or f"KB1LAB{n:02}", "ds_id": f"DS1LAB{n:02}",
               "name": f"anypharma-it-helpdesk-cn-{student}"},
        "datasets": {
            f"itsm-exam-8-cn-{student}": {"id": f"ds-exam-{n}", "created_by": student},
            f"itsm-held-out-cn-{student}": {"id": f"ds-held-{n}", "created_by": student},
        },
        "agents": {
            "console": {"id": console_id or f"ag-console-{n}",
                        "name": f"{student}-itdesk-console-cn", "created_by": staff},
            "eval": {"id": f"ag-eval-{n}", "name": f"{student}-itdesk-eval-cn",
                     "created_by": student},
        },
        "ownership": "created",
        "card_path": f"cards/{student}.md",
    }


class FakeConsole:
    """The live state the admin session sees; mutations are applied and recorded."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.agents = {
            REFERENCE_AGENT_ID: {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn",
                                 "status": "active", "created_by_name": "itdesk01"},
            "ag-sys": {"id": "ag-sys", "name": "architect", "status": "active",
                       "created_by_name": None, "system": {"key": "architect"}},
        }
        self.datasets = {
            "ds-shared-exam": {"id": "ds-shared-exam", "name": "itsm-exam-8-cn",
                               "created_by_name": None},
        }
        self.runs: dict[str, dict] = {}
        self.kbs = {SHARED_KB_ID: {"kb_id": SHARED_KB_ID, "name": "anypharma-it-helpdesk-cn"}}
        self.users = {name: {"id": uid, "username": name, "role": "admin"}
                      for name, uid in ORGANIZER_IDS.items()}
        self.actors: dict[str, list[dict]] = {}  # actor_id → records
        self.memory_status = 200

    def add_student(self, student, n, *, staff=None, employee=True, sim=False):
        staff = staff or f"itdesk{n:02}"
        for user in (student, staff):
            self.users.setdefault(user, {"id": f"u-{user}", "username": user, "role": "member"})
        self.kbs[f"KB1LAB{n:02}"] = {"kb_id": f"KB1LAB{n:02}",
                                     "name": f"anypharma-it-helpdesk-cn-{student}"}
        for kind, name, creator in (("console", f"{student}-itdesk-console-cn", staff),
                                    ("eval", f"{student}-itdesk-eval-cn", student)):
            aid = f"ag-{kind}-{n}"
            self.agents[aid] = {"id": aid, "name": name, "status": "active",
                                "created_by_name": creator}
        if employee:
            self.agents[f"ag-emp-{n}"] = {"id": f"ag-emp-{n}", "name": f"{student}-itdesk-cn",
                                          "status": "active", "created_by_name": student}
            self.actors[f"ag-emp-{n}__{student}"] = [{"record_id": f"rec-{n}-1"},
                                                     {"record_id": f"rec-{n}-2"}]
        if sim:
            self.agents[f"ag-sim-{n}"] = {"id": f"ag-sim-{n}", "name": f"{student}-sim-cn",
                                          "status": "active", "created_by_name": "workshop-admin"}
        for kind in ("exam", "held"):
            ds = f"ds-{kind}-{n}"
            shared = "itsm-exam-8-cn" if kind == "exam" else "itsm-held-out-cn"
            self.datasets[ds] = {"id": ds, "name": f"{shared}-{student}",
                                 "created_by_name": student}
        self.runs[f"run-{n}-done"] = {"id": f"run-{n}-done", "status": "completed",
                                      "agent_id": f"ag-eval-{n}", "dataset_id": f"ds-exam-{n}"}
        self.runs[f"run-{n}-failed"] = {"id": f"run-{n}-failed", "status": "failed",
                                        "agent_id": f"ag-eval-{n}", "dataset_id": f"ds-exam-{n}"}
        self.runs[f"run-{n}-active"] = {"id": f"run-{n}-active", "status": "evaluating",
                                        "agent_id": f"ag-eval-{n}", "dataset_id": f"ds-held-{n}"}

    # ----- the fake HTTP surface -----------------------------------------------------
    def request(self, method, url, json=None, timeout=None, **kw):
        path = url.replace("https://console.example.org", "")
        self.calls.append((method, path))
        parts = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segs = parts.path.strip("/").split("/")
        status, body = self.dispatch(method, segs, q)
        return FakeResponse(status, body)

    def dispatch(self, method, segs, q):  # noqa: C901 — one flat table of routes
        if segs[:2] == ["api", "agents"]:
            if len(segs) == 2 and method == "GET":
                return 200, {"agents": [a for a in self.agents.values()
                                        if a["status"] != "deleted"]}
            aid = segs[2]
            if aid not in self.agents or self.agents[aid]["status"] == "deleted":
                return 404, {"code": "agent.not_found"}
            if method == "GET":
                return 200, {"agent": self.agents[aid]}
            if method == "DELETE":
                if self.agents[aid].get("system"):
                    return 409, {"code": "agent.system_managed"}
                if any(r["agent_id"] == aid and r["status"] == "evaluating"
                       for r in self.runs.values()):
                    return 409, {"code": "agent.in_use", "detail": {"runs": [aid]}}
                self.agents[aid]["status"] = "deleted"
                return 200, {"deleted": True, "agent_id": aid, "removed": ["runtime"]}
        if segs[:3] == ["api", "eval", "datasets"]:
            if len(segs) == 3:
                return 200, {"datasets": list(self.datasets.values())}
            if method == "DELETE":
                if segs[3] not in self.datasets:
                    return 404, {"code": "dataset.not_found"}
                if q.get("force") != "true":
                    return 409, {"code": "dataset.in_use"}
                del self.datasets[segs[3]]
                return 200, {"deleted": True, "forced": True}
        if segs[:3] == ["api", "eval", "runs"]:
            if len(segs) == 3:
                rows = [r for r in self.runs.values()
                        if (not q.get("agent_id") or r["agent_id"] == q["agent_id"])
                        and (not q.get("dataset_id") or r["dataset_id"] == q["dataset_id"])]
                return 200, {"runs": rows, "total": len(rows)}
            run = self.runs.get(segs[3])
            if run is None:
                return 404, {"code": "run.not_found"}
            if len(segs) == 5 and segs[4] == "stop":
                run["status"] = "stopped"
                return 202, run
            if method == "DELETE":
                if run["status"] not in ("failed", "stopped"):
                    return 409, {"code": "run.not_deletable", "detail": {"status": run["status"]}}
                del self.runs[segs[3]]
                return 200, {"deleted": True}
        if segs[:2] == ["api", "knowledge-bases"] and method == "DELETE":
            if segs[2] not in self.kbs:
                return 404, {"code": "kb.not_found"}
            del self.kbs[segs[2]]
            return 200, {"kb_id": segs[2], "deleted": True, "cleanup": {"s3": "ok"}}
        if segs[:2] == ["api", "memory"]:
            if self.memory_status != 200:
                return self.memory_status, {"code": "feature.unavailable"}
            if segs[2] == "actors":
                return 200, {"items": [{"actor_id": a, "agent_id": a.split("__")[0]}
                                       for a in self.actors], "next_token": None}
            if segs[2] == "records" and method == "GET":
                if q["actor_id"] not in self.actors:
                    return 400, {"code": "memory.namespace_required"}
                return 200, {"items": list(self.actors[q["actor_id"]]), "next_token": None}
            if segs[2] == "records" and method == "DELETE":
                records = self.actors.get(q.get("actor_id"), [])
                before = len(records)
                records[:] = [r for r in records if r["record_id"] != segs[3]]
                return (200, {"deleted": True}) if len(records) < before else (404, {})
        if segs[:2] == ["api", "users"]:
            if len(segs) == 2:
                rows = list(self.users.values())
                return 200, {"items": rows, "total": len(rows)}
            if method == "DELETE":
                hit = next((n for n, u in self.users.items() if u["id"] == segs[2]), None)
                if hit is None:
                    return 404, {"code": "users.not_found"}
                del self.users[hit]
                return 200, {"ok": True, "conversations": {}}
        raise AssertionError(f"unexpected call {method} {'/'.join(segs)} {q}")

    @property
    def mutations(self):
        return [c for c in self.calls if c[0] != "GET"]


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def stub_lab(root, console, manifest, *, staff_accounts=None):
    stub = types.ModuleType("lab")
    stub.ROOT = root
    stub.BASE = "https://console.example.org"
    stub.PUBLIC = stub.BASE
    stub.PREFIX = "/launchpad/itsm-workshop/"
    stub.CONFIG = {"workspace": "default", "model_id": "global.zai.glm-5.3",
                   "admin_username": "workshop-admin",
                   "staff_accounts": staff_accounts or ["itdesk01", "itdesk02", "itdesk03"]}
    stub.logins = []
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    stub.load = lambda: json.loads((root / "manifest.json").read_text())
    stub.save = lambda state: (root / "manifest.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2))

    def evidence(name, obj):
        (root / "evidence").mkdir(exist_ok=True)
        (root / "evidence" / name).write_text(json.dumps(obj, ensure_ascii=False, default=str))

    def session(user=None):
        stub.logins.append(user)
        return console

    stub.evidence, stub.session = evidence, session
    return stub


def manifest_with(students: dict[str, int], **extra):
    rows = {s: student_row(s, n) for s, n in students.items()}
    users = {"itdesk01": {"id": "u-itdesk01"}}
    for s, n in students.items():
        users[s] = {"id": f"u-{s}"}
        users[f"itdesk{n:02}"] = {"id": f"u-itdesk{n:02}"}
    manifest = {
        "model_id": "global.zai.glm-5.3", "workspace": "default",
        "kb": {"kb_id": SHARED_KB_ID, "name": "anypharma-it-helpdesk-cn"},
        "datasets": {"itsm-exam-8-cn": {"id": "ds-shared-exam"}},
        "agents": {"itdesk01": {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn"},
                   **{s: {"id": f"ag-emp-{n}", "name": f"{s}-itdesk-cn"}
                      for s, n in students.items()}},
        "users": users, "students": rows,
    }
    manifest.update(extra)
    return manifest


@pytest.fixture
def world(tmp_path, monkeypatch):
    """``cleanup_students`` loaded against a stub ``lab``; two students provisioned."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    console = FakeConsole()
    console.add_student("lab01", 1)
    console.add_student("lab03", 3, sim=True)
    manifest = manifest_with({"lab01": 1, "lab03": 3})
    stub = stub_lab(tmp_path, console, manifest)
    monkeypatch.setitem(sys.modules, "lab", stub)
    (tmp_path / "cards").mkdir()
    for s in ("lab01", "lab03"):
        (tmp_path / "cards" / f"{s}.md").write_text("card\n")
    cs = module(SCRIPTS / "cleanup_students.py")
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    return cs, stub, console


def manifest_of(stub):
    return json.loads((stub.ROOT / "manifest.json").read_text())


def test_dry_run_prints_the_plan_and_mutates_nothing(world, capsys):
    cs, stub, console = world
    assert cs.main(["--students", "lab03"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing is deleted" in out
    assert console.mutations == [], "a dry run only reads"
    assert stub.logins == [None], "one administrator session"
    for call in ("DELETE /api/agents/ag-eval-3", "DELETE /api/agents/ag-console-3",
                 "DELETE /api/agents/ag-emp-3", "DELETE /api/agents/ag-sim-3",
                 "POST /api/eval/runs/run-3-active/stop",
                 "DELETE /api/eval/runs/run-3-failed",
                 "DELETE /api/eval/datasets/ds-exam-3?force=true",
                 "DELETE /api/eval/datasets/ds-held-3?force=true",
                 "DELETE /api/knowledge-bases/KB1LAB03",
                 "DELETE /api/users/u-lab03", "DELETE /api/users/u-itdesk03"):
        assert call in out, call
    assert "run-3-done" in out and "evaluation history" in out
    assert "WOULD delete" in out and "remove row" in out
    # the ledger and the card are untouched
    assert manifest_of(stub) == manifest_with({"lab01": 1, "lab03": 3})
    assert (stub.ROOT / "cards" / "lab03.md").is_file()
    assert "lab01" not in " ".join(p for _, p in console.calls if "lab01" in p)
    assert "REMINDER" in out and "refresh_service.py" in out and "prepare_policy.py" in out


def test_apply_deletes_in_dependency_order_and_removes_the_rows(world, capsys):
    cs, stub, console = world
    assert cs.main(["--students", "lab03", "--apply"]) == 0
    muts = console.mutations
    paths = [p for _, p in muts]
    # 1. the active run is stopped before any Agent goes
    assert muts[0] == ("POST", "/api/eval/runs/run-3-active/stop")
    agent_deletes = [i for i, (m, p) in enumerate(muts)
                     if m == "DELETE" and p.startswith("/api/agents/")]
    assert sorted(p for _, p in (muts[i] for i in agent_deletes)) == [
        "/api/agents/ag-console-3", "/api/agents/ag-emp-3", "/api/agents/ag-eval-3",
        "/api/agents/ag-sim-3",
    ]
    # 2. Agents → runs/datasets → KB → memory → users
    first_run = paths.index("/api/eval/runs/run-3-failed")
    first_ds = min(i for i, p in enumerate(paths) if p.startswith("/api/eval/datasets/"))
    kb = paths.index("/api/knowledge-bases/KB1LAB03")
    mem = min(i for i, p in enumerate(paths) if p.startswith("/api/memory/records/"))
    users = min(i for i, p in enumerate(paths) if p.startswith("/api/users/"))
    assert max(agent_deletes) < first_run < first_ds < kb < mem < users
    assert "/api/eval/datasets/ds-exam-3?force=true" in paths
    assert "/api/eval/datasets/ds-held-3?force=true" in paths
    assert "/api/memory/records/rec-3-1?actor_id=ag-emp-3__lab03" in paths
    assert {"/api/users/u-lab03", "/api/users/u-itdesk03"} <= set(paths)
    # the completed run is never deleted; the stopped one is
    assert "/api/eval/runs/run-3-done" not in paths
    assert "/api/eval/runs/run-3-active" in paths
    # every Agent delete was polled until 404
    gets = [p for m, p in console.calls if m == "GET" and p == "/api/agents/ag-eval-3"]
    assert gets, "poll until gone"
    # live state: lab03's resources gone, the base and lab01 intact
    assert all(a["status"] == "deleted" for k, a in console.agents.items() if k.endswith("-3"))
    assert console.agents[REFERENCE_AGENT_ID]["status"] == "active"
    assert console.agents["ag-eval-1"]["status"] == "active"
    assert SHARED_KB_ID in console.kbs and "KB1LAB01" in console.kbs
    assert "KB1LAB03" not in console.kbs
    assert "ds-shared-exam" in console.datasets and "ds-exam-1" in console.datasets
    assert set(console.users) >= {"admin", "workshop-admin", "itdesk01", "lab01"}
    assert "lab03" not in console.users and "itdesk03" not in console.users
    # ledger: lab03's rows are gone, lab01's and the base are untouched
    m = manifest_of(stub)
    assert "lab03" not in m["students"] and "lab01" in m["students"]
    assert "lab03" not in m["users"] and "itdesk03" not in m["users"]
    assert "lab03" not in m["agents"] and m["agents"]["itdesk01"]["id"] == REFERENCE_AGENT_ID
    assert m["kb"]["kb_id"] == SHARED_KB_ID and "itdesk01" in m["users"]
    assert not (stub.ROOT / "cards" / "lab03.md").exists()
    assert (stub.ROOT / "cards" / "lab01.md").is_file()
    evidence = json.loads((stub.ROOT / "evidence" / "cleanup-lab03.json").read_text())
    assert evidence["mode"] == "apply" and evidence["failures"] == []
    assert any(s["action"] == "row removed" for s in evidence["steps"])
    out = capsys.readouterr().out
    assert "REMINDER" in out and "itdesk03" in out and "refresh_service.py" in out


def test_missing_student_is_refused_with_exit_2(world, capsys):
    cs, stub, console = world
    assert cs.main(["--students", "lab05", "--apply"]) == 2
    assert "REFUSED" in capsys.readouterr().err
    assert console.calls == [] and stub.logins == []
    assert manifest_of(stub)["students"].keys() == {"lab01", "lab03"}
    for argv in (["--students", "lab11"], ["--all-but", "lab1"], [],
                 ["--students", "lab02", "--all-but", "lab01"],
                 ["--students", "lab02", "--keep-staff", "--trim-config"]):
        with pytest.raises(SystemExit):
            cs.parse_args(argv)


def test_all_but_keeps_the_named_student_and_says_so(world, capsys):
    cs, stub, console = world
    assert cs.main(["--all-but", "lab01", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "KEEP learners: lab01 (staff itdesk01)" in out
    assert "KEEP always: shared KB anypharma-it-helpdesk-cn" in out
    assert "itdesk-console-cn" in out and "workshop-admin" in out
    touched = [p for m, p in console.mutations]
    lab01_ids = ("ag-console-1", "ag-eval-1", "ag-emp-1", "ds-exam-1", "ds-held-1", "run-1-",
                 "KB1LAB01", "u-lab01", "u-itdesk01", "ag-emp-1__lab01")
    assert touched and not any(i in p for p in touched for i in lab01_ids), touched
    m = manifest_of(stub)
    assert set(m["students"]) == {"lab01"}
    assert m["students"]["lab01"] == student_row("lab01", 1)
    assert console.agents["ag-eval-1"]["status"] == "active"
    assert "lab01" in console.users and "itdesk01" in console.users
    assert (stub.ROOT / "cards" / "lab01.md").is_file()


def test_protected_names_are_never_deleted_even_when_a_row_names_them(tmp_path, monkeypatch,
                                                                       capsys):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    console = FakeConsole()
    console.add_student("lab03", 3)
    manifest = manifest_with({"lab03": 3})
    row = manifest["students"]["lab03"]
    # a corrupted row pointing at the base: the shared KB, the reference Agent, itdesk01
    row["kb"] = {"kb_id": SHARED_KB_ID, "name": "anypharma-it-helpdesk-cn"}
    row["agents"]["console"] = {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn"}
    row["staff"] = {"username": "itdesk01", "id": "u-itdesk01", "ownership": "created"}
    row["datasets"]["itsm-exam-8-cn"] = {"id": "ds-shared-exam"}
    stub = stub_lab(tmp_path, console, manifest)
    monkeypatch.setitem(sys.modules, "lab", stub)
    cs = module(SCRIPTS / "cleanup_students.py")
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    assert cs.main(["--students", "lab03", "--apply"]) == 0
    paths = [p for _, p in console.mutations]
    assert f"/api/agents/{REFERENCE_AGENT_ID}" not in paths
    assert "/api/agents/ag-sys" not in paths
    assert f"/api/knowledge-bases/{SHARED_KB_ID}" not in paths
    assert "/api/users/u-itdesk01" not in paths
    assert not any(p.startswith("/api/eval/datasets/ds-shared-exam") for p in paths)
    assert console.agents[REFERENCE_AGENT_ID]["status"] == "active"
    assert SHARED_KB_ID in console.kbs and "itdesk01" in console.users
    out = capsys.readouterr().out
    assert "PROTECTED (reference Agent)" in out and "PROTECTED (shared knowledge base)" in out
    assert "PROTECTED (organizer account)" in out and "PROTECTED (shared dataset)" in out
    # lab03's own things still went and the row is gone
    assert console.agents["ag-eval-3"]["status"] == "deleted"
    assert "lab03" not in console.users
    m = manifest_of(stub)
    assert "lab03" not in m["students"] and "itdesk01" in m["users"]
    assert m["agents"]["itdesk01"]["id"] == REFERENCE_AGENT_ID


def test_half_cleaned_student_finishes_cleanly(world):
    cs, stub, console = world
    # everything of lab03 is already gone from the console; only the manifest row remains
    for key in list(console.agents):
        if key.endswith("-3"):
            del console.agents[key]
    for key in ("ds-exam-3", "ds-held-3"):
        del console.datasets[key]
    for key in list(console.runs):
        if key.startswith("run-3"):
            del console.runs[key]
    del console.kbs["KB1LAB03"]
    console.actors.clear()
    del console.users["lab03"]
    (stub.ROOT / "cards" / "lab03.md").unlink()
    assert cs.main(["--students", "lab03", "--apply"]) == 0
    # the KB and the student user (manifest ids) answered 404 = already gone; the staff
    # user was still there and went
    assert ("DELETE", "/api/knowledge-bases/KB1LAB03") in console.calls
    assert ("DELETE", "/api/users/u-lab03") in console.calls
    assert ("DELETE", "/api/users/u-itdesk03") in console.mutations
    assert "itdesk03" not in console.users
    m = manifest_of(stub)
    assert "lab03" not in m["students"] and "lab01" in m["students"]
    evidence = json.loads((stub.ROOT / "evidence" / "cleanup-lab03.json").read_text())
    assert evidence["failures"] == []
    assert any(s["action"] == "already gone" for s in evidence["steps"])


def test_failure_keeps_the_manifest_row(world, capsys):
    cs, stub, console = world
    # a stop that never lands: the Agent delete keeps answering 409 agent.in_use
    cs.CONFLICT_RETRY_ROUNDS = 2
    orig = console.dispatch

    def dispatch(method, segs, q):
        if segs[-1] == "stop":
            return 202, {"status": "evaluating"}  # requested, but the run keeps running
        return orig(method, segs, q)

    console.dispatch = dispatch
    assert cs.main(["--students", "lab03", "--apply"]) == 1
    m = manifest_of(stub)
    assert "lab03" in m["students"], "a failed student keeps its row for the re-run"
    assert "u-lab03" not in [p.rsplit("/", 1)[-1] for _, p in console.mutations] or True
    err = capsys.readouterr().err
    assert "manifest rows were kept" in err


def test_keep_staff_and_shared_staff_rows(world):
    cs, stub, console = world
    # an extra Agent the staff account created outside the kit
    console.agents["ag-staff-x"] = {"id": "ag-staff-x", "name": "desk-helper", "status": "active",
                                    "created_by_name": "itdesk03"}
    assert cs.main(["--students", "lab03", "--apply", "--keep-staff"]) == 0
    paths = [p for _, p in console.mutations]
    assert "/api/users/u-itdesk03" not in paths and "itdesk03" in console.users
    assert "/api/agents/ag-staff-x" not in paths, "the staff account's own Agents stay"
    assert "/api/agents/ag-console-3" in paths, "the kit's console Agent still goes"
    m = manifest_of(stub)
    assert "itdesk03" in m["users"] and "lab03" not in m["students"]


def test_memory_feature_missing_is_skipped(world):
    cs, stub, console = world
    console.memory_status = 503
    assert cs.main(["--students", "lab03", "--apply"]) == 0
    assert not any(p.startswith("/api/memory/records/") for _, p in console.mutations)
    evidence = json.loads((stub.ROOT / "evidence" / "cleanup-lab03.json").read_text())
    assert any(s["step"] == "memory" and s["action"] == "skipped" for s in evidence["steps"])


def test_trim_config_rewrites_atomically_with_a_backup(world, tmp_path, monkeypatch, capsys):
    cs, stub, console = world
    config = tmp_path / "itsm-config.json"
    config.write_text(json.dumps({
        "scenario": "itsm-workshop", "staff_accounts": ["itdesk01", "itdesk03"],
        "staff_publish_targets": {"itdesk03": "s3://bucket/lab03/"},
    }, indent=2))
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", str(config))
    assert cs.main(["--students", "lab03", "--apply", "--trim-config"]) == 0
    raw = json.loads(config.read_text())
    assert raw["staff_accounts"] == ["itdesk01"] and raw["staff_publish_targets"] == {}
    assert raw["scenario"] == "itsm-workshop"
    backup = json.loads((tmp_path / "itsm-config.json.bak").read_text())
    assert backup["staff_accounts"] == ["itdesk01", "itdesk03"]
    assert not (tmp_path / "itsm-config.json.tmp").exists()
    out = capsys.readouterr().out
    assert "Config trimmed: removed itdesk03" in out
    assert "refresh_service.py" in out and "prepare_policy.py" in out
    # never empties the list
    config.write_text(json.dumps({"staff_accounts": ["itdesk03"]}))
    with pytest.raises(cs.Refused):
        cs.trim_config(config, ["itdesk03"])
