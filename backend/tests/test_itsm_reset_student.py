"""samples/itsm-cn post-class reset of a kept student pair (``reset_student.py``) against a
stub ``lab``.

The stub serves a fake console (agents with redeploy, eval runs + recommendations, memory
namespaces / records, knowledge base documents + sync + ingestion jobs, users, policies, chat
sessions), a fake S3 client and a fake SSM ``remote``, and records every call — so the tests
assert the plan a dry run prints, the order of the mutations an apply issues, the KB object
filter, the memory actor derivation, ``--skip-tickets``, the refusal of an unknown student
and the protected names that are never touched, without a config, a login or a network call.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import types
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/itsm-cn"
SCRIPTS = SAMPLE / "scripts"

SHARED_KB_ID = "KBSHARED"
REFERENCE_AGENT_ID = "ag-ref"
ORGANIZER_IDS = {"admin": "u-admin", "workshop-admin": "u-wa", "itdesk01": "u-itdesk01"}
BUCKET = "launchpad-artifacts-test"
BASE_DOCS = ("01-QDocs权限申请.md", "02-VPN.md", "03-CRM.md", "04-Mail.md", "05-Laptop.md")
# the class pairs lab01 with itdesk11 (itdesk01 is the organizer's reference account)
STAFF_OF = {1: "itdesk11"}


def staff_name(n):
    return STAFF_OF.get(n, f"itdesk{n:02}")


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


def kit_spec(student, n):
    return {
        "name": f"{student}-itdesk-eval-cn", "method": "harness", "model_id": "old-model",
        "model_source": "bedrock", "system_prompt": "KIT BASELINE PROMPT", "native_tools": [],
        "memory": {"short_term": False, "long_term": False},
        "tools": [{"type": "gateway", "name": "servicenow",
                   "config": {"gateway_id": "gw", "record_id": "rec",
                              "tools": ["servicenow___get_ticket"]}}],
        "skills": ["skills/ticket-triage"],
        "knowledge_bases": [{"kb_id": f"KB1LAB{n:02}",
                             "name": f"anypharma-it-helpdesk-cn-{student}"}],
    }


def student_row(student, n, *, staff=None):
    staff = staff or staff_name(n)
    kb_id, ds_id = f"KB1LAB{n:02}", f"DS1LAB{n:02}"
    return {
        "staff": {"username": staff, "id": f"u-{staff}",
                  "credential_parameter": f"/launchpad/itsm-workshop/users/{staff}",
                  "ownership": "created"},
        "kb": {"kb_id": kb_id, "ds_id": ds_id, "name": f"anypharma-it-helpdesk-cn-{student}",
               "data_sources": [{"ds_id": ds_id, "bucket": BUCKET,
                                 "prefix": f"kb/{kb_id}/{ds_id}/"}]},
        "datasets": {
            f"itsm-exam-8-cn-{student}": {"id": f"ds-exam-{n}", "created_by": student},
            f"itsm-held-out-cn-{student}": {"id": f"ds-held-{n}", "created_by": student},
        },
        "agents": {
            "console": {"id": f"ag-console-{n}", "name": f"{student}-itdesk-console-cn",
                        "created_by": staff},
            "eval": {"id": f"ag-eval-{n}", "name": f"{student}-itdesk-eval-cn",
                     "created_by": student, "spec": kit_spec(student, n)},
        },
        "ownership": "created",
        "prepared_at": "2026-11-01T00:00:00Z",
        "card_path": f"cards/{student}.md",
    }


class FakeS3:
    def __init__(self, objects: dict[str, set[str]]):
        self.objects = objects  # bucket → keys
        self.deleted: list[str] = []
        self.calls: list[str] = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                s3.calls.append(f"list {Bucket}/{Prefix}")
                keys = sorted(k for k in s3.objects.get(Bucket, ()) if k.startswith(Prefix))
                yield {"Contents": [{"Key": k} for k in keys[:2]]}
                yield {"Contents": [{"Key": k} for k in keys[2:]]}

        return Paginator()

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.calls.append(f"delete {Bucket} {len(keys)}")
        for k in keys:
            self.objects[Bucket].discard(k)
            self.deleted.append(k)
        return {"Deleted": [{"Key": k} for k in keys]}


class FakeConsole:
    """The live state the admin session sees; mutations are applied and recorded."""

    def __init__(self):
        self.calls: list[tuple[str, str, object]] = []
        self.agents = {
            REFERENCE_AGENT_ID: {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn",
                                 "status": "active", "version": 2, "created_by_name": "itdesk01"},
            "ag-sys": {"id": "ag-sys", "name": "architect", "status": "active", "version": 1,
                       "created_by_name": None, "system": {"key": "architect"}},
        }
        self.redeploys: dict[str, list[dict]] = {}
        self.runs: dict[str, dict] = {}
        self.recommendations: dict[str, list[dict]] = {}
        self.kbs = {SHARED_KB_ID: {"kb_id": SHARED_KB_ID, "name": "anypharma-it-helpdesk-cn"}}
        self.documents: dict[str, list[str]] = {}  # kb_id → names
        self.jobs: dict[str, list[dict]] = {}
        self.syncs: list[tuple[str, dict]] = []
        self.users = {name: {"id": uid, "username": name, "role": "admin",
                             "expires_at": None} for name, uid in ORGANIZER_IDS.items()}
        self.actors: dict[str, list[dict]] = {}  # actor_id → records
        self.policies = {
            "p-agents": {"id": "p-agents", "name": "itsm_agents_call_tools",
                         "enforcement_mode": "active"},
            "p-req": {"id": "p-req", "name": "itsm_requester_is_self",
                      "enforcement_mode": "active"},  # chapter 4 switched it on
            "p-publish": {"id": "p-publish", "name": "itsm_kb_publish_servicedesk_only",
                          "enforcement_mode": "log_only"},  # somebody relaxed it
        }
        self.sessions: dict[str, list[dict]] = {}
        self.memory_status = 200
        # the model catalog every picker reads (`GET /api/capabilities.models`, N17-obs557)
        self.catalog = [
            {"model_id": "global.zai.glm-5.3", "provider": "bedrock", "tools": True,
             "available": True, "enabled": True},
            {"model_id": "litellm.gpt-oss-120b", "provider": "litellm", "tools": True,
             "available": True, "enabled": True},
            {"model_id": "litellm.judge-only", "provider": "litellm", "tools": False,
             "available": True, "enabled": True},
            {"model_id": "global.unconfigured.model", "provider": "other", "tools": True,
             "available": False, "enabled": True},
        ]

    def add_student(self, student, n, *, staff=None, employee=True, sim=False, s3=None):
        staff = staff or staff_name(n)
        for user in (student, staff):
            self.users.setdefault(user, {"id": f"u-{user}", "username": user, "role": "member",
                                         "expires_at": "2026-11-04T00:00:00+08:00"})
        kb_id, ds_id = f"KB1LAB{n:02}", f"DS1LAB{n:02}"
        self.kbs[kb_id] = {"kb_id": kb_id, "name": f"anypharma-it-helpdesk-cn-{student}",
                           "data_sources": [{"ds_id": ds_id}]}
        self.documents[kb_id] = [*BASE_DOCS, "KB0010001.md", "KB0010007.md"]
        self.jobs[kb_id] = [{"job_id": "job-old", "status": "COMPLETE",
                             "created_at": "2026-11-01T00:00:00Z"}]
        if s3 is not None:
            prefix = f"kb/{kb_id}/{ds_id}/"
            s3.objects.setdefault(BUCKET, set()).update({
                *(prefix + d for d in BASE_DOCS),
                prefix + "KB0010001.md", prefix + "KB0010007.md",
                prefix + "KB001.md",                    # not an article id
                prefix + "drafts/KB0010009.md",         # nested: not published
                f"kb/{kb_id}/OTHERDS/KB0010003.md",      # another data source
            })
        for kind, name, creator in (("console", f"{student}-itdesk-console-cn", staff),
                                    ("eval", f"{student}-itdesk-eval-cn", student)):
            aid = f"ag-{kind}-{n}"
            self.agents[aid] = {"id": aid, "name": name, "status": "active", "version": 3,
                                "created_by_name": creator}
        self.sessions[f"ag-console-{n}"] = [{"session_id": f"s-{i}"} for i in range(4)]
        if employee:
            self.agents[f"ag-emp-{n}"] = {"id": f"ag-emp-{n}", "name": f"{student}-itdesk-cn",
                                          "status": "active", "version": 5,
                                          "created_by_name": student}
            self.actors[f"ag-emp-{n}__{student}"] = [{"record_id": f"rec-{n}-1"},
                                                     {"record_id": f"rec-{n}-2"}]
            self.sessions[f"ag-emp-{n}"] = [{"session_id": f"e-{i}"} for i in range(19)]
        if sim:
            self.agents[f"ag-sim-{n}"] = {"id": f"ag-sim-{n}", "name": f"{student}-sim-cn",
                                          "status": "active", "version": 1,
                                          "created_by_name": "workshop-admin"}
            self.agents[f"ag-extra-{n}"] = {"id": f"ag-extra-{n}", "name": "scratch-agent",
                                            "status": "active", "version": 1,
                                            "created_by_name": staff}
        self.runs[f"run-{n}-done"] = {"id": f"run-{n}-done", "name": f"{student}-baseline-A",
                                      "status": "completed", "agent_id": f"ag-eval-{n}"}
        self.runs[f"run-{n}-active"] = {"id": f"run-{n}-active", "name": f"{student}-heldout",
                                        "status": "evaluating", "agent_id": f"ag-eval-{n}"}
        self.recommendations[f"run-{n}-done"] = [
            {"id": f"rec-{n}-a", "status": "completed", "accepted": {"at": "2026-11-05"}},
            {"id": f"rec-{n}-b", "status": "completed", "accepted": None},
        ]

    # ----- the fake HTTP surface -----------------------------------------------------
    def request(self, method, url, json=None, timeout=None, **kw):
        path = url.replace("https://console.example.org", "")
        self.calls.append((method, path, json))
        parts = urlsplit(path)
        q = {k: v[0] for k, v in parse_qs(parts.query).items()}
        segs = parts.path.strip("/").split("/")
        status, body = self.dispatch(method, segs, q, json)
        return FakeResponse(status, body)

    def dispatch(self, method, segs, q, body):  # noqa: C901 — one flat table of routes
        if segs == ["api", "capabilities"] and method == "GET":
            return 200, {"features": [], "models": list(self.catalog)}
        if segs[:2] == ["api", "agents"]:
            if len(segs) == 2 and method == "GET":
                return 200, {"agents": [a for a in self.agents.values()
                                        if a["status"] != "deleted"]}
            aid = segs[2]
            agent = self.agents.get(aid)
            if agent is None or agent["status"] == "deleted":
                return 404, {"code": "agent.not_found"}
            if len(segs) == 4 and segs[3] == "redeploy" and method == "POST":
                if agent.get("system"):
                    return 409, {"code": "agent.system_managed"}
                if any(r["agent_id"] == aid and r["status"] == "evaluating"
                       for r in self.runs.values()):
                    return 409, {"code": "agent.in_use"}
                assert body["name"] == agent["name"], "name is immutable"
                self.redeploys.setdefault(aid, []).append(body)
                agent["status"] = "deploying"
                agent["spec"] = body
                return 202, {"agent": {**agent}, "job_id": f"job-{aid}-{len(self.redeploys[aid])}"}
            if method == "GET":
                if agent["status"] == "deploying":  # the next poll sees it active
                    agent["status"] = "active"
                    agent["version"] += 1
                    self.sessions[aid] = []
                    return 200, {"agent": {**agent, "status": "deploying"}}
                return 200, {"agent": agent}
            if method == "DELETE":
                if agent.get("system"):
                    return 409, {"code": "agent.system_managed"}
                agent["status"] = "deleted"
                self.actors = {a: r for a, r in self.actors.items() if not a.startswith(aid + "__")}
                return 200, {"deleted": True, "agent_id": aid, "removed": ["runtime"]}
        if segs[:3] == ["api", "eval", "runs"]:
            if len(segs) == 3:
                rows = [r for r in self.runs.values()
                        if not q.get("agent_id") or r["agent_id"] == q["agent_id"]]
                return 200, {"runs": rows, "total": len(rows)}
            run = self.runs.get(segs[3])
            if run is None:
                return 404, {"code": "run.not_found"}
            if len(segs) == 5 and segs[4] == "stop":
                run["status"] = "stopped"
                return 202, run
            if len(segs) == 5 and segs[4] == "recommendations" and method == "GET":
                return 200, {"recommendations": self.recommendations.get(segs[3], [])}
        if segs[:2] == ["api", "knowledge-bases"]:
            kb = self.kbs.get(segs[2])
            if kb is None:
                return 404, {"code": "kb.not_found"}
            if len(segs) == 3 and method == "GET":
                return 200, kb
            if segs[3] == "data-sources" and len(segs) == 6:
                kb_id, ds_id, leaf = segs[2], segs[4], segs[5]
                if leaf == "documents" and method == "GET":
                    docs = [{"name": n, "status": "INDEXED"} for n in self.documents[kb_id]]
                    return 200, {"documents": docs, "next_token": None}
                if leaf == "sync" and method == "POST":
                    assert (body or {}).get("full") is True, "the reset needs a FULL rebuild"
                    self.syncs.append((kb_id, body))
                    # the rebuild drops the vectors of the deleted objects
                    live = {k.rsplit("/", 1)[-1] for k in self.s3.objects.get(BUCKET, ())
                            if k.startswith(f"kb/{kb_id}/{ds_id}/") and "/" not in
                            k[len(f"kb/{kb_id}/{ds_id}/"):]}
                    self.documents[kb_id] = sorted(d for d in self.documents[kb_id] if d in live)
                    self.jobs[kb_id].append({"job_id": "job-rebuild", "status": "COMPLETE",
                                             "created_at": "2026-11-07T00:00:00Z"})
                    return 202, {"job_id": "job-rebuild", "full": True}
                if leaf == "ingestion-jobs" and method == "GET":
                    return 200, {"items": list(self.jobs[kb_id])}
        if segs[:2] == ["api", "memory"]:
            if self.memory_status != 200:
                return self.memory_status, {"code": "feature.unavailable"}
            if segs[2] == "namespaces":
                if q["actor_id"] not in self.actors:
                    return 200, {"items": []}
                return 200, {"items": [
                    {"namespace": f"/summaries/{q['actor_id']}", "strategy_id": "summ",
                     "resolvable": True},
                    {"namespace": f"/facts/{q['actor_id']}", "strategy_id": "facts",
                     "resolvable": True},
                    {"namespace": "/broken", "strategy_id": "x", "resolvable": False},
                ]}
            if segs[2] == "records" and len(segs) == 3 and method == "GET":
                records = self.actors.get(q["actor_id"])
                if records is None:
                    return 400, {"code": "memory.namespace_required"}
                strategy = q["namespace"].split("/")[1]
                mine = [r for r in records if r.get("strategy", "summaries") == strategy]
                return 200, {"items": mine, "next_token": None}
            if segs[2] == "records" and method == "DELETE":
                records = self.actors.get(q.get("actor_id"), [])
                before = len(records)
                records[:] = [r for r in records if r["record_id"] != segs[3]]
                return (200, {"deleted": True}) if len(records) < before else (404, {})
        if segs[:2] == ["api", "users"]:
            if len(segs) == 2:
                rows = list(self.users.values())
                return 200, {"items": rows, "total": len(rows)}
            if method == "PATCH":
                hit = next((u for u in self.users.values() if u["id"] == segs[2]), None)
                if hit is None:
                    return 404, {"code": "users.not_found"}
                assert set(body) == {"expires_at"}, body
                hit["expires_at"] = body["expires_at"].replace("Z", "+00:00")
                return 200, {**hit}
        if segs[:2] == ["api", "policy-engines"] and segs[2] == "eng-1" and segs[3] == "policies":
            policy = self.policies.get(segs[4])
            if policy is None:
                return 404, {"code": "policy.not_found"}
            if len(segs) == 5 and method == "GET":
                return 200, policy
            if len(segs) == 6 and segs[5] == "enforcement-mode" and method == "PUT":
                policy["enforcement_mode"] = body["enforcement_mode"]
                return 200, policy
        if segs[:2] == ["api", "chat"] and segs[3] == "sessions" and method == "GET":
            return 200, {"sessions": self.sessions.get(segs[2], [])}
        raise AssertionError(f"unexpected call {method} {'/'.join(segs)} {q}")

    @property
    def mutations(self):
        return [(m, p) for m, p, _ in self.calls if m != "GET"]


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def stub_lab(root, console, manifest, s3, *, staff_accounts=None):
    stub = types.ModuleType("lab")
    stub.ROOT = root
    stub.BASE = "https://console.example.org"
    stub.PUBLIC = stub.BASE
    stub.PREFIX = "/launchpad/itsm-workshop/"
    stub.NAME = "launchpad-itsm-workshop"
    stub.SCENARIO = "itsm-workshop"
    stub.CONFIG_DIGEST = "digest"
    # N17-obs559: reset_lab builds its run-marker guard from these
    stub.LEGACY_CONFIG_DIGEST = "legacy-digest"
    stub.legacy_config_digest = lambda model_id: f"legacy-{model_id}"
    stub.run_marker_guard = lambda path, *, legacy_digests=(): (
        f"# guard {path} {stub.CONFIG_DIGEST} legacy={sorted(legacy_digests)}")
    stub.sync_assets = lambda: []  # N17-obs558
    stub.CONFIG = {"workspace": "default", "model_id": "global.zai.glm-5.3",
                   "admin_username": "workshop-admin", "tools_host": "i-0123456789abcdef0",
                   "tools_user": "launchpad",
                   "staff_accounts": staff_accounts or ["itdesk01", "itdesk02", "itdesk03"]}
    stub.logins = []
    stub.remote_calls = []
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

    def remote(script, label, timeout=300):
        stub.remote_calls.append((label, script))
        return {"CommandId": "cmd", "Status": "Success"}

    class AWS:
        @staticmethod
        def client(name):
            assert name == "s3"
            return s3

    stub.evidence, stub.session, stub.remote, stub.AWS = evidence, session, remote, AWS
    (root / "lab-assets").mkdir(exist_ok=True)
    shutil.copy(SAMPLE / "lab-assets/prompts.json", root / "lab-assets/prompts.json")
    return stub


def manifest_with(students: dict[str, int], **extra):
    rows = {s: student_row(s, n) for s, n in students.items()}
    users = {"itdesk01": {"id": "u-itdesk01"}}
    for s, n in students.items():
        users[s] = {"id": f"u-{s}"}
        users[staff_name(n)] = {"id": f"u-{staff_name(n)}"}
    manifest = {
        "model_id": "global.zai.glm-5.3", "workspace": "default",
        "kb": {"kb_id": SHARED_KB_ID, "name": "anypharma-it-helpdesk-cn",
               "data_sources": [{"ds_id": "DSSHARED", "bucket": BUCKET,
                                 "prefix": f"kb/{SHARED_KB_ID}/DSSHARED/"}]},
        "datasets": {"itsm-exam-8-cn": {"id": "ds-shared-exam"}},
        "agents": {"itdesk01": {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn"},
                   **{s: {"id": f"ag-emp-{n}", "name": f"{s}-itdesk-cn", "stage": 1}
                      for s, n in students.items()}},
        "users": users, "students": rows,
        "service": {"host": "i-0123456789abcdef0",
                    "path": "/srv/launchpad-demo/scenarios/itsm-workshop",
                    "unit": "launchpad-itsm-lab.service"},
        "engine": {"id": "eng-1", "name": "itsm_cn_lab_engine"},
        "policies": {"itsm_agents_call_tools": {"id": "p-agents"},
                     "itsm_requester_is_self": {"id": "p-req"},
                     "itsm_kb_publish_servicedesk_only": {"id": "p-publish"}},
    }
    manifest.update(extra)
    return manifest


def load_world(tmp_path, monkeypatch, console, manifest, s3):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    stub = stub_lab(tmp_path, console, manifest, s3)
    console.s3 = s3
    monkeypatch.setitem(sys.modules, "lab", stub)
    for name in ("reset_lab", "student_kit", "cleanup_students"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    (tmp_path / "cards").mkdir()
    for s in manifest["students"]:
        (tmp_path / "cards" / f"{s}.md").write_text("old card\n")
    rs = module(SCRIPTS / "reset_student.py")
    monkeypatch.setattr(rs.time, "sleep", lambda s: None)
    return rs, stub


@pytest.fixture
def world(tmp_path, monkeypatch):
    """``reset_student`` loaded against a stub ``lab``; lab01 (with a sim leftover) and
    lab03 provisioned, the KB prefixes populated in a fake S3."""
    console, s3 = FakeConsole(), FakeS3({})
    console.add_student("lab01", 1, sim=True, s3=s3)
    console.add_student("lab03", 3, s3=s3)
    manifest = manifest_with({"lab01": 1, "lab03": 3})
    rs, stub = load_world(tmp_path, monkeypatch, console, manifest, s3)
    return rs, stub, console, s3


def manifest_of(stub):
    return json.loads((stub.ROOT / "manifest.json").read_text())


# ----- pure helpers --------------------------------------------------------------------
def test_kb_object_filter_matches_only_published_articles(world):
    rs = world[0]
    prefix = "kb/KB1LAB01/DS1LAB01/"
    keys = [prefix + "KB0010001.md", prefix + "KB0010123.md", prefix + "01-QDocs权限申请.md",
            prefix + "KB001.md", prefix + "KB00100012.md", prefix + "KB0010001.txt",
            prefix + "drafts/KB0010009.md", "kb/KB1LAB01/OTHER/KB0010003.md",
            "kb/KBSHARED/DS/KB0010004.md", prefix + "kb0010001.md"]
    assert rs.article_keys(keys, prefix) == [prefix + "KB0010001.md", prefix + "KB0010123.md"]
    assert rs.ARTICLE_RE.pattern == r"^KB\d{7}\.md$"


def test_memory_actor_is_agent_id_double_underscore_user(world):
    rs = world[0]
    assert rs.memory_actor("ag-emp-1", "lab01") == "ag-emp-1__lab01"
    assert rs.memory_actor("abc123", "itdesk11") == "abc123__itdesk11"


def test_stage1_spec_is_the_scenario_one_baseline_and_a_valid_request_body():
    from app.schemas.agent import AgentSpecRequest

    lib = module(SCRIPTS / "student_kit_lib.py")
    prompts = json.loads((SAMPLE / "lab-assets/prompts.json").read_text())
    spec = lib.stage1_agent_spec("lab01-itdesk-cn", model_id="global.zai.glm-5.3",
                                 prompts=prompts)
    assert spec["system_prompt"] == prompts["step1"]
    assert spec["tools"] == [] and spec["skills"] == [] and spec["knowledge_bases"] == []
    assert spec["native_tools"] == [] and spec["policy_ids"] == []
    assert spec["memory"] == {"short_term": True, "long_term": False, "memory_id": None}
    parsed = AgentSpecRequest(**spec)
    assert parsed.model_id == "global.zai.glm-5.3" and parsed.memory.long_term is False


def test_kb_location_prefers_the_recorded_data_source(world):
    rs = world[0]
    row = student_row("lab01", 1)["kb"]
    assert rs.kb_location(row, {}) == (BUCKET, "kb/KB1LAB01/DS1LAB01/")
    bare = {"kb_id": "KBX", "ds_id": "DSX"}
    shared = {"kb": {"data_sources": [{"bucket": "shared-bucket"}]}}
    assert rs.kb_location(bare, shared) == ("shared-bucket", "kb/KBX/DSX/")


# ----- the CLI -------------------------------------------------------------------------
def test_dry_run_prints_the_plan_and_mutates_nothing(world, capsys):
    rs, stub, console, s3 = world
    assert rs.main(["--student", "lab01"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing is changed" in out
    assert console.mutations == [], "a dry run only reads"
    assert s3.deleted == [] and not any(c.startswith("delete") for c in s3.calls)
    assert stub.remote_calls == [] and stub.logins == [None]
    for text in ("WOULD stop id=run-1-active",
                 "WOULD redeploy target=lab01-itdesk-cn id=ag-emp-1",
                 "POST /api/agents/ag-emp-1/redeploy",
                 'memory={"long_term": false, "memory_id": null, "short_term": true}',
                 "WOULD redeploy target=lab01-itdesk-eval-cn id=ag-eval-1",
                 "WOULD delete target=lab01-sim-cn", "WOULD delete target=scratch-agent",
                 "DELETE /api/memory/records/rec-1-1?actor_id=ag-emp-1__lab01",
                 f"WOULD delete object=s3://{BUCKET}/kb/KB1LAB01/DS1LAB01/KB0010001.md",
                 f"WOULD delete object=s3://{BUCKET}/kb/KB1LAB01/DS1LAB01/KB0010007.md",
                 "WOULD sync kb_id=KB1LAB01 ds_id=DS1LAB01 full=True",
                 "WOULD reset ALL tickets", "unit=launchpad-itsm-lab.service",
                 "WOULD set name=itsm_kb_publish_servicedesk_only mode=log_only→active",
                 "PUT /api/policy-engines/eng-1/policies/p-publish/enforcement-mode",
                 "WOULD extend username=lab01", "WOULD extend username=itdesk11",
                 "completed evaluation runs remain", "run-1-done(lab01-baseline-A)",
                 "chat sessions on the console Agent", "count=4",
                 "accepted recommendation(s) remain", "WOULD write path=cards/lab01.md"):
        assert text in out, text
    assert "KB001.md" not in out and "drafts/KB0010009.md" not in out
    assert "OTHERDS" not in out
    # lab03 is not touched by a lab01 run
    assert not any("lab03" in p or "-3" in p for _, p, _ in console.calls), console.calls
    # the ledger and the card are untouched
    assert manifest_of(stub) == manifest_with({"lab01": 1, "lab03": 3})
    assert (stub.ROOT / "cards" / "lab01.md").read_text() == "old card\n"
    assert console.agents["ag-emp-1"]["version"] == 5
    assert console.policies["p-req"]["enforcement_mode"] == "active"


def test_apply_resets_in_the_manual_order(world, capsys):
    rs, stub, console, s3 = world
    assert rs.main(["--student", "lab01", "--apply", "--extend-days", "14"]) == 0
    muts = console.mutations
    paths = [p for _, p in muts]
    # 0. the active run is stopped before any redeploy
    assert muts[0] == ("POST", "/api/eval/runs/run-1-active/stop")
    emp = paths.index("/api/agents/ag-emp-1/redeploy")
    ev = paths.index("/api/agents/ag-eval-1/redeploy")
    sim = paths.index("/api/agents/ag-sim-1")
    extra = paths.index("/api/agents/ag-extra-1")
    mem = min(i for i, p in enumerate(paths) if p.startswith("/api/memory/records/"))
    sync = paths.index("/api/knowledge-bases/KB1LAB01/data-sources/DS1LAB01/sync")
    policy = paths.index("/api/policy-engines/eng-1/policies/p-publish/enforcement-mode")
    users = min(i for i, p in enumerate(paths) if p.startswith("/api/users/"))
    assert 0 < emp < ev < min(sim, extra) < mem < sync < policy < users
    # 1. the employee Agent is back at stage 1 (model from the manifest, prompt = step1)
    prompts = json.loads((SAMPLE / "lab-assets/prompts.json").read_text())
    [emp_spec] = console.redeploys["ag-emp-1"]
    assert emp_spec["system_prompt"] == prompts["step1"]
    assert emp_spec["model_id"] == "global.zai.glm-5.3"
    assert emp_spec["memory"] == {"short_term": True, "long_term": False, "memory_id": None}
    assert emp_spec["tools"] == emp_spec["skills"] == emp_spec["knowledge_bases"] == []
    assert console.agents["ag-emp-1"]["status"] == "active"
    assert console.agents["ag-emp-1"]["version"] == 6
    # 2. the evaluation Agent is back at the kit spec with the manifest model
    [eval_spec] = console.redeploys["ag-eval-1"]
    assert eval_spec["system_prompt"] == "KIT BASELINE PROMPT"
    assert eval_spec["model_id"] == "global.zai.glm-5.3" and eval_spec["model_id"] != "old-model"
    assert eval_spec["tools"] == kit_spec("lab01", 1)["tools"]
    assert console.agents["ag-eval-1"]["version"] == 4
    # 3. leftovers gone, the kit's console Agent and the base untouched
    assert console.agents["ag-sim-1"]["status"] == "deleted"
    assert console.agents["ag-extra-1"]["status"] == "deleted"
    assert "ag-console-1" not in console.redeploys
    assert console.agents["ag-console-1"]["status"] == "active"
    assert console.agents[REFERENCE_AGENT_ID]["status"] == "active"
    assert console.agents["ag-sys"]["status"] == "active"
    # 4. memory records of <employee>__lab01, both strategies, deleted one by one
    assert "/api/memory/records/rec-1-1?actor_id=ag-emp-1__lab01" in paths
    assert "/api/memory/records/rec-1-2?actor_id=ag-emp-1__lab01" in paths
    assert console.actors["ag-emp-1__lab01"] == []
    namespaces = [p for m, p, _ in console.calls if p.startswith("/api/memory/namespaces")]
    assert "/api/memory/namespaces?actor_id=ag-emp-1__lab01" in namespaces
    assert "/api/memory/namespaces?actor_id=ag-eval-1__lab01" in namespaces
    # 5. only the two articles left S3; a full rebuild ran; 5 base documents remain
    assert sorted(s3.deleted) == ["kb/KB1LAB01/DS1LAB01/KB0010001.md",
                                  "kb/KB1LAB01/DS1LAB01/KB0010007.md"]
    assert "kb/KB1LAB01/DS1LAB01/KB001.md" in s3.objects[BUCKET]
    assert "kb/KB1LAB01/DS1LAB01/drafts/KB0010009.md" in s3.objects[BUCKET]
    assert "kb/KB1LAB01/OTHERDS/KB0010003.md" in s3.objects[BUCKET]
    assert not any(k.startswith(f"kb/{SHARED_KB_ID}/") for k in s3.deleted)
    assert console.syncs == [("KB1LAB01", {"full": True})]
    assert console.documents["KB1LAB01"] == sorted(BASE_DOCS)
    assert console.documents["KB1LAB03"] == [*BASE_DOCS, "KB0010001.md", "KB0010007.md"]
    # 6. the synthetic service was reset through reset_lab's remote script
    assert [label for label, _ in stub.remote_calls] == ["reset-business"]
    script = stub.remote_calls[0][1]
    assert "systemctl stop" in script and "service.py --reset" in script
    assert "launchpad-itsm-lab.service" in script
    # 7. every lab rule active, the requester rule included (N17-obs548)
    assert console.policies["p-req"]["enforcement_mode"] == "active"
    assert console.policies["p-publish"]["enforcement_mode"] == "active"
    assert console.policies["p-agents"]["enforcement_mode"] == "active"
    assert "/api/policy-engines/eng-1/policies/p-agents/enforcement-mode" not in paths
    # 8. both accounts expire 14 days from now; nobody else was patched
    patched = [p for p in paths if p.startswith("/api/users/")]
    assert sorted(patched) == ["/api/users/u-itdesk11", "/api/users/u-lab01"]
    from datetime import UTC, datetime
    for name in ("lab01", "itdesk11"):
        until = datetime.fromisoformat(console.users[name]["expires_at"])
        assert 13.9 < (until - datetime.now(UTC)).total_seconds() / 86400 < 14.1
    assert console.users["lab03"]["expires_at"] == "2026-11-04T00:00:00+08:00"
    # 9. report, evidence, manifest row and card
    out = capsys.readouterr().out
    assert "run-1-done(lab01-baseline-A)" in out and "cannot" in out
    assert "accepted recommendation(s) remain" in out and "runs=run-1-done" in out
    assert "remaining documents as expected count=5 expected=5" in out
    assert "chat sessions on the console Agent" in out and "count=4" in out
    assert "extended username=lab01" in out and "extended username=itdesk11" in out
    evidence = sorted((stub.ROOT / "evidence").glob("reset-lab01-*.json"))
    assert len(evidence) == 1
    record = json.loads(evidence[0].read_text())
    assert record["mode"] == "apply" and record["failures"] == []
    assert record["report"]["completed_runs"] == [
        {"id": "run-1-done", "name": "lab01-baseline-A", "agent_id": "ag-eval-1"}]
    assert record["report"]["kb"]["documents_after"] == 5
    assert record["report"]["versions"] == {"lab01-itdesk-cn": 6, "lab01-itdesk-eval-cn": 4}
    assert set(record["report"]["expires_at"]) == {"lab01", "itdesk11"}
    assert console.users["itdesk01"]["expires_at"] is None, "organizer account untouched"
    m = manifest_of(stub)
    assert m["students"]["lab01"]["last_reset"]["evidence"] == f"evidence/{evidence[0].name}"
    assert m["students"]["lab01"]["card_path"] == "cards/lab01.md"
    assert m["students"]["lab03"] == student_row("lab03", 3), "other students untouched"
    card = (stub.ROOT / "cards" / "lab01.md").read_text()
    assert card != "old card\n" and "lab01-itdesk-eval-cn" in card and "KB1LAB01" in card
    assert (stub.ROOT / "cards" / "lab03.md").read_text() == "old card\n"


def test_skip_tickets_leaves_the_service_alone(world, capsys):
    rs, stub, console, s3 = world
    assert rs.main(["--student", "lab01", "--apply", "--skip-tickets"]) == 0
    assert stub.remote_calls == []
    out = capsys.readouterr().out
    assert "[tickets] skipped (--skip-tickets)" in out
    assert "NOTE: the ticket reset covers ALL" not in out
    # the rest still happened
    assert console.redeploys["ag-emp-1"] and console.syncs
    assert console.policies["p-publish"]["enforcement_mode"] == "active"


def test_employee_agent_delete_mode_removes_it_instead_of_redeploying(world, capsys):
    rs, stub, console, s3 = world
    assert rs.main(["--student", "lab01", "--apply", "--employee-agent", "delete"]) == 0
    paths = [p for _, p in console.mutations]
    assert "/api/agents/ag-emp-1" in paths and "/api/agents/ag-emp-1/redeploy" not in paths
    assert console.agents["ag-emp-1"]["status"] == "deleted"
    # the eval Agent is still rebuilt; the employee's records went with its Agent
    assert console.redeploys["ag-eval-1"]
    assert not any("ag-emp-1__lab01" in p for p in paths)
    assert "ag-emp-1__lab01" not in console.actors
    out = capsys.readouterr().out
    assert "student-created; delete" in out and "reason=--employee-agent delete" in out


def test_manifest_prebuilt_false_means_delete_and_absent_means_nothing(tmp_path, monkeypatch,
                                                                       capsys):
    console, s3 = FakeConsole(), FakeS3({})
    console.add_student("lab01", 1, s3=s3)          # employee Agent exists
    console.add_student("lab03", 3, employee=False, s3=s3)  # never created
    manifest = manifest_with({"lab01": 1, "lab03": 3})
    manifest["students"]["lab01"]["agents"]["employee"] = {
        "id": "ag-emp-1", "name": "lab01-itdesk-cn", "prebuilt": False}
    rs, stub = load_world(tmp_path, monkeypatch, console, manifest, s3)
    assert rs.main(["--apply", "--skip-tickets"]) == 0  # every student
    paths = [p for _, p in console.mutations]
    assert "/api/agents/ag-emp-1" in paths and "/api/agents/ag-emp-1/redeploy" not in paths
    assert not any(p.startswith("/api/agents/ag-emp-3") for p in paths)
    out = capsys.readouterr().out
    assert "reason=manifest: prebuilt false" in out
    assert "[employee] absent (the student creates it in scenario one) name=lab03-itdesk-cn" in out
    assert "== lab01" in out and "== lab03" in out
    assert console.redeploys["ag-eval-1"] and console.redeploys["ag-eval-3"]
    assert sorted(stub.ROOT.glob("evidence/reset-lab0*.json"))


def test_unknown_student_is_refused_with_exit_2(world, capsys):
    rs, stub, console, s3 = world
    assert rs.main(["--student", "lab05", "--apply"]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and "lab05" in err
    assert console.calls == [] and stub.logins == [] and stub.remote_calls == []
    assert manifest_of(stub) == manifest_with({"lab01": 1, "lab03": 3})
    for argv in (["--student", "lab11"], ["--student", "itdesk01"],
                 ["--student", "lab01", "--extend-days", "0"],
                 ["--student", "lab01", "--extend-days", "4000"],
                 ["--student", "lab01", "--employee-agent", "rebuild"],
                 ["--dry-run", "--apply"]):
        with pytest.raises(SystemExit):
            rs.parse_args(argv)


def test_protected_names_are_never_touched_even_when_a_row_names_them(tmp_path, monkeypatch,
                                                                      capsys):
    console, s3 = FakeConsole(), FakeS3({})
    console.add_student("lab03", 3, s3=s3)
    manifest = manifest_with({"lab03": 3})
    row = manifest["students"]["lab03"]
    # a corrupted row pointing at the base: the shared KB, the reference Agent as the
    # employee Agent, itdesk01 as the staff account
    row["kb"] = {"kb_id": SHARED_KB_ID, "ds_id": "DSSHARED", "name": "anypharma-it-helpdesk-cn"}
    row["agents"]["employee"] = {"id": REFERENCE_AGENT_ID, "name": "itdesk-console-cn"}
    row["agents"]["eval"]["id"] = "ag-sys"
    row["agents"]["eval"]["name"] = "architect"
    row["staff"] = {"username": "itdesk01", "id": "u-itdesk01", "ownership": "created"}
    s3.objects[BUCKET].add(f"kb/{SHARED_KB_ID}/DSSHARED/KB0010002.md")
    rs, stub = load_world(tmp_path, monkeypatch, console, manifest, s3)
    assert rs.main(["--student", "lab03", "--apply", "--skip-tickets"]) == 0
    paths = [p for _, p in console.mutations]
    assert not any(p.startswith(f"/api/agents/{REFERENCE_AGENT_ID}") for p in paths)
    assert not any(p.startswith("/api/agents/ag-sys") for p in paths)
    assert not any(SHARED_KB_ID in p for p in paths)
    assert "/api/users/u-itdesk01" not in paths
    assert console.agents[REFERENCE_AGENT_ID]["version"] == 2
    assert console.agents["ag-sys"]["version"] == 1
    assert s3.deleted == [] and console.syncs == []
    assert console.users["itdesk01"]["expires_at"] is None
    out = capsys.readouterr().out
    assert "PROTECTED (reference Agent)" in out and "PROTECTED (shared knowledge base)" in out
    assert "PROTECTED (system preset)" in out and "PROTECTED (organizer account)" in out
    # lab03's own account was still extended and its real console Agent reported
    assert "/api/users/u-lab03" in paths
    assert "chat sessions on the console Agent" in out


def test_memory_feature_missing_is_skipped_not_failed(world, capsys):
    rs, stub, console, s3 = world
    console.memory_status = 503
    assert rs.main(["--student", "lab01", "--apply", "--skip-tickets"]) == 0
    out = capsys.readouterr().out
    assert "[memory] skipped actor_id=ag-emp-1__lab01 reason=feature.unavailable" in out
    assert not any(p.startswith("/api/memory/records/") for _, p in console.mutations)


# ----- the model switch (N17-obs557) ---------------------------------------------------
def console_kit_spec(student, n):
    return {**kit_spec(student, n), "name": f"{student}-itdesk-console-cn",
            "system_prompt": "KIT CONSOLE PROMPT"}


@pytest.fixture
def switched(tmp_path, monkeypatch):
    """lab01 provisioned with a recorded console spec; the lab config now says gpt-oss while
    the manifest copy still says glm."""
    console, s3 = FakeConsole(), FakeS3({})
    console.add_student("lab01", 1, s3=s3)
    manifest = manifest_with({"lab01": 1})
    manifest["students"]["lab01"]["agents"]["console"]["spec"] = console_kit_spec("lab01", 1)
    rs, stub = load_world(tmp_path, monkeypatch, console, manifest, s3)
    stub.CONFIG["model_id"] = "litellm.gpt-oss-120b"
    return rs, stub, console, s3


def test_model_switch_dry_run_plans_all_three_redeploys_and_writes_nothing(switched, capsys):
    rs, stub, console, s3 = switched
    assert rs.main(["--student", "lab01"]) == 0
    out = capsys.readouterr().out
    assert ("model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b (from config; dry run,"
            " manifest not written)") in out
    for text in ("WOULD redeploy target=lab01-itdesk-cn id=ag-emp-1",
                 "WOULD redeploy target=lab01-itdesk-eval-cn id=ag-eval-1",
                 "model changed; redeploy to the kit spec name=lab01-itdesk-console-cn"
                 " model=old-model→litellm.gpt-oss-120b",
                 "WOULD redeploy target=lab01-itdesk-console-cn id=ag-console-1"):
        assert text in out, text
    assert out.count("model=litellm.gpt-oss-120b") == 3, "every planned redeploy names the model"
    assert console.mutations == [] and s3.deleted == []
    assert manifest_of(stub)["model_id"] == "global.zai.glm-5.3", "a dry run writes nothing"
    assert (stub.ROOT / "cards" / "lab01.md").read_text() == "old card\n"


def test_model_switch_apply_redeploys_all_three_agents_and_rewrites_the_card(switched, capsys):
    rs, stub, console, s3 = switched
    assert rs.main(["--student", "lab01", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b (from config)" in out
    paths = [p for _, p in console.mutations]
    # the sync is written before the first redeploy; the catalog is read before the sync
    reads = [p for m, p, _ in console.calls if m == "GET"]
    assert reads[0] == "/api/capabilities"
    emp = paths.index("/api/agents/ag-emp-1/redeploy")
    ev = paths.index("/api/agents/ag-eval-1/redeploy")
    con = paths.index("/api/agents/ag-console-1/redeploy")
    mem = min(i for i, p in enumerate(paths) if p.startswith("/api/memory/records/"))
    assert emp < ev < con < mem
    for aid in ("ag-emp-1", "ag-eval-1", "ag-console-1"):
        [spec] = console.redeploys[aid]
        assert spec["model_id"] == "litellm.gpt-oss-120b", aid
        assert console.agents[aid]["status"] == "active"
    assert console.redeploys["ag-console-1"][0]["system_prompt"] == "KIT CONSOLE PROMPT"
    assert console.redeploys["ag-eval-1"][0]["system_prompt"] == "KIT BASELINE PROMPT"
    manifest = manifest_of(stub)
    assert manifest["model_id"] == "litellm.gpt-oss-120b"
    row = manifest["students"]["lab01"]["agents"]
    assert row["eval"]["spec"]["model_id"] == "litellm.gpt-oss-120b"
    assert row["console"]["spec"]["model_id"] == "litellm.gpt-oss-120b"
    card = (stub.ROOT / "cards" / "lab01.md").read_text()
    assert "| 模型 | litellm.gpt-oss-120b |" in card and "glm-5.3" not in card
    assert "chat sessions on the console Agent" in out and "count=0" in out  # rebuilt
    # a second reset finds the model in place: the console Agent keeps its sessions
    console.calls.clear()
    assert rs.main(["--student", "lab01", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "model_id:" not in out
    assert "unchanged (model matches; sessions kept) name=lab01-itdesk-console-cn" in out
    assert len(console.redeploys["ag-console-1"]) == 1
    assert len(console.redeploys["ag-emp-1"]) == 2, "the baseline redeploys still happen"


def test_console_agent_without_a_recorded_spec_is_left_alone(world, capsys):
    rs, stub, console, s3 = world
    stub.CONFIG["model_id"] = "litellm.gpt-oss-120b"
    assert rs.main(["--student", "lab01", "--apply"]) == 0
    out = capsys.readouterr().out
    assert "no kit spec recorded; model not compared" in out
    assert "ag-console-1" not in console.redeploys
    assert manifest_of(stub)["model_id"] == "litellm.gpt-oss-120b"
    assert console.redeploys["ag-emp-1"][0]["model_id"] == "litellm.gpt-oss-120b"


def test_model_override_is_one_run_only(switched, capsys):
    rs, stub, console, s3 = switched
    stub.CONFIG["model_id"] = "global.zai.glm-5.3"  # config = manifest; the run overrides
    assert rs.main(["--student", "lab01", "--apply", "--model", "litellm.gpt-oss-120b"]) == 0
    out = capsys.readouterr().out
    assert ("model_id: litellm.gpt-oss-120b for THIS run only (config / manifest / card keep"
            " global.zai.glm-5.3)") in out
    assert "(from config)" not in out
    for aid in ("ag-emp-1", "ag-eval-1", "ag-console-1"):
        assert console.redeploys[aid][0]["model_id"] == "litellm.gpt-oss-120b", aid
    assert manifest_of(stub)["model_id"] == "global.zai.glm-5.3"
    assert "| 模型 | global.zai.glm-5.3 |" in (stub.ROOT / "cards" / "lab01.md").read_text()


@pytest.mark.parametrize("model_id, phrase", [
    ("litellm.gpt-oss-20b", "not in this console's model catalog"),
    ("litellm.judge-only", "does not support tool calling"),
    ("global.unconfigured.model", "has no credential in this workspace"),
])
def test_unusable_model_is_refused_before_any_mutation_or_manifest_write(switched, capsys,
                                                                          model_id, phrase):
    rs, stub, console, s3 = switched
    stub.CONFIG["model_id"] = model_id
    assert rs.main(["--student", "lab01", "--apply"]) == 2
    err = capsys.readouterr().err
    assert "REFUSED" in err and phrase in err and model_id in err
    assert "global.zai.glm-5.3, litellm.gpt-oss-120b" in err, "the usable ids are named"
    assert console.mutations == []
    assert [p for m, p, _ in console.calls] == ["/api/capabilities"]
    assert manifest_of(stub)["model_id"] == "global.zai.glm-5.3"
    assert (stub.ROOT / "cards" / "lab01.md").read_text() == "old card\n"


def test_parse_args_takes_the_one_run_model(world):
    rs = world[0]
    assert rs.parse_args(["--student", "lab01"]).model is None
    assert rs.parse_args(["--model", "litellm.gpt-oss-120b"]).model == "litellm.gpt-oss-120b"
