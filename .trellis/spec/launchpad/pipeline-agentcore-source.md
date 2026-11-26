# 数据处理 — `agentcore` pipeline source (AgentCore dataset generation)

## 1. Scope / Trigger

Contract between `backend/app/evaluation/generation.py` (pure selection / request / keep
rules), `pipeline_routers._run_agentcore` (the run: start → poll → pull back), the AWS
wrappers in `agentcore_eval.py` (`start_dataset_generation`, `get_dataset_generation`,
`list_dataset_examples(generation_id=)`, `create_dataset(examples=[])`) and the V2/V3
pipeline editors. Touch this when changing how sessions reach `StartDatasetGeneration`,
how generated examples land locally, or the resume path. Introduced 2026-11-26 (adopting
the official API; local extractors stay for log sources and non-OTel agents).

## 2. Load-bearing facts (live on dev, 2026-11-26 spike)

- **Identifiers only.** A filter-based generation writes every matching session — other
  principals' private ones too — into the shared AWS Draft, and `DeleteDatasetGeneration`
  does not remove the examples it wrote. Sessions are selected locally (private-session
  rule, project fence) and sent as `selection.identifiers.sessionIds` (≤ 25).
- **The runtime's own log group is the source.** `aws/spans` alone fails every session
  with `SPANS_INCOMPLETE`; runtime log group alone works for Harness, ZIP runtime and the
  Claude-SDK container. Service name = `<runtime name>.<endpoint>` (Harness:
  `harness_<name>.<endpoint>`) — `resolve_telemetry` returns both.
- **Fresh sessions are not ready** (~10 min); some old sessions are permanently
  incomplete. `SPANS_INCOMPLETE` is per session: the run is `COMPLETED` with
  `errorDetails`; `FAILED` only when nothing was produced. `COMPLETED_WITH_ERRORS` was
  never observed.
- **Always send an explicit window**: the default is 7 days, listed ids outside it fail
  `SESSION_NOT_FOUND`; a window older than the group's retention is a sync
  `ValidationException`.
- **DYNAMIC** writes `actor_profile` (+ `known_info` / `unknown_info` strings,
  `max_turns: 30`) only for multi-turn sessions; single-turn ones give `input`-only items.
  No ground truth for DYNAMIC.
- **FIXED** examples: `turns[].input` string, `expected_response` per turn,
  `expected_trajectory` top-level tool names (`[]` when no tool ran) — validate unchanged.
- Every example carries `metadata.generation_id` and `metadata.source.session_id` (+ the
  source descriptor; `metadata.redaction` with redaction). `scenario_id == exampleId`, a
  32-hex id **not stable per session**.
- AWS dedupes against the Draft on **content + `metadata.source`**, not scenario_id — so
  store and sync examples verbatim (`scenarios.aws_examples`).
- One generation per dataset; while it runs, Draft writes and `CreateDatasetVersion` →
  `ConflictException`. FIXED ≈ 16 s, DYNAMIC ≈ 21 s for ≤ 5 sessions; cold starts ~45 s.
- A Managed Harness inline-function (approval) session yields junk: the interrupt payload
  (`[{'id': 'v1:before_tool_call…`) as `expected_response`, the system prompt as the next
  `input` — dropped by `is_interrupt_transcript`.

## 3. Signatures

```python
# generation.py
select_sessions(rows, *, service, status, dynamic, present, limit, now)
    -> (picked newest-first, skipped[{session_id, reason: too_fresh|already_in_dataset}], matched)
build_source(cfg, *, service_name, log_group, sessions, keep_replies, now, retention_days)
    -> traceGeneration dict   # window = first − 5 min … min(now, last + 5 min), retention-clamped
keep_examples(examples, *, requested, validate) -> (kept, [(exampleId, skipped)])
merge(existing, kept, *, dedupe) -> (merged, taken, skipped)      # pipelines.merge_items per item
summary(GetDatasetGeneration) -> {status, results, error_details, redaction, failure_reason}
failed_sessions(GetDatasetGeneration) -> [{session_id, reason: generation_failed, code}]
generation_name(pipeline_id) / client_token(pipeline_id, run_at)  # AWS patterns; token deterministic
# pipeline_routers.py
PipelineSource.type == "agentcore": agent_id*, max_sessions ≤ 25, scenario_type FIXED|DYNAMIC,
    expected_trajectory, redaction{identifiers, custom[{name, regex}]}
GENERATION_TARGET_KIND = {"FIXED": "predefined", "DYNAMIC": "simulated"}
    # else 400 pipeline.target_kind_mismatch {scenario_type, kind}
_run_agentcore → _prepare_generation (select, sync/create target, persist request; False =
                 nothing to send, no AWS write)
              → _issue_start (persisted source + client_token → same generation on re-issue)
              → _finish_generation (poll w/ transient retry, re-list while < examplesWritten,
                 keep, delete strays from the Draft, merge, best-effort cloud blob)
last_run.generation = {id, cloud_dataset_id, created_dataset, local_dataset_id, scenario_type,
    stage: starting|generating|merging|done, status, requested, session_ids, results,
    error_details, redaction, failure_reason, request{source, client_token}, creator}
_claim(db, id): conditional UPDATE status!=running + live set under one lock (run route)
_reap(db, row, ws): under the same lock — refresh, still running, cloud_dataset_id + stage
    starting|generating|merging + (id or request) → resume on a thread; else fail the row
DELETE while running → 409 pipeline.running
skip reasons (selection): too_fresh, already_in_dataset, unsupported_session_id (AWS
    SessionId `[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}`), expired (older than the log retention)
```

### Evaluation-run source (P3)

```python
PipelineSource.batch_run_id   # EvalRun id: completed, mode evaluators, batch, same agent
PipelineSource.max_score      # 0..1, default 0.7; None = every scored session
generation.lowest_normalized(results)   # min over scored records; 1 - score when
                                        # ac.evaluator_polarity(id) < 0 (== v2 normalizedScore)
generation.select_run_sessions(session_ids, results, *, max_score, visible, present, limit)
    # skips: unscored, above_threshold, already_in_dataset, unsupported_session_id;
    # a private session of another principal is dropped silently (never named)
build_source(..., batch_arn=)   # sourceConfig.batchEvaluation, no log group / window
_generation_run(db, ws, source, viewer)  # 404 run.not_found | 400 pipeline.run_not_generatable
                                         # {reason} | 400 pipeline.run_agent_mismatch
```

Results come from `service.run_results` (the batch results stream); live check 2026-11-26:
`run_56c8b018` (energy harness) → 6 sessions, 4 below 0.9, 3 sent (cap) → 3 FIXED
scenarios with ground truth, 13 custom-regex redaction findings.

## 4. Invariants

- After a run the AWS Draft equals the local items: every generated example the local row
  does not hold (foreign session, interrupt, invalid, merge duplicate / cap) is deleted.
- The dataset write and the progress that records it commit together (`_save_progress`),
  so a resumed run never merges twice or creates the output dataset twice.
- A new-name output whose generation keeps nothing deletes the empty AWS dataset and
  fails `dataset.nothing_extracted`.
- Any failure after the request was persisted cleans up (`_cleanup_after_failure`): the
  run's own new AWS dataset is deleted; otherwise this generation's examples are deleted
  from the Draft — impossible while it still runs (ConflictException), which the error
  says (the next sync removes them; the local row never holds them).
- A new output dataset gets the generation's kind (`GENERATION_TARGET_KIND`), not the
  items' — a DYNAMIC run that produced only `input` items still yields `simulated`.
- The requester is the output dataset's creator, persisted on the generation for resume.

## 5. Tests

`backend/tests/evaluation/test_pipeline_agentcore.py` — `FakeAgentCore` keeps a Draft and
writes `generated` examples on the first terminal poll: selection, request shape, new /
existing targets, junk + foreign drop with Draft deletes, empty-output cleanup, FAILED,
ConflictException, restart resume (a `BaseException` "process death" inside `_spawn`).

## 6. Not covered / open

Strands container, BYOC, A2A and `live`-endpoint service names untested live; managed
redaction identifiers never matched real PII on dev; `onlineEvaluation` / `batchEvaluation`
sources (P3: bad cases → dataset) not wired yet.
