"""Evaluation ledger models (adapted from agentcore_eva_opt db.py row shapes)."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Index, Integer, String, Text, event, inspect
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


def _id() -> str:
    return uuid.uuid4().hex[:12]


def _now() -> datetime:
    return datetime.now(UTC)


def _evaluator_id() -> str:
    return "cust_" + uuid.uuid4().hex[:12]


class EvalDataset(Base):
    __tablename__ = "eval_datasets"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    # the `cloud` sync below targets one region, so the row belongs to one workspace
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default="legacy")  # legacy|predefined
    locale: Mapped[str] = mapped_column(String(8), default="en")
    description: Mapped[str] = mapped_column(Text, default="")
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    # legacy items: [{"prompt": str, "expected": str|None}]
    # predefined items (devguide scenario schema): [{"scenario_id", "turns":
    #   [{"input", "expected_response"?}], "expected_trajectory"?: [str],
    #   "assertions"?: [str], "metadata"?: {}}]
    cloud: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # cloud: {dataset_id, arn, status, synced_at, failure_reason, draft_status
    #   (MODIFIED|UNMODIFIED), example_count, versions: [{version, example_count,
    #   created_at}]} — the row's one AWS Dataset (draft edited in place on re-sync)
    # N17-obs407: the agent the scenarios are written for (optional) — its runtime tool
    # names gate every ``expected_trajectory`` at create / edit (`evaluation/agent_tools.py`)
    agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # D3-c: the local dataset's version — 1 at creation, +1 on every change of `items` by any
    # writer (the `set` listener below), so a run can record what it replayed
    revision: Mapped[int] = mapped_column(Integer, default=1)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


@event.listens_for(EvalDataset.items, "set")
def _bump_revision(target: EvalDataset, value: Any, old: Any, _initiator: Any) -> None:
    """D3-c: a persisted dataset whose items are replaced moves to the next revision (the
    constructor's own assignment and an identical re-assignment do not)."""
    if inspect(target).persistent and value != old:
        target.revision = (target.revision or 1) + 1


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # "" for a CloudWatch-sourced run (no platform agent): see ``log_source``
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    # the agent's name — or, without an agent, the telemetry service name
    agent_name: Mapped[str] = mapped_column(String(64))
    # {service_name, log_group_names}: the run reads an agent's telemetry straight
    # from CloudWatch Logs instead of a platform agent (NULL for agent runs)
    log_source: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Operator-facing task name/description (console V2 evaluation tasks); NULL on
    # runs started before they existed or from a surface that does not name them.
    name: Mapped[str | None] = mapped_column(String(64), default=None)
    description: Mapped[str | None] = mapped_column(Text, default=None)
    dataset_id: Mapped[str | None] = mapped_column(String(16), default=None)
    dataset_name: Mapped[str | None] = mapped_column(String(64), default=None)
    # Published cloud-dataset version the run replayed ("2"); NULL = the DRAFT
    # (and every local / session / window run).
    dataset_version: Mapped[str | None] = mapped_column(String(16), default=None)
    mode: Mapped[str] = mapped_column(String(12), default="evaluators")  # evaluators|insights
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="queued")
    # queued | invoking | waiting | evaluating | completed | failed | stopped
    queue_position: Mapped[int] = mapped_column(default=0)
    session_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    batch_eval_id: Mapped[str | None] = mapped_column(String(80), default=None)
    scores: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    insights: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    # Dataset scenarios the agent ran out of budget on ({scenario_id, session_id, code,
    # stop_reason}): scored as they are instead of failing the run; NULL on older rows.
    budget_stops: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # Upstream #251 parity (02607ab4, N15-obs4): simulated persona scenarios the worker
    # skipped after their replay attempts while the run went on — ``[{scenario_id,
    # retries, code, error}]`` (``retries`` a string, as upstream serialises it), folded by
    # ``service._finish`` from the ``simulated`` entries of the jobs' ``replay_failures[]``.
    # NULL when none / on older rows. The same scenarios are also in ``session_failures``
    # as kind ``replay_failed`` — this is the per-persona view the task detail lists.
    scenario_failures: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # N12-bug4: evaluator records (insights: sessions) that errored on the worker —
    # ``{count, total, first_type, first_message}``; None when none did. All errored →
    # the run is ``failed``; some → ``completed`` with ``error`` carrying the summary.
    record_errors: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # N12-obs47 (upstream 00eb359b parity): the SESSIONS a partially failed evaluators run
    # left unscored, and why — ``{total, failed, kind, sessions: [{session_id, index, kind,
    # error_type, message, arm}]}`` (``service.summarize_session_failures``; ``index`` = 1-based
    # dataset scenario position, ``kind`` telemetry_incomplete | replay_failed |
    # evaluator_error, run kind folds to one of those | mixed; ``arm`` = the session's
    # pre-assigned C | T1, null for a plain run — N12-obs62). NULL on clean / older rows.
    session_failures: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # Eval accuracy item 1: the extraction reconciliation of the evaluated sessions —
    # ``{summary: extraction_check.summarise(...), sessions: {session_id: extraction_check}}``
    # folded by ``service._finish`` from the jobs' ``done.json`` ``extraction_checks``; NULL
    # on insights runs, runs that evaluated nothing and older rows.
    extraction_check: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # P3.1 / P3.2 (N17-obs349 / obs350): the built-in judge the run scores with (chosen per
    # run, default the `judge` model role at submit) and its effective inference config
    # (``judge_budget.judge_inference``: model_id, temperature | "provider default",
    # temperature_applied, max_tokens) — insights runs summarise with it too. NULL on older rows.
    judge_model_id: Mapped[str | None] = mapped_column(String(120), default=None)
    judge_inference: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # R3-f: expected vs judged units per evaluator, failures by class, the coverage and the
    # threshold verdict (``run_coverage.run_coverage``); NULL on insights runs and older rows
    unit_coverage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # P8.3 / P8.4: the run's judge tokens {inputTokens, outputTokens, totalTokens, budget,
    # exhausted} folded from the jobs' done.json; NULL when no worker reported any
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # N17-obs418: the newest worker heartbeat (``status.json`` ``updated_at``, ISO UTC) the
    # poller read while the run's jobs ran — a healthy slow run is seen to be alive
    last_heartbeat_at: Mapped[str | None] = mapped_column(String(40), default=None)
    # P7.1: the resolved evaluator definitions AT SUBMIT [{id, name, kind, version,
    # definition_hash, definition}] — the plan replays them, so an edit while the run waits
    # changes nothing; P7.6: the evaluation worker release the run was dispatched to
    evaluator_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    worker_release: Mapped[str | None] = mapped_column(String(40), default=None)
    # The agent version the run evaluated (`evaluation/scorecard.py`, upstream #289): the
    # newest `SpecSnapshot.seq` with an `aws_version` when the run was submitted (a newer
    # snapshot still deploying is not what the run invokes). NULL for a log-source
    # run, an agent with no deployed snapshot, and older rows (inferred on read).
    agent_snapshot_seq: Mapped[int | None] = mapped_column(Integer, default=None)
    # What a replay run replayed: the first 16 hex of sha256 over its normalized
    # scenarios (`scenarios.items_fingerprint`) and their count. NULL for passive
    # scopes and older rows; a cloud run also keeps `dataset_version`.
    dataset_fingerprint: Mapped[str | None] = mapped_column(String(16), default=None)
    dataset_size: Mapped[int | None] = mapped_column(Integer, default=None)
    # {session_id: {scenario_id, input}} of a replay run, written as each session
    # lands (`input` = the scenario's first prompt, ≤ 200 chars). A skipped persona
    # has no entry. NULL for passive scopes and older rows.
    scenario_map: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # N17-obs407: ``[{scenario_id, from, to}]`` — expected-trajectory names the run start
    # rewrote to the name the agent calls the tool by (the Gateway alias prefix added or
    # dropped); the dataset itself is untouched. NULL when none / on older rows.
    tool_name_rewrites: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    # D3-d / R3-c: the dataset split a replay run replayed — all | tuning | held_out (an
    # acceptance run); NULL for passive scopes and older rows (they replayed every scenario)
    dataset_split: Mapped[str | None] = mapped_column(String(12), default=None)
    # D3-c: `EvalDataset.revision` of the local dataset the run replayed (a cloud run keeps
    # `dataset_version`); NULL for cloud / passive scopes and older rows
    dataset_revision: Mapped[int | None] = mapped_column(Integer, default=None)
    # N17-obs407: non-blocking run warnings — ``[{kind: expected_tool_not_observed,
    # session_id, scenario_id, tools, observed, message}]`` for an agent whose tool names the
    # run start could not check (`service.trajectory_warnings`); NULL when none / older rows
    warnings: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class EvalRecommendation(Base):
    """One recommendation job pinned to an evaluation run's batch.

    The job itself (status, recommended text) lives on AWS; the row keeps the job id,
    the inputs the operator confirmed (they cannot be read back once a Harness is
    edited) and the last result read from GetRecommendation.
    """

    __tablename__ = "eval_recommendations"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    run_id: Mapped[str] = mapped_column(String(16), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # system_prompt | tool_descriptions
    recommendation_id: Mapped[str] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="PENDING")
    # where the inputs came from: harness (live GetHarness) | spec | manual
    input_source: Mapped[str] = mapped_column(String(16), default="manual")
    system_prompt: Mapped[str | None] = mapped_column(Text, default=None)
    evaluator: Mapped[str | None] = mapped_column(String(256), default=None)
    tools: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    # tools the job rejected as absent from the traces (dropped on the one retry)
    skipped_tools: Mapped[list[str]] = mapped_column(JSON, default=list)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # set once a system-prompt recommendation was accepted into a new Harness version:
    # {by, at, agent_id, job_id, deployment_id, previous_version}
    accepted: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class OnlineEvalConfig(Base):
    """Ledger row linking an AWS online evaluation config to the agent it scores.

    Identifiers only — status, evaluators, rule and data source are read back from
    ``GetOnlineEvaluationConfig`` on every list/detail (AWS is the source of
    truth). Configs the console did not create (experiment arms, AWS console /
    CLI) have no row and are classified by name at read time.
    """

    __tablename__ = "online_eval_configs"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    agent_name: Mapped[str] = mapped_column(String(64))
    config_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    config_arn: Mapped[str] = mapped_column(String(256))
    name: Mapped[str] = mapped_column(String(48))
    service_name: Mapped[str] = mapped_column(String(128))
    log_group: Mapped[str] = mapped_column(String(256))
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # --- CN (N12, CA-3): the config is local truth — what AWS used to hold ---
    # N17-obs337: a log-source config (Global's cloudWatchLogs {logGroupNames, serviceNames})
    # watches 1..10 groups and has no agent (`agent_id` ""); NULL on agent configs, which
    # read their one runtime `log_group`
    log_groups: Mapped[list[str] | None] = mapped_column(JSON, default=None)
    description: Mapped[str] = mapped_column(Text, default="")
    mode: Mapped[str] = mapped_column(String(12), default="scores")  # scores|insights
    evaluators: Mapped[list[str]] = mapped_column(JSON, default=list)
    insights: Mapped[list[str]] = mapped_column(JSON, default=list)
    clustering_frequencies: Mapped[list[str]] = mapped_column(JSON, default=list)
    # P3.2 (N17-obs350): the built-in judge, snapshotted at create — every dispatch of the
    # series uses it, so a Settings → Model roles change no longer switches the judge mid
    # series. NULL on older rows (those keep reading the role at dispatch).
    judge_model_id: Mapped[str | None] = mapped_column(String(120), default=None)
    judge_inference: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    sampling_percentage: Mapped[float] = mapped_column(Float, default=10.0)
    session_timeout_minutes: Mapped[int] = mapped_column(Integer, default=15)
    filters: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    execution_status: Mapped[str] = mapped_column(String(10), default="ENABLED")  # |DISABLED
    status: Mapped[str] = mapped_column(String(10), default="ACTIVE")  # |FAILED
    failure_reason: Mapped[str | None] = mapped_column(Text, default=None)
    last_scanned_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # N17-obs444: the sampling floor set when a paused config is resumed (DISABLED → ENABLED);
    # NULL = never resumed, the floor is ``created_at``. Only activity after the floor is
    # sampled (``online_scheduler.sampling_floor``).
    sampling_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    # --- end CN (N12) ---


class EvalPipeline(Base):
    """A saved data-processing task (console V2 数据处理): which observed sessions
    to read, how to turn their transcripts into dataset items, and which local
    dataset receives them. Runs are on demand; the last outcome is kept on the row."""

    __tablename__ = "eval_pipelines"

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(Text, default="")
    # {source: {agent, range, status, max_sessions},
    #  processing: {first_turn_only, dedupe, min_input_chars},
    #  output: {dataset_id} | {dataset_name}}
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="idle")  # idle|running|succeeded|failed
    # {at, scanned, matched, added, skipped: [{session_id, reason}], dataset_id, error}
    last_run: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


# ── CN additive tables (N12 E0-4; plan §A.2, contract amendment CA-3) ────────────
# The AWS services that held evaluators, results and online samples do not exist in
# aws-cn, so the ledger holds what they held. W-E1 writes evaluators/results, W-E2 the
# online samples; W-E3 reads results per arm.


class EvalEvaluator(Base):
    """A custom evaluator (judge | derived | code) — the Global evaluator record,
    held locally. ``name`` is unique per workspace; built-ins are not rows."""

    __tablename__ = "eval_evaluators"
    __table_args__ = (
        # an INDEX rather than a table constraint so the pre-workspace migration
        # rehearsal (tests/test_ledger_migration.py) can drop `workspace_id`
        Index("uq_eval_evaluators_workspace_name", "workspace_id", "name", unique=True),
    )

    id: Mapped[str] = mapped_column(String(20), primary_key=True, default=_evaluator_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(12), default="TRACE")  # TOOL_CALL|TRACE|SESSION
    description: Mapped[str] = mapped_column(Text, default="")
    definition: Mapped[str] = mapped_column(String(8))  # judge|derived|code
    instructions: Mapped[str | None] = mapped_column(Text, default=None)
    rating_scale: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    model_id: Mapped[str | None] = mapped_column(String(128), default=None)
    base_evaluator_id: Mapped[str | None] = mapped_column(String(128), default=None)
    lambda_arn: Mapped[str | None] = mapped_column(String(256), default=None)
    lambda_timeout_s: Mapped[int | None] = mapped_column(Integer, default=None)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    # P7.1: the scoring definition's version — 1 at create, +1 whenever a field that changes
    # what is scored (definition, level, instructions, scale, model, base, Lambda) changes —
    # and its hash (`evaluators_local.definition_hash`); NULL on rows from before
    version: Mapped[int | None] = mapped_column(Integer, default=None)
    definition_hash: Mapped[str | None] = mapped_column(String(16), default=None)
    # The creating console principal (`app.services.creators`): `users.id` (NULL for
    # the config admin / auth-disabled operator, system paths and legacy rows) and
    # the username at creation, display only. Server-set, never from a payload.
    created_by: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    created_by_name: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class EvalResult(Base):
    """One evaluator judgement on one session (the Global results-stream record).

    ``id`` is the worker's deterministic ``record_id`` (``evaluation.jobs.record_id``),
    so re-ingesting a job's ``results.jsonl`` is idempotent. ``arm`` is filled from
    ``experiment_assignments`` at write time so A/B verdicts need no join at read time.
    """

    __tablename__ = "eval_results"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    source: Mapped[str] = mapped_column(String(10))  # run|online|ondemand
    run_id: Mapped[str | None] = mapped_column(String(16), index=True, default=None)
    config_id: Mapped[str | None] = mapped_column(String(80), index=True, default=None)
    session_id: Mapped[str] = mapped_column(String(256), index=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)
    # N17-obs324: the tool call a TOOL_CALL row judged (span id without the trace scope)
    span_id: Mapped[str | None] = mapped_column(String(64), default=None)
    tool_name: Mapped[str | None] = mapped_column(String(256), default=None)
    # ok | mismatch | unverifiable — the extraction check of the judged turn (N17-obs324)
    extraction_status: Mapped[str | None] = mapped_column(String(16), default=None)
    evaluator_id: Mapped[str] = mapped_column(String(256))
    level: Mapped[str | None] = mapped_column(String(12), default=None)
    score: Mapped[float | None] = mapped_column(Float, default=None)
    label: Mapped[str | None] = mapped_column(String(128), default=None)
    explanation: Mapped[str | None] = mapped_column(Text, default=None)
    error_type: Mapped[str | None] = mapped_column(String(64), default=None)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)
    arm: Mapped[str | None] = mapped_column(String(4), default=None)  # C|T1
    # P3.1 / P3.2: the judge that produced this row and its effective inference config
    # (``judge_budget.judge_inference``); NULL for trajectory / code evaluators, error rows
    # of a missing session keep their evaluator's judge, and older rows have none.
    judge_model_id: Mapped[str | None] = mapped_column(String(120), default=None)
    judge_inference: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # P8.3: the judge tokens of the unit this row judged (Global's result `tokenUsage`); on
    # the unit's first row only, NULL on deterministic / error / older rows
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, index=True
    )


class OnlineSample(Base):
    """A session the online scheduler picked for one config (dedupe + resume ledger)."""

    __tablename__ = "online_samples"
    __table_args__ = (
        # N17-obs422: one row per activity SEGMENT of a session (an idle-resume is a new one)
        Index("uq_online_samples_config_session_segment", "config_id", "session_id", "segment",
              unique=True),
    )

    id: Mapped[str] = mapped_column(String(16), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    config_id: Mapped[str] = mapped_column(String(80), index=True)
    session_id: Mapped[str] = mapped_column(String(256))
    status: Mapped[str] = mapped_column(String(12), default="queued")  # |evaluated|failed|skipped
    sampled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    error: Mapped[str | None] = mapped_column(Text, default=None)
    # Eval accuracy item 1: the sample's extraction reconciliation against the chat ledger
    # (``extraction_check.check`` shape); NULL until evaluated and on older rows
    extraction_check: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # P4.6: how many times the sample was dispatched (NULL on older rows = 1), and when a
    # transiently failed sample is due for its next attempt (NULL = no retry pending)
    attempts: Mapped[int | None] = mapped_column(Integer, default=1)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # N17-obs421: the judge tokens of the sample's rows, summed over its attempts
    # (``{inputTokens, outputTokens, totalTokens}``); NULL when no row reported any
    token_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    # N17-obs422: the session's activity segment this row samples (1 = the first; an
    # activity window after an idle gap longer than the session timeout is the next), the
    # last activity it covers (the scan's ``last`` when sampled) and the previous segment's
    # (its traces start after it; NULL for the first segment)
    segment: Mapped[int] = mapped_column(Integer, default=1)
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                           default=None)
    segment_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True),
                                                           default=None)
