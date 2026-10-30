"""E09 — data-centre pipelines as a state machine + the JSONL upload property (round 16).

**Pipelines** (`evaluation/pipeline_routers.py`): a Hypothesis ``RuleBasedStateMachine``
drives create / run / finish / run-again / restart (the dead-process sweep) / reap-on-read /
update / delete against the real routes; the background thread is captured instead of
started, so the machine decides when a run finishes, and "the process died" is a cleared
``_live_runs`` set plus the startup sweep. Invariants: a row's status only moves along
``idle|succeeded|failed → running → succeeded|failed``; a run never rewrites a previous
outcome until it ends; a pipeline creates at most ONE output dataset over its life and every
later run appends to it; the output dataset stays valid (≤ 200 items, unique scenario ids);
``added ≤ matched ≤ scanned``; a failed run names its error; a ``running`` row is reaped
to ``failed`` after a simulated dead process.

Contract sources: `evaluation-engine.md` §21a (the upload: line rule, caps, error codes — the
oracle below is written from it), §21 + the `pipeline_routers` module docstring (one output
dataset per saved task, append on later runs, a restart fails a `running` row honestly, the
reap-on-read twin). **No contract line states the pipeline status machine itself**
(`idle|running|succeeded|failed` and its transitions) — flagged in the N16 register.

**Upload** (`POST /api/eval/datasets/upload`): random JSONL — valid prompt / scenario /
persona items with arbitrary Unicode, blank lines, non-object lines, broken JSON,
duplicate scenario ids, a > 1 MB line, 201 items, a deeply nested value, an invalid UTF-8
body — against a small independent oracle: either 201 with ``item_count`` = the valid
lines, or a precise 4xx; never a 500.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    consumes,
    initialize,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from sqlalchemy import delete

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import pipeline_routers
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.routers import _validate_items
from app.main import create_app
from tests.evaluation.test_pipelines import ROWS, SESSIONS

SCALE = float(os.environ.get("LAUNCHPAD_PROPERTY_SCALE", "1"))


def examples(n: int) -> int:
    return max(5, int(n * SCALE))


TERMINAL = {"succeeded", "failed"}
STATUSES = {"idle", "running"} | TERMINAL
ALL_ROWS = ROWS + [
    {"session_id": "sess-eeeeeeee-0005", "agent": "hr-bot", "errors": 1,
     "last": "2026-09-25T12:00:00Z"},
]


# ─── the pipeline machine ────────────────────────────────────────────────────

_CLIENT: list[TestClient] = []


def _shared_client() -> TestClient:
    """One console for every machine instance (``create_app`` + ``init_db`` per example
    would dominate the run time); the machine's ``teardown`` wipes the eval tables."""
    if not _CLIENT:
        _CLIENT.append(TestClient(create_app()))
    return _CLIENT[0]


def _output(config_output: dict[str, Any]) -> dict[str, Any]:
    """The output target without the ``None`` twin pydantic's dump leaves behind."""
    return {k: v for k, v in (config_output or {}).items() if v is not None}


class PipelineMachine(RuleBasedStateMachine):
    """One console, several saved pipelines, a fake observability service whose session
    listing the machine varies between runs."""

    pipelines = Bundle("pipelines")

    def __init__(self) -> None:
        super().__init__()
        self.client = _shared_client()
        self.model: dict[str, dict[str, Any]] = {}  # id → {status, last_run, datasets, ...}
        self.pending: dict[str, Any] = {}  # id → the captured run thread body
        self.rows: list[dict[str, Any]] = list(ALL_ROWS)
        self.listing_fails = False
        self.deleted_running: set[str] = set()
        self.seed_dataset = self._seed_dataset()

    # ── fakes ──

    @staticmethod
    def _seed_dataset() -> str:
        db = SessionLocal()
        try:
            row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="seed", kind="predefined",
                              items=[{"scenario_id": "seed-1", "turns": [{"input": "seed q"}]}])
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    def _get_session(self, session_id, range_key, db, workspace, force=False, logs=None):
        if session_id not in SESSIONS:
            from app.core.errors import NotFoundError

            raise NotFoundError("observability.session_not_found", "session not found")
        return {"session_id": session_id, **SESSIONS[session_id]}

    def _list_sessions(self, range_key, db, workspace, force=False, logs=None):
        if self.listing_fails:
            raise RuntimeError("logs unavailable")
        return {"range": range_key, "sessions": list(self.rows), "count": len(self.rows)}

    def _spawn(self, fn: Any) -> None:
        # the thread body is captured; `finish` runs it when the machine says so
        self.pending[self._spawning] = fn

    @initialize()
    def install(self) -> None:
        self._patch = pytest.MonkeyPatch()
        self._patch.setattr(pipeline_routers.observability, "get_session", self._get_session)
        self._patch.setattr(pipeline_routers.observability, "list_sessions", self._list_sessions)
        self._patch.setattr(pipeline_routers, "_spawn", self._spawn)

    # ── helpers ──

    def _read(self, pid: str) -> dict[str, Any]:
        res = self.client.get(f"/api/eval/pipelines/{pid}")
        assert res.status_code == 200, res.text
        return res.json()

    def _check(self, pid: str, body: dict[str, Any]) -> None:
        m = self.model[pid]
        assert body["status"] in STATUSES
        assert body["status"] == m["status"], (body["status"], m["status"])
        if body["status"] == "running":
            assert body["last_run"] == m["last_run"]  # the previous outcome stands until the end
        assert _output(body["config"]["output"]) == m["output"]

    # ── rules ──

    @rule(target=pipelines, by_name=st.booleans(), agent=st.sampled_from([None, "hr-bot", "other"]),
          status=st.sampled_from(["all", "ok", "error"]), max_sessions=st.integers(1, 5),
          first_turn_only=st.booleans(), dedupe=st.booleans())
    def create(self, by_name, agent, status, max_sessions, first_turn_only, dedupe) -> str:
        output = ({"dataset_name": f"out-{len(self.model)}"} if by_name
                  else {"dataset_id": self.seed_dataset})
        res = self.client.post("/api/eval/pipelines", json={
            "name": f"p{len(self.model)}",
            "source": {"agent": agent, "range": "24h", "status": status,
                       "max_sessions": max_sessions},
            "processing": {"first_turn_only": first_turn_only, "dedupe": dedupe},
            "output": output,
        })
        assert res.status_code == 201, res.text
        body = res.json()
        assert body["status"] == "idle" and body["last_run"] is None
        self.model[body["id"]] = {"status": "idle", "last_run": None,
                                  "output": _output(body["config"]["output"]),
                                  "datasets": set(), "runs": 0}
        return body["id"]

    @rule(pid=pipelines)
    def start(self, pid: str) -> None:
        m = self.model[pid]
        self._spawning = pid
        res = self.client.post(f"/api/eval/pipelines/{pid}/run")
        if m["status"] == "running":
            assert res.status_code == 409 and res.json()["code"] == "pipeline.running", res.text
            return
        assert res.status_code == 202, res.text
        assert res.json()["status"] == "running"
        assert pid in self.pending
        m["status"] = "running"
        m["runs"] += 1

    @precondition(lambda self: self.pending)
    @rule(data=st.data())
    def finish(self, data) -> None:
        pid = data.draw(st.sampled_from(sorted(self.pending)))
        fn = self.pending.pop(pid)
        fn()  # the captured thread body, start to finish
        m = self.model.get(pid)
        if m is None:  # deleted while it ran
            return
        body = self._read(pid)
        assert body["status"] in TERMINAL, body
        last = body["last_run"]
        assert last is not None and last != m["last_run"]
        assert last["added"] <= last["matched"] <= last["scanned"], last
        visible = len(self.rows) if not self.listing_fails else 0
        assert last["scanned"] <= visible
        if body["status"] == "failed":
            assert last["error"], last
        else:
            assert last["error"] is None
        if last["dataset_id"]:
            m["datasets"].add(last["dataset_id"])
            assert len(m["datasets"]) == 1, ("a second output dataset", m["datasets"])
            # the pipeline now targets the dataset it created (or appended to)
            assert _output(body["config"]["output"]) == {"dataset_id": last["dataset_id"]}
            self._check_dataset(last["dataset_id"])
        elif body["status"] == "succeeded":
            # nothing extracted and no target: the output stays by name
            assert _output(body["config"]["output"]) == m["output"]
        m.update(status=body["status"], last_run=last, output=_output(body["config"]["output"]))

    def _check_dataset(self, dataset_id: str) -> None:
        db = SessionLocal()
        try:
            row = db.get(EvalDataset, dataset_id)
            assert row is not None
            items = list(row.items or [])
            assert 1 <= len(items) <= 200
            _validate_items(items)  # unique scenario ids, non-empty turns
            assert row.kind == ("predefined" if any("turns" in i for i in items) else "legacy")
        finally:
            db.close()

    @rule(pid=pipelines)
    def read(self, pid: str) -> None:
        self._check(pid, self._read(pid))
        listed = {p["id"]: p for p in self.client.get("/api/eval/pipelines").json()["pipelines"]}
        assert pid in listed
        self._check(pid, listed[pid])

    @rule()
    def restart(self) -> None:
        """The process died: no thread survives, the startup sweep fails every running row."""
        with pipeline_routers._live_lock:
            pipeline_routers._live_runs.clear()
        self.pending.clear()
        swept = set(pipeline_routers.sweep_interrupted_pipelines())
        running = {pid for pid, m in self.model.items() if m["status"] == "running"}
        assert running <= swept
        for pid in running:
            body = self._read(pid)
            assert body["status"] == "failed"
            assert body["last_run"]["error"] == pipeline_routers.PIPELINE_RESTART_ERROR
            self.model[pid].update(status="failed", last_run=body["last_run"])

    @rule(pid=pipelines)
    def reap(self, pid: str) -> None:
        """A `running` row whose thread is gone fails on the next read (the in-request
        twin of the sweep)."""
        m = self.model[pid]
        with pipeline_routers._live_lock:
            pipeline_routers._live_runs.discard(pid)
        self.pending.pop(pid, None)
        body = self._read(pid)
        if m["status"] == "running":
            assert body["status"] == "failed"
            assert body["last_run"]["error"] == pipeline_routers.PIPELINE_RESTART_ERROR
            m.update(status="failed", last_run=body["last_run"])
        else:
            self._check(pid, body)

    @rule(pid=pipelines)
    def update(self, pid: str) -> None:
        m = self.model[pid]
        res = self.client.put(f"/api/eval/pipelines/{pid}", json={
            "name": "renamed", "output": m["output"]})  # the same target: output unchanged
        if m["status"] == "running":
            assert res.status_code == 409 and res.json()["code"] == "pipeline.running"
        else:
            assert res.status_code == 200, res.text
            self._check(pid, res.json())

    @rule(pid=consumes(pipelines))
    def remove(self, pid: str) -> None:
        if self.model[pid]["status"] == "running":
            self.deleted_running.add(pid)
        assert self.client.delete(f"/api/eval/pipelines/{pid}").json() == {"deleted": True}
        assert self.client.get(f"/api/eval/pipelines/{pid}").status_code == 404
        del self.model[pid]

    @rule(subset=st.lists(st.integers(0, len(ALL_ROWS) - 1), unique=True),
          fails=st.booleans())
    def vary_sessions(self, subset, fails) -> None:
        self.rows = [ALL_ROWS[i] for i in sorted(subset)]
        self.listing_fails = fails

    @precondition(lambda self: self.model)
    @invariant()
    def statuses_are_known(self) -> None:
        for p in self.client.get("/api/eval/pipelines").json()["pipelines"]:
            assert p["status"] in STATUSES
            if p["id"] in self.model:
                assert p["status"] == self.model[p["id"]]["status"]

    def teardown(self) -> None:
        self._patch.undo()
        with pipeline_routers._live_lock:
            pipeline_routers._live_runs.clear()
        db = SessionLocal()
        try:
            db.execute(delete(EvalPipeline))
            db.execute(delete(EvalDataset))
            db.commit()
        finally:
            db.close()


def test_pipeline_state_machine():
    run_state_machine_as_test(
        PipelineMachine,
        settings=settings(max_examples=examples(8), stateful_step_count=20, deadline=None,
                          database=None, suppress_health_check=list(HealthCheck)),
    )


@pytest.mark.xfail(strict=True, reason="N16-obs15: a pipeline deleted while running still writes "
                                      "its output dataset (`orphan-out`), which no pipeline owns")
def test_a_pipeline_deleted_mid_run_leaves_no_output_dataset_behind(monkeypatch, client):
    """A run keeps going after its pipeline is deleted; its output dataset must not appear
    out of nowhere (no pipeline owns it, nothing lists it as a pipeline outcome)."""
    pending: list[Any] = []

    def get_session(sid, r, db, ws, force=False, logs=None):
        if sid not in SESSIONS:
            from app.core.errors import NotFoundError

            raise NotFoundError("observability.session_not_found", "session not found")
        return {"session_id": sid, **SESSIONS[sid]}

    monkeypatch.setattr(pipeline_routers.observability, "get_session", get_session)
    monkeypatch.setattr(pipeline_routers.observability, "list_sessions",
                        lambda r, db, ws, force=False, logs=None: {"range": r, "sessions": ROWS,
                                                                   "count": len(ROWS)})
    monkeypatch.setattr(pipeline_routers, "_spawn", pending.append)
    pid = client.post("/api/eval/pipelines", json={
        "name": "orphan", "output": {"dataset_name": "orphan-out"}}).json()["id"]
    assert client.post(f"/api/eval/pipelines/{pid}/run").status_code == 202
    assert client.delete(f"/api/eval/pipelines/{pid}").json() == {"deleted": True}
    pending[0]()  # the thread finishes after the delete
    names = [d["name"] for d in client.get("/api/eval/datasets").json()["datasets"]]
    assert "orphan-out" not in names, names


# ─── the upload property ─────────────────────────────────────────────────────


text = st.text(
    alphabet=st.characters(exclude_categories=("Cs",), exclude_characters="\n\r"),
    min_size=1, max_size=20,
).filter(lambda s: s.strip())

prompt_items = st.fixed_dictionaries({"prompt": text}, optional={"expected": text})
scenario_items = st.fixed_dictionaries({
    "scenario_id": st.sampled_from(["s1", "s2", "s3", "s4"]),
    "turns": st.lists(st.fixed_dictionaries({"input": text}), min_size=1, max_size=2),
})
persona_items = st.fixed_dictionaries({
    "scenario_id": st.sampled_from(["p1", "p2", "p3"]),
    "input": text,
    "actor_profile": st.fixed_dictionaries({"context": text, "goal": text}),
})
bad_items = st.one_of(
    st.just({"prompt": ""}), st.just({"prompt": "   "}), st.just({"turns": []}),
    st.just({"scenario_id": "x", "turns": [{"input": ""}]}),
    st.just({"scenario_id": "", "turns": [{"input": "q"}]}),
    st.just({"scenario_id": "a1", "input": "q", "actor_profile": {"context": "c"}}),
    st.just({"scenario_id": "a2", "actor_profile": {"context": "c", "goal": "g"}}),
    st.just({"other": 1}),
)
lines = st.one_of(
    st.one_of(prompt_items, scenario_items, persona_items, bad_items).map(
        lambda item: json.dumps(item, ensure_ascii=False)),
    st.sampled_from(["", "   ", "\t"]),  # blank lines are skipped
    st.sampled_from(["[1, 2]", "42", '"text"', "null", "true"]),  # JSON, not an object
    st.sampled_from(["{", '{"prompt": }', "not json", "{'prompt': 'q'}"]),  # broken JSON
)


def _oracle(jsonl: str) -> tuple[int, str | None, int]:
    """(status, code, item_count) the contract promises for ``jsonl``, written apart from
    the route: lines are separated by ``\\n`` only; blank lines are skipped; a line that is
    not JSON → 422 ``dataset.invalid_jsonl``, not an object → 422 ``dataset.invalid_item``;
    no item → 422 ``dataset.empty``; then the per-item shape rules of ``_validate_items``."""
    items: list[dict[str, Any]] = []
    for line in jsonl.split("\n"):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            return 422, "dataset.invalid_jsonl", 0
        if not isinstance(item, dict):
            return 422, "dataset.invalid_item", 0
        items.append(item)
    if not items:
        return 422, "dataset.empty", 0
    seen: set[str] = set()
    for item in items:
        if "turns" in item or "actor_profile" in item:
            sid = str(item.get("scenario_id") or "").strip()
            if not sid or sid in seen:
                return 422, "dataset.invalid_item", 0
            seen.add(sid)
        if "turns" in item:
            turns = item.get("turns")
            if not isinstance(turns, list) or not turns or any(
                    not str((t or {}).get("input") or "").strip() for t in turns):
                return 422, "dataset.invalid_item", 0
        elif "actor_profile" in item:
            profile = item.get("actor_profile")
            complete = isinstance(profile, dict) and str(profile.get("context", "")).strip() and (
                str(profile.get("goal", "")).strip())
            if not str(item.get("input", "")).strip() or not complete:
                return 422, "dataset.invalid_item", 0
        elif not str(item.get("prompt", "")).strip():
            return 422, "dataset.invalid_item", 0
    return 201, None, len(items)


@settings(max_examples=examples(200), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.lists(lines, min_size=1, max_size=8))
@example(['{"prompt": "a b"}'])  # U+2028 inside a JSON string is one line, not two
@example(['{"prompt": "a\x85b"}'])  # NEL
@example(['{"prompt": "a\x1cb"}'])  # FS
def test_upload_is_a_precise_4xx_or_a_201_with_every_valid_line(client, data):
    jsonl = "\n".join(data)
    if not jsonl.strip():
        return  # pydantic's min_length=1 covers the empty body
    expected_status, expected_code, expected_count = _oracle(jsonl)
    res = client.post("/api/eval/datasets/upload", json={"name": "up", "jsonl": jsonl})
    assert res.status_code != 500, res.text
    assert res.status_code == expected_status, (res.text, data)
    if expected_status == 201:
        body = res.json()
        assert body["item_count"] == expected_count == len(body["items"])
        assert body["items"] == [json.loads(line) for line in jsonl.split("\n") if line.strip()]
        assert body["kind"] in ("legacy", "predefined", "simulated")
    else:
        body = res.json()
        assert body["code"] == expected_code, (body, data)
        if expected_code != "dataset.empty":
            assert "line" in body["message"] or "item" in body["message"], body


def _upload(client: TestClient, jsonl: str, **extra: Any):
    return client.post("/api/eval/datasets/upload", json={"name": "up", "jsonl": jsonl, **extra})


def test_upload_holds_items_to_the_same_caps_as_dataset_create(client):
    """N16-obs13 (fixed in r16-b): 201 items / a > 8000-char prompt / a > 16000-char item
    used to be accepted by the upload while DatasetCreate refused them."""
    many = "\n".join(json.dumps({"prompt": f"q{i}"}) for i in range(201))
    res = _upload(client, many)
    assert res.status_code == 422 and res.json()["code"] == "dataset.too_many_items", res.text
    exactly = "\n".join(json.dumps({"prompt": f"q{i}"}) for i in range(200))
    assert _upload(client, exactly).status_code == 201
    huge = json.dumps({"prompt": "x" * 1_100_000})  # a > 1 MB line
    res = _upload(client, huge)
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_item", res.text
    assert "item 1" in res.json()["message"]
    assert _upload(client, json.dumps({"prompt": "x" * 8001})).status_code == 422
    assert _upload(client, json.dumps({"prompt": "x" * 8000})).status_code == 201
    padded = json.dumps({"prompt": "q", "expected": "y" * 16000})
    assert _upload(client, padded).status_code == 422


def test_upload_of_a_deeply_nested_value_is_a_4xx_not_a_500():
    client = TestClient(create_app(), raise_server_exceptions=False)
    deep = "[" * 100_000 + "]" * 100_000
    nested = json.dumps({"prompt": "q", "meta": None}).replace("null", deep)
    res = _upload(client, nested)  # N16-obs14 (fixed in r16-b): used to be a 500
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_jsonl", res.text
    assert res.json()["message"].startswith("line 1:")


def test_upload_of_an_invalid_utf8_body_is_a_4xx(client):
    res = client.post("/api/eval/datasets/upload", content=b'{"name": "x", "jsonl": "\xff\xfe"}',
                      headers={"content-type": "application/json"})
    assert 400 <= res.status_code < 500, res.text
    assert res.json()["code"]
