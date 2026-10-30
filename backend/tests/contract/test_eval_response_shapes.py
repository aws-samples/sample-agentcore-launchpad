"""N12 gate item 2 (second half) — the key-sets of the un-typed ``dict`` responses.

Handlers return plain dicts (no ``response_model``), so the OpenAPI snapshot cannot see
response shapes; the restored frontend reads these keys. Each constant below is the
baseline ``4e27b1f4`` key-set (plan §A.1), pinned against the `_out` builders and the
inline dict handlers. E1–E3 replace what fills the values, never the keys — a key added
for CN must be *additive* and recorded here with a comment.
"""

from datetime import UTC, datetime

from app.evaluation import (
    evaluators_local,
    online,
    pipeline_routers,
    results_store,
    routers,
    service,
)
from app.evaluation.models import EvalDataset, EvalEvaluator, EvalPipeline, EvalRun
from app.optimization import canary_routers
from app.optimization.models import RuntimeCanary
from app.optimization.providers import registry as providers

NOW = datetime(2026, 10, 10, 8, 0, tzinfo=UTC)

# Family B step 1 (upstream #261): every user-managed row carries its creator —
# `created_by` (users.id) + `created_by_name` — additive on every shape below.
CREATOR = {"created_by", "created_by_name"}
DATASET_OUT = {"id", "name", "kind", "locale", "description", "items", "item_count", "cloud",
               "has_ground_truth", "created_at", *CREATOR,
               # CN additive (N17-obs407): the agent whose tool names gate the trajectories
               "agent_id",
               # CN additive (D3-d / R3-c): scenarios per split {tuning, held_out} and each
               # item's split
               "split_counts", "item_splits",
               # CN additive (D3-c): the local dataset's revision (bumped on every item change)
               "revision"}
EVALUATOR_OUT = {"id", "name", "level", "description", "definition", "instructions",
                 "rating_scale", "model_id", "base_evaluator_id", "lambda_arn",
                 "lambda_timeout_s", "evaluator_type", "provider", "status",
                 "version", "definition_hash", *CREATOR,  # P7.1 (additive)
                 "pass_threshold",  # N17-obs425 (additive): the evaluator's pass bar
                 # N17-obs437 (additive): what ground truth the evaluator reads
                 "requires_ground_truth", "ground_truth_fields"}
RUN_OUT = {"id", "agent_id", "agent_name", "log_source", "name", "description", "dataset_id",
           "dataset_name", "dataset_version", "mode", "evaluators", "status", "queue_position",
           "session_ids", "batch_eval_id", "scores", "insights", "budget_stops", "error",
           "stop_requested", "created_at", "updated_at",
           # CN additive (N17-obs391): {session_id, scenario_id, scenario_label} per session
           "session_scenarios",
           # CN additive (N17-obs450): `error` read back as a stable reason | null
           "error_code", "error_params",
           # CN additive (N12-bug4): evaluator records / insight sessions that errored on the
           # worker — {count, total, first_type, first_message} | null
           "errors",
           # CN additive (N12-obs47, upstream 00eb359b parity): the sessions a partially failed
           # evaluators run left unscored and why — {total, failed, kind, sessions[]} | null
           "session_failures",
           # upstream #289 (N15-obs1): the agent version the run evaluated — {seq, source} | null
           "snapshot",
           # upstream #251 parity (02607ab4, N15-obs4): simulated persona scenarios skipped
           # after their replay attempts — [{scenario_id, retries, code, error}], [] when none
           "scenario_failures",
           # CN additive (N15-obs8): the referenced rows masked for this viewer —
           # {agent: bool, dataset: bool}; a masked reference keeps its id, nulls its name
           "hidden",
           # CN additive (eval accuracy item 1): the extraction reconciliation summary —
           # {status, sessions, ok, mismatch, unverifiable, …} | null
           "extraction_check",
           # CN additive (R3-f): expected vs judged units, failures by class, coverage and
           # the threshold verdict | null
           "unit_coverage",
           # CN additive (P8.3 / P8.4): the run's judge tokens + budget verdict | null
           "token_usage",
           # CN additive (N17-obs418): the newest worker heartbeat the poller read | null
           "last_heartbeat_at",
           # CN additive (P7.1 / P7.6): evaluator versions at submit, worker release
           "evaluator_versions", "worker_release",
           # CN additive (P3.1 / P3.2, N17-obs349 / obs350): the run's built-in judge and its
           # effective inference config {model_id, temperature, temperature_applied,
           # max_tokens} | null
           "judge_model_id", "judge_inference", *CREATOR,
           # CN additive (N17-obs407): trajectory names the run start rewrote to the agent's
           # own — [{scenario_id, from, to}], [] when none
           "tool_name_rewrites",
           # CN additive (D3-d / R3-c): the dataset split the run replayed — all | tuning |
           # held_out | null
           "dataset_split",
           # CN additive (D3-c): the local dataset revision the run replayed | null
           "dataset_revision",
           # CN additive (N17-obs407): non-blocking warnings [{kind, session_id, scenario_id,
           # tools, observed, message}], [] when none
           "warnings"}
RUN_RESULTS_BASE = {"run_id", "batch_eval_id", "available", "sessions", "count", "truncated"}
RESULT_RECORD = {"evaluator_id", "level", "score", "label", "explanation", "error_type",
                 "error_message",
                 # CN additive (N12-obs62): the session's pre-assigned experiment / canary arm
                 # (`C` | `T1`, as eval_results.arm carries it); null on a plain run
                 "arm",
                 # CN additive (P3.1 / P3.2): the judge that produced the row — its effective
                 # inference config, null for trajectory / code evaluators and older rows
                 "judge",
                 # CN additive (N17-obs317): the judged turn / call and its reconciliation, and
                 # the judge under the run-level names (`judge` is their alias)
                 "trace_id", "span_id", "tool_name", "extraction_status",
                 "judge_model_id", "judge_inference"}
# CN additive (N17-obs391): the run results row also names the session's dataset scenario
RUN_RESULT_ROW = RESULT_RECORD | {"scenario_id", "scenario_label",
                                  "failure_class",  # R3-f: why the row did not score
                                  "token_usage"}  # P8.3: the unit's judge tokens
QUEUE_STATE = {"running", "queued", "locked", "max_concurrency"}
ONLINE_CONFIG = {"config_id", "arn", "name", "description", "owner", "status",
                 "execution_status", "failure_reason", "agent_id", "agent_name",
                 "matched_agent", "detailed", "evaluators", "sampling_percentage",
                 "session_timeout_minutes", "filter_count", "filters", "data_source", "mode",
                 "insights", "clustering_frequencies", "execution_role_arn",
                 # CN additive (P3.2): the judge snapshotted at create | null
                 "judge_model_id", "judge_inference",
                 "results_log_group", "duplicate_enabled", "created_at", "updated_at", *CREATOR,
                 "owner_run"}  # CN additive (N17-obs445): an arm config's owning run | null
ONLINE_DATA_SOURCE = {"log_groups", "service_name"}
PIPELINE_OUT = {"id", "name", "description", "config", "status", "last_run", "created_at",
                "updated_at", *CREATOR}
# N13-obs2: `source_experiment_id` left with the retired configuration experiment
CANARY_OUT = {"id", "name", "champion_agent_id", "champion_agent_name", "challenger_agent_id",
              "challenger_agent_name", "status", "stage", "stages",
              "artifacts", "running_action", "progress", "error", "created_at", *CREATOR,
              # CN additive (N17-obs447): the running line's stable code + params | null
              "progress_code", "progress_params"}
PROVIDER_OUT = {"id", "label", "requires_source", "supports", "models", "default_model_id",
                "enabled",  # N12 E2-4 (plan §B.5): the picker greys out a disabled provider
                "target_evaluator"}  # CN additive (N17-obs453): optimises toward one evaluator
RUNTIME_CANARY_STAGES = ("setup", "traffic", "verdict", "ramp", "complete", "rollback",
                         "cleanup")


def test_dataset_out():
    row = EvalDataset(id="ds1", workspace_id="default", name="d", kind="predefined",
                      locale="en", description="", items=[{"prompt": "hi"}], cloud=None,
                      created_at=NOW)
    assert set(routers._dataset_out(row)) == DATASET_OUT


def test_evaluator_out_for_every_definition():
    # E1-1: the evaluator is an ``eval_evaluators`` row (CA-3); the key-set is the baseline's
    judge = EvalEvaluator(id="j", workspace_id="default", name="j", definition="judge",
                          instructions="x", rating_scale=[], model_id="m")
    derived = EvalEvaluator(id="d", workspace_id="default", name="d", definition="derived",
                            base_evaluator_id="Builtin.Helpfulness", model_id="m")
    code = EvalEvaluator(id="c", workspace_id="default", name="c", definition="code",
                         lambda_arn="a", lambda_timeout_s=1)
    for row in (judge, derived, code):
        assert set(routers._evaluator_out(row)) == EVALUATOR_OUT
    assert set(evaluators_local.builtin_out("Builtin.Helpfulness")) == EVALUATOR_OUT


def test_run_out():
    row = EvalRun(id="run1", workspace_id="default", agent_id="a", agent_name="n",
                  evaluators=[], created_at=NOW, updated_at=NOW)
    assert set(routers._run_out(row, snapshot=None)) == RUN_OUT


def test_run_results_unavailable_and_available(monkeypatch, workspace):
    insights = EvalRun(id="r1", agent_id="a", agent_name="n", mode="insights",
                       batch_eval_id="b", status="completed")
    out = service.run_results(insights, workspace=workspace)
    assert set(out) == RUN_RESULTS_BASE | {"reason"} and out["available"] is False

    done = EvalRun(id="r2", agent_id="a", agent_name="n", mode="evaluators",
                   batch_eval_id="r2", status="completed", session_ids=["s1"])
    from app.core.db import SessionLocal
    from app.evaluation import results_store

    with SessionLocal() as db:  # E1-4: the records are eval_results rows
        results_store.persist(db, "default", source="run", owner_id="r2", records=[
            {"session_id": "s1", "evaluator_id": "Builtin.Helpfulness", "score": 1.0}])
    out = service.run_results(done, workspace=workspace)
    # CN additive (eval accuracy item 1): the run-level and per-session extraction checks
    assert set(out) == RUN_RESULTS_BASE | {"extraction_check"} and out["available"] is True
    assert set(out["sessions"][0]) == {"session_id", "results", "extraction_check",
                                       "scenario_id", "scenario_label",  # N17-obs391
                                       "turns"}  # D3-c: {defined, replayed, judged}
    assert set(out["sessions"][0]["results"][0]) == RUN_RESULT_ROW


def test_result_record_normalizer_shape():
    assert set(results_store.normalize_result_record({})) == RESULT_RECORD


def test_queue_state(client):
    assert set(client.get("/api/eval/queue").json()) == QUEUE_STATE


def test_online_config_normalize():
    aws = {"onlineEvaluationConfigId": "oe1", "onlineEvaluationConfigArn": "arn",
           "onlineEvaluationConfigName": "n", "status": "ACTIVE", "executionStatus": "ENABLED"}
    for detailed in (True, False):
        out = online._normalize(aws, owner="agent", row=None, detailed=detailed)
        assert set(out) == ONLINE_CONFIG
        assert set(out["data_source"]) == ONLINE_DATA_SOURCE


def test_pipeline_out():
    row = EvalPipeline(id="p1", workspace_id="default", name="p", description="", config={},
                       status="idle", last_run=None, created_at=NOW, updated_at=NOW)
    assert set(pipeline_routers._pipeline_out(row)) == PIPELINE_OUT


def test_canary_out_and_stages():
    row = RuntimeCanary(id="c1", workspace_id="default", name="c", champion_agent_id="a",
                        champion_agent_name="n", challenger_agent_id="a",
                        challenger_agent_name="n", status="running", stage="setup",
                        artifacts={}, created_at=NOW)
    out = canary_routers._out(row)
    assert set(out) == CANARY_OUT
    assert tuple(out["stages"]) == RUNTIME_CANARY_STAGES


def test_provider_descriptions():
    described = providers.describe_providers()
    assert described and all(set(p) == PROVIDER_OUT for p in described)
    # CN additive (N-R6-obs1): `available` = the model's provider is configured here
    assert all(set(m) == {"model_id", "label", "available"}
               for p in described for m in p["models"])


