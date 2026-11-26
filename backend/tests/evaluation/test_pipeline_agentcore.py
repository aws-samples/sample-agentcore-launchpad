"""数据处理 `agentcore` source — AgentCore dataset generation into a local dataset.

Hermetic: observability sessions are fixtures, the AgentCore control/data planes
are one in-memory fake (`FakeAgentCore`) that keeps a dataset Draft, so the run's
start → poll → pull-back → Draft-equals-local contract is checked end to end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import generation, pipeline_routers
from app.evaluation import routers as eval_routers
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.routers import _validate_items
from app.models.ledger import Agent

SERVICE = "hr_runtime.DEFAULT"
LOG_GROUP = "/aws/bedrock-agentcore/runtimes/hr_runtime-abc123-DEFAULT"
NOW = datetime.now(UTC)


def _iso(minutes_ago: float) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def _row(sid, *, service=SERVICE, errors=0, traces=1, minutes_ago=60.0):
    return {"session_id": sid, "service": service, "agent": "hr", "errors": errors,
            "traces": traces, "first": _iso(minutes_ago + 2), "last": _iso(minutes_ago)}


ROWS = [
    _row("sess-one-aaaaaaaa", traces=1, minutes_ago=30),
    _row("sess-two-bbbbbbbb", traces=3, minutes_ago=40),
    _row("sess-err-cccccccc", traces=2, errors=1, minutes_ago=50),
    _row("sess-new-dddddddd", traces=2, minutes_ago=2),  # too fresh
    _row("sess-oth-eeeeeeee", service="other.DEFAULT"),
]


def _fixed(sid, text, *, reply="ok"):
    return {"scenario_id": f"gen-{sid}", "turns": [{"input": text, "expected_response": reply}],
            "expected_trajectory": [],
            "metadata": {"generation_id": "GEN", "source": {"session_id": sid}}}


class FakeAgentCore:
    """Control + data plane of one account: datasets with a Draft, generations."""

    def __init__(self):
        self.datasets: dict[str, dict] = {}
        self.examples: dict[str, list[dict]] = {}
        self.calls: list[tuple[str, dict]] = []
        self.generated: list[dict] = []  # what the next generation writes
        self.gen_status = "COMPLETED"
        self.conflict = False
        self.polls_before_done = 1
        self.busy = False  # another generation holds the dataset
        self.throttle_polls = 0
        self._seq = 0

    # control plane
    def create_dataset(self, **kw):
        self.calls.append(("create_dataset", kw))
        did = f"{kw['datasetName']}-X{len(self.datasets)}"
        self.datasets[did] = {"datasetId": did, "datasetName": kw["datasetName"],
                              "datasetArn": f"arn:ds/{did}", "status": "ACTIVE"}
        self.examples[did] = []
        for ex in (kw.get("source") or {}).get("inlineExamples", {}).get("examples", []):
            self._add(did, ex)
        return {"datasetId": did, "datasetArn": f"arn:ds/{did}"}

    def _add(self, did, ex):
        self._seq += 1
        self.examples[did].append({"exampleId": ex.get("scenario_id") or f"e{self._seq}", **ex})

    def get_dataset(self, datasetId, datasetVersion=None):
        d = self.datasets[datasetId]
        return {**d, "exampleCount": len(self.examples[datasetId]), "draftStatus": "MODIFIED"}

    def list_dataset_examples(self, datasetId, nextToken=None, datasetVersion=None, filter=None):
        self.calls.append(("list_dataset_examples", {"datasetId": datasetId, "filter": filter}))
        rows = self.examples[datasetId]
        if filter:
            gid = filter["generationIds"][0]
            rows = [e for e in rows if (e.get("metadata") or {}).get("generation_id") == gid]
        return {"examples": [dict(e) for e in rows]}

    def add_dataset_examples(self, datasetId, source, clientToken=None):
        for ex in source["inlineExamples"]["examples"]:
            self._add(datasetId, ex)
        return {}

    def delete_dataset_examples(self, datasetId, exampleIds, clientToken=None):
        self.calls.append(("delete_dataset_examples", {"exampleIds": list(exampleIds)}))
        self.examples[datasetId] = [e for e in self.examples[datasetId]
                                    if e["exampleId"] not in set(exampleIds)]
        return {}

    def delete_dataset(self, datasetId, datasetVersion=None):
        self.calls.append(("delete_dataset", {"datasetId": datasetId}))
        self.datasets.pop(datasetId, None)
        return {}

    def list_dataset_versions(self, datasetId, nextToken=None):
        return {"versions": []}

    # data plane
    def list_dataset_generations(self, datasetId, nextToken=None):
        return {"datasetGenerations": [{"generationId": "other", "status": "IN_PROGRESS"}]
                if self.busy else []}

    def start_dataset_generation(self, **kw):
        self.calls.append(("start_dataset_generation", kw))
        if self.conflict:
            raise ClientError({"Error": {"Code": "ConflictException",
                                         "Message": "A generation is already running"}},
                              "StartDatasetGeneration")
        gid = f"{kw['name']}-0123456789"
        self.current = {"gid": gid, "did": kw["datasetId"], "polls": 0}
        return {"generationId": gid, "status": "IN_PROGRESS"}

    def get_dataset_generation(self, datasetId, generationId):
        if self.throttle_polls:
            self.throttle_polls -= 1
            raise ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow"}},
                              "GetDatasetGeneration")
        self.current["polls"] += 1
        if self.current["polls"] <= self.polls_before_done:
            return {"status": "IN_PROGRESS"}
        if not self.current.get("written"):
            self.current["written"] = True
            for ex in self.generated:
                meta = {**ex["metadata"], "generation_id": generationId}
                self._add(datasetId, {**ex, "metadata": meta})
        return {
            "status": self.gen_status,
            "failureReason": "boom" if self.gen_status == "FAILED" else None,
            "generationResults": {"sessionsMatched": 2, "sessionsSelected": 2,
                                  "sessionsFailed": 1, "scenariosGenerated": len(self.generated),
                                  "examplesWritten": len(self.generated)},
            "errorDetails": [{"errorCode": "SPANS_INCOMPLETE", "errorMessage": "x", "count": 1,
                              "sessionIds": ["sess-err-cccccccc"]}],
            "redactionSummary": {"scenariosScanned": 2, "findingCounts": {"ORDER_ID": 3}},
        }


@pytest.fixture
def aws(monkeypatch):
    fake = FakeAgentCore()
    monkeypatch.setattr(pipeline_routers, "control_client", lambda ctx=None: fake)
    monkeypatch.setattr(pipeline_routers, "data_client", lambda ctx=None, **_: fake)
    monkeypatch.setattr(eval_routers, "control_client", lambda ctx=None: fake)
    monkeypatch.setattr(pipeline_routers, "resolve_telemetry",
                        lambda agent, ws, logs=None, **_: (SERVICE, LOG_GROUP))
    monkeypatch.setattr(pipeline_routers, "_retention_days", lambda ws, group: 30)
    monkeypatch.setattr(pipeline_routers, "_sleep", lambda s: None)
    monkeypatch.setattr(
        pipeline_routers.observability, "list_sessions",
        lambda range_key, db, workspace, force=False, logs=None: {"sessions": ROWS},
    )
    monkeypatch.setattr(pipeline_routers, "_spawn", lambda fn: fn())  # run inline
    return fake


@pytest.fixture
def agent_id():
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="hr", method="zip_runtime",
                  status="active", resource_id="hr_runtime-abc123", spec={"name": "hr"})
    db.add(agent)
    db.commit()
    aid = agent.id
    db.close()
    return aid


def _dataset(items, kind):
    db = SessionLocal()
    row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="seed", kind=kind, items=items)
    db.add(row)
    db.commit()
    rid = row.id
    db.close()
    return rid


def _body(agent_id, output, **source):
    return {"name": "gen", "source": {"type": "agentcore", "agent_id": agent_id, **source},
            "output": output}


def _create_and_run(client, body):
    res = client.post("/api/eval/pipelines", json=body)
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    assert client.post(f"/api/eval/pipelines/{pid}/run").status_code == 202
    return pid, client.get(f"/api/eval/pipelines/{pid}").json()


# ─── pure helpers ────────────────────────────────────────────────────────────
def test_select_sessions_service_status_turns_fresh_and_present():
    picked, skipped, matched = generation.select_sessions(
        ROWS, service=SERVICE, status="all", dynamic=False,
        present={"sess-two-bbbbbbbb"}, limit=25, now=NOW,
    )
    assert [r["session_id"] for r in picked] == ["sess-one-aaaaaaaa", "sess-err-cccccccc"]
    assert {(s["session_id"], s["reason"]) for s in skipped} == {
        ("sess-new-dddddddd", "too_fresh"), ("sess-two-bbbbbbbb", "already_in_dataset"),
    }
    assert matched == 4  # the other service never counts
    picked, _, matched = generation.select_sessions(
        ROWS, service=SERVICE, status="ok", dynamic=True, present=set(), limit=1, now=NOW,
    )
    # DYNAMIC: multi-turn only; ok: no errors; newest first, capped at 1
    assert [r["session_id"] for r in picked] == ["sess-two-bbbbbbbb"] and matched == 2


def test_build_source_window_identifiers_ground_truth_and_redaction():
    cfg = pipeline_routers.PipelineSource(
        type="agentcore", agent_id="a1", scenario_type="FIXED",
        redaction={"identifiers": ["EMAIL_ADDRESS"], "custom": [{"name": "ORDER_ID",
                                                                 "regex": "ORD-[0-9]+"}]},
    )
    sessions = [ROWS[0], ROWS[1]]
    src = generation.build_source(cfg, service_name=SERVICE, log_group=LOG_GROUP,
                                  sessions=sessions, keep_replies=True, now=NOW,
                                  retention_days=30)
    cw = src["sourceConfig"]["cloudWatchLogs"]
    assert cw["logGroupNames"] == [LOG_GROUP] and cw["serviceNames"] == [SERVICE]
    assert cw["startTime"] == datetime.fromisoformat(ROWS[1]["first"]) - generation.WINDOW_MARGIN
    assert cw["endTime"] <= NOW
    assert src["selection"] == {"identifiers": {"sessionIds": ["sess-one-aaaaaaaa",
                                                               "sess-two-bbbbbbbb"]}}
    assert src["sampling"] == {"maxScenarios": 2}
    assert src["groundTruth"] == {"fields": ["expected_response", "expected_trajectory"]}
    assert src["redactionConfiguration"]["customDataIdentifiers"] == [
        {"name": "ORDER_ID", "regex": "ORD-[0-9]+"}]
    dyn = pipeline_routers.PipelineSource(type="agentcore", agent_id="a1",
                                          scenario_type="DYNAMIC")
    src = generation.build_source(dyn, service_name=SERVICE, log_group=LOG_GROUP,
                                  sessions=sessions, keep_replies=True, now=NOW,
                                  retention_days=1)
    assert "groundTruth" not in src and "redactionConfiguration" not in src
    # a 1-day retention clamps a window that would reach further back
    assert src["sourceConfig"]["cloudWatchLogs"]["startTime"] >= NOW - timedelta(days=1)


def test_keep_examples_drops_foreign_interrupt_and_invalid():
    junk = _fixed("sess-one-aaaaaaaa", "status?",
                  reply="[{'id': 'v1:before_tool_call:tooluse_X', 'name': 'f'}]")
    examples = [
        {"exampleId": "e1", **_fixed("sess-one-aaaaaaaa", "hi")},
        {"exampleId": "e2", **_fixed("sess-zzz", "foreign")},
        {"exampleId": "e3", **junk},
        {"exampleId": "e4", "scenario_id": "bad", "turns": [],
         "metadata": {"source": {"session_id": "sess-one-aaaaaaaa"}}},
    ]
    kept, dropped = generation.keep_examples(
        examples, requested={"sess-one-aaaaaaaa"}, validate=_validate_items)
    assert [k["scenario_id"] for k in kept] == ["gen-sess-one-aaaaaaaa"]
    assert "exampleId" not in kept[0]
    assert [(eid, d["reason"]) for eid, d in dropped] == [
        ("e2", "unexpected_session"), ("e3", "interrupt_transcript"), ("e4", "invalid_example")]


def test_redaction_rules_refused_at_save():
    from pydantic import ValidationError
    for bad in (
        {"custom": [{"name": "A", "regex": "x(?=y)"}]},
        {"custom": [{"name": "A", "regex": "x"}, {"name": "A", "regex": "y"}]},
        {"identifiers": ["NAME"], "custom": [{"name": "NAME", "regex": "x"}]},
    ):
        with pytest.raises(ValidationError):
            pipeline_routers.Redaction.model_validate(bad)
    rows = [{**_row("sess-nots-ffffffff"), "last": None, "first": None}]
    picked, skipped, _ = generation.select_sessions(
        rows, service=SERVICE, status="all", dynamic=False, present=set(), limit=5, now=NOW)
    assert not picked and skipped[0]["reason"] == "too_fresh"


def test_names_and_tokens_match_the_aws_patterns():
    import re
    assert re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,47}", generation.generation_name("ab12-cd"))
    token = generation.client_token("ab12cd", "2026-11-26T12:00:00.123+00:00")
    assert 33 <= len(token) <= 256 and re.fullmatch(r"[a-zA-Z0-9](-*[a-zA-Z0-9]){0,256}", token)
    assert generation.present_session_ids([
        {"metadata": {"source": {"session_id": "a"}}}, {"metadata": {"session_id": "b"}},
        {"prompt": "x"}]) == {"a", "b"}


# ─── config validation ───────────────────────────────────────────────────────
def test_agentcore_config_validation(client, aws, agent_id):
    res = client.post("/api/eval/pipelines", json=_body(agent_id, {"dataset_name": "x"},
                                                          max_sessions=26))
    assert res.status_code == 422
    res = client.post("/api/eval/pipelines", json={"name": "g", "source": {"type": "agentcore"},
                                                     "output": {"dataset_name": "x"}})
    assert res.status_code == 422
    res = client.post("/api/eval/pipelines", json=_body("nope", {"dataset_name": "x"}))
    assert res.status_code == 404 and res.json()["code"] == "agent.not_found"
    persona = _dataset([{"scenario_id": "p", "input": "hi",
                         "actor_profile": {"context": "c", "goal": "g"}}], "simulated")
    res = client.post("/api/eval/pipelines", json=_body(agent_id, {"dataset_id": persona}))
    assert res.status_code == 400 and res.json()["code"] == "pipeline.target_kind_mismatch"
    legacy = _dataset([{"prompt": "x"}], "legacy")
    res = client.post("/api/eval/pipelines", json=_body(agent_id, {"dataset_id": legacy},
                                                          scenario_type="DYNAMIC"))
    assert res.status_code == 400 and res.json()["detail"]["kind"] == "legacy"


# ─── runs ────────────────────────────────────────────────────────────────────
def test_fixed_run_into_new_dataset(client, aws, agent_id):
    aws.generated = [
        _fixed("sess-one-aaaaaaaa", "How many leave days?"),
        _fixed("sess-err-cccccccc", "status?",
               reply="[{'id': 'v1:before_tool_call:tooluse_X'}]"),  # junk → dropped
    ]
    pid, row = _create_and_run(client, _body(agent_id, {"dataset_name": "gen_set"},
                                             redaction={"identifiers": ["EMAIL_ADDRESS"]}))
    assert row["status"] == "succeeded", row["last_run"]
    run = row["last_run"]
    gen = run["generation"]
    assert gen["stage"] == "done" and gen["status"] == "COMPLETED"
    assert gen["requested"] == 3 and gen["results"]["sessionsFailed"] == 1
    assert gen["error_details"][0]["code"] == "SPANS_INCOMPLETE"
    assert gen["redaction"] == {"scanned": 2, "findings": {"ORDER_ID": 3}}
    reasons = {(s["session_id"], s["reason"], s.get("code")) for s in run["skipped"]}
    assert ("sess-new-dddddddd", "too_fresh", None) in reasons
    assert ("sess-err-cccccccc", "generation_failed", "SPANS_INCOMPLETE") in reasons
    assert ("sess-err-cccccccc", "interrupt_transcript", None) in reasons
    assert run["added"] == 1

    start = next(kw for name, kw in aws.calls if name == "start_dataset_generation")
    assert start["scenarioType"] == "FIXED"
    tg = start["source"]["traceGeneration"]
    assert tg["selection"]["identifiers"]["sessionIds"] == [
        "sess-one-aaaaaaaa", "sess-two-bbbbbbbb", "sess-err-cccccccc"]
    assert tg["groundTruth"]["fields"] == ["expected_response", "expected_trajectory"]
    assert tg["redactionConfiguration"]["identifiers"] == ["EMAIL_ADDRESS"]
    create = next(kw for name, kw in aws.calls if name == "create_dataset")
    assert "source" not in create  # an empty Draft the generation fills
    # the pull-back reads only this generation's examples, on every page
    lists = [kw for name, kw in aws.calls if name == "list_dataset_examples" and kw["filter"]]
    assert lists and lists[0]["filter"] == {"generationIds": [gen["id"]]}

    # the run created the local dataset, retargeted the pipeline, Draft == local
    db = SessionLocal()
    ds = db.get(EvalDataset, run["dataset_id"])
    assert ds.kind == "predefined" and ds.name == "gen_set"
    assert [i["scenario_id"] for i in ds.items] == ["gen-sess-one-aaaaaaaa"]
    assert ds.items[0]["metadata"]["source"]["session_id"] == "sess-one-aaaaaaaa"
    assert ds.cloud["dataset_id"] == gen["cloud_dataset_id"]
    assert db.get(EvalPipeline, pid).config["output"] == {"dataset_id": ds.id}
    db.close()
    assert [e["scenario_id"] for e in aws.examples[gen["cloud_dataset_id"]]] == [
        "gen-sess-one-aaaaaaaa"]


def test_dynamic_run_appends_to_existing_persona_dataset(client, aws, agent_id):
    seed = {"scenario_id": "p0", "input": "hi", "actor_profile": {"context": "c", "goal": "g"}}
    ds_id = _dataset([seed], "simulated")
    aws.generated = [{"scenario_id": "gen-p1", "input": "need leave", "max_turns": 30,
                      "actor_profile": {"context": "employee", "goal": "book leave",
                                        "known_info": "x", "unknown_info": "y"},
                      "metadata": {"source": {"session_id": "sess-two-bbbbbbbb"}}}]
    _, row = _create_and_run(client, _body(agent_id, {"dataset_id": ds_id},
                                           scenario_type="DYNAMIC", status="ok"))
    assert row["status"] == "succeeded", row["last_run"]
    start = next(kw for name, kw in aws.calls if name == "start_dataset_generation")
    tg = start["source"]["traceGeneration"]
    assert start["scenarioType"] == "DYNAMIC" and "groundTruth" not in tg
    assert tg["selection"]["identifiers"]["sessionIds"] == ["sess-two-bbbbbbbb"]
    db = SessionLocal()
    ds = db.get(EvalDataset, ds_id)
    assert [i["scenario_id"] for i in ds.items] == ["p0", "gen-p1"]
    cloud_id = ds.cloud["dataset_id"]
    db.close()
    # the existing row was synced first, so the Draft holds the seed + the persona
    assert sorted(e["scenario_id"] for e in aws.examples[cloud_id]) == ["gen-p1", "p0"]


def test_nothing_to_send_succeeds_without_aws_writes(client, aws, agent_id, monkeypatch):
    monkeypatch.setattr(pipeline_routers.observability, "list_sessions",
                        lambda *a, **k: {"sessions": [ROWS[3]]})  # only a fresh session
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "x"}))
    assert row["status"] == "succeeded" and row["last_run"]["added"] == 0
    assert row["last_run"]["skipped"] == [{"session_id": "sess-new-dddddddd",
                                           "reason": "too_fresh"}]
    assert aws.calls == []


def test_new_dataset_left_empty_is_deleted(client, aws, agent_id):
    aws.generated = [_fixed("sess-zzz", "not ours")]
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "x"}))
    assert row["status"] == "failed"
    assert row["last_run"]["error"].startswith("dataset.nothing_extracted")
    assert any(name == "delete_dataset" for name, _ in aws.calls)
    assert not aws.datasets


def test_generation_failed_and_conflict(client, aws, agent_id):
    aws.gen_status = "FAILED"
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "x"}))
    assert row["status"] == "failed"
    assert row["last_run"]["error"].startswith("pipeline.generation_failed: boom")
    assert "deleted" in row["last_run"]["error"]  # the run's own AWS dataset went away
    assert not aws.datasets
    assert row["last_run"]["generation"]["status"] == "FAILED"

    aws.gen_status, aws.conflict = "COMPLETED", True
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "y"}))
    assert row["status"] == "failed"
    assert row["last_run"]["error"].startswith("dataset.generation_running")


def test_restart_resumes_the_started_generation(client, aws, agent_id, monkeypatch):
    """A restart between start and finish leaves the row `running` with the
    generation id persisted: the next read resumes polling and merges once."""
    aws.generated = [_fixed("sess-one-aaaaaaaa", "hi")]

    class ProcessDied(BaseException):
        """Not an Exception: the run's own error handling never sees it."""

    def crash(*a, **k):
        raise ProcessDied

    def spawn_and_die(fn):
        try:
            fn()
        except ProcessDied:
            pass

    real_finish = pipeline_routers._finish_generation
    monkeypatch.setattr(pipeline_routers, "_finish_generation", crash)
    monkeypatch.setattr(pipeline_routers, "_spawn", spawn_and_die)
    res = client.post("/api/eval/pipelines", json=_body(agent_id, {"dataset_name": "res"}))
    pid = res.json()["id"]
    assert client.post(f"/api/eval/pipelines/{pid}/run").status_code == 202
    db = SessionLocal()
    row = db.get(EvalPipeline, pid)
    assert row.status == "running"  # what the dead process left behind
    gen = row.last_run["generation"]
    db.close()
    assert gen["stage"] == "generating" and gen["id"]

    monkeypatch.setattr(pipeline_routers, "_finish_generation", real_finish)
    monkeypatch.setattr(pipeline_routers, "_spawn", lambda fn: fn())
    client.get(f"/api/eval/pipelines/{pid}")  # the read resumes it (inline)
    row = client.get(f"/api/eval/pipelines/{pid}").json()
    assert row["status"] == "succeeded", row["last_run"]
    assert row["last_run"]["added"] == 1
    assert sum(1 for name, _ in aws.calls if name == "start_dataset_generation") == 1


def test_run_refuses_an_agent_the_caller_cannot_see(client, aws, agent_id, monkeypatch):
    res = client.post("/api/eval/pipelines", json=_body(agent_id, {"dataset_name": "x"}))
    pid = res.json()["id"]
    db = SessionLocal()
    db.get(Agent, agent_id).status = "deleted"
    db.commit()
    db.close()
    res = client.post(f"/api/eval/pipelines/{pid}/run")
    assert res.status_code in (400, 404)
    assert aws.calls == []


def test_hidden_and_fenced_sessions_never_reach_aws(client, aws, agent_id, monkeypatch):
    """Another principal's private session is never sent, even though it is the
    agent's own traffic: selection happens locally, AWS only ever sees ids."""
    class Hidden:
        def visible(self, sid):
            return sid != "sess-two-bbbbbbbb"

        def mentions_hidden(self, _):
            return False

    monkeypatch.setattr(pipeline_routers, "_private", lambda request, ws, db: Hidden())
    aws.generated = [_fixed("sess-one-aaaaaaaa", "hi")]
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "priv"}))
    assert row["status"] == "succeeded", row["last_run"]
    start = next(kw for name, kw in aws.calls if name == "start_dataset_generation")
    ids = start["source"]["traceGeneration"]["selection"]["identifiers"]["sessionIds"]
    assert "sess-two-bbbbbbbb" not in ids and "sess-one-aaaaaaaa" in ids
    assert "filters" not in start["source"]["traceGeneration"]["selection"]


def test_dynamic_new_dataset_keeps_the_simulated_kind(client, aws, agent_id):
    """Every kept item input-only (no persona) still makes a `simulated` dataset,
    so the next DYNAMIC run of the same pipeline is accepted."""
    aws.generated = [{"scenario_id": "g-in", "input": "hi there",
                      "metadata": {"source": {"session_id": "sess-two-bbbbbbbb"}}}]
    pid, row = _create_and_run(client, _body(agent_id, {"dataset_name": "dyn"},
                                             scenario_type="DYNAMIC"))
    assert row["status"] == "succeeded", row["last_run"]
    db = SessionLocal()
    ds = db.get(EvalDataset, row["last_run"]["dataset_id"])
    assert ds.kind == "simulated"
    assert db.get(EvalPipeline, pid).config["output"] == {"dataset_id": ds.id}
    db.close()


def test_timeout_after_start_cleans_up_the_runs_own_dataset(client, aws, agent_id,
                                                             monkeypatch):
    monkeypatch.setattr(generation, "POLL_MAX", 2)
    aws.polls_before_done = 99  # never terminal
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "slow"}))
    assert row["status"] == "failed"
    assert row["last_run"]["error"].startswith("TimeoutError")
    assert row["last_run"]["generation"]["stage"] == "done"
    assert not aws.datasets  # the empty-at-start dataset (and what it got) is gone


def test_throttled_polls_are_retried(client, aws, agent_id):
    aws.generated = [_fixed("sess-one-aaaaaaaa", "hi")]
    aws.throttle_polls = 2
    _, row = _create_and_run(client, _body(agent_id, {"dataset_name": "thr"}))
    assert row["status"] == "succeeded", row["last_run"]


def test_existing_target_with_a_running_generation_is_refused(client, aws, agent_id):
    seed = {"scenario_id": "s0", "turns": [{"input": "seed"}]}
    ds_id = _dataset([seed], "predefined")
    db = SessionLocal()
    db.get(EvalDataset, ds_id).cloud = {"dataset_id": "seed-X9", "status": "ACTIVE"}
    db.commit()
    db.close()
    aws.datasets["seed-X9"] = {"datasetId": "seed-X9", "datasetName": "seed",
                               "datasetArn": "arn", "status": "ACTIVE"}
    aws.examples["seed-X9"] = []
    aws.busy = True
    _, row = _create_and_run(client, _body(agent_id, {"dataset_id": ds_id}))
    assert row["status"] == "failed"
    assert row["last_run"]["error"].startswith("dataset.generation_running")
    assert not any(name == "start_dataset_generation" for name, _ in aws.calls)


def test_death_before_start_reissues_the_persisted_request(client, aws, agent_id,
                                                            monkeypatch):
    """Killed between persisting the request and StartDatasetGeneration: the read
    re-issues the same request (same client token) and keeps the requester as the
    output dataset's creator."""
    aws.generated = [_fixed("sess-one-aaaaaaaa", "hi")]

    class ProcessDied(BaseException):
        pass

    def die(*a, **k):
        raise ProcessDied

    def spawn_and_die(fn):
        try:
            fn()
        except ProcessDied:
            pass

    real_issue = pipeline_routers._issue_start
    monkeypatch.setattr(pipeline_routers, "_issue_start", die)
    monkeypatch.setattr(pipeline_routers, "_spawn", spawn_and_die)
    pid = client.post("/api/eval/pipelines",
                      json=_body(agent_id, {"dataset_name": "re"})).json()["id"]
    client.post(f"/api/eval/pipelines/{pid}/run")
    db = SessionLocal()
    gen = db.get(EvalPipeline, pid).last_run["generation"]
    db.close()
    assert gen["stage"] == "starting" and gen["id"] is None
    assert gen["request"]["client_token"] and gen["creator"] is not None

    monkeypatch.setattr(pipeline_routers, "_issue_start", real_issue)
    monkeypatch.setattr(pipeline_routers, "_spawn", lambda fn: fn())
    client.get(f"/api/eval/pipelines/{pid}")
    row = client.get(f"/api/eval/pipelines/{pid}").json()
    assert row["status"] == "succeeded", row["last_run"]
    [start] = [kw for name, kw in aws.calls if name == "start_dataset_generation"]
    assert start["clientToken"] == gen["request"]["client_token"]
    db = SessionLocal()
    ds = db.get(EvalDataset, row["last_run"]["dataset_id"])
    assert ds.created_by_name == gen["creator"]["name"]
    db.close()


def test_claim_is_exclusive(client, aws, agent_id):
    pid = client.post("/api/eval/pipelines",
                      json=_body(agent_id, {"dataset_name": "c"})).json()["id"]
    db = SessionLocal()
    try:
        assert pipeline_routers._claim(db, pid) is True
        assert pipeline_routers._claim(db, pid) is False  # live already
        pipeline_routers._live_runs.discard(pid)
        assert pipeline_routers._claim(db, pid) is False  # the row says running
    finally:
        pipeline_routers._live_runs.discard(pid)
        db.close()


def test_select_sessions_refuses_unsupported_ids_and_expired():
    rows = [_row("bad id with spaces"), _row("sess-old-gggggggg", minutes_ago=3 * 24 * 60)]
    picked, skipped, _ = generation.select_sessions(
        rows, service=SERVICE, status="all", dynamic=False, present=set(), limit=5,
        now=NOW, oldest=generation.retention_floor(NOW, 1))
    assert not picked
    assert [s["reason"] for s in skipped] == ["unsupported_session_id", "expired"]
