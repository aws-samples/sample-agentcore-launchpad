# Interface: observability (spans source, Chat trace rail)

Status: current as of 2026-10-16 (round 11, `cn/r11-h`). The Observability module is
`backend/app/services/observability.py` (Logs Insights aggregations, 60 s cache,
`run_insights_queries`, `_spans_source()`) with `backend/app/routers/observability.py`
(`/api/observability/*`); the session trace rail is `backend/app/services/traces.py`.
Contract for the module's AWS surface and for the CN-specific rules it must follow.

## Spans source

Every Logs Insights query starts from `_spans_source()`: `SOURCE logGroups(namePrefix:
['aws/spans', '/aws/bedrock-agentcore/runtimes/'])`, narrowed by
`Settings.observability_spans_source` (`both` | `spans` | `runtime_logs`). Per-agent
runtime groups hold application logs and OTEL log records mixed with spans, so a span
reader must filter for real spans (`ispresent(name) or ispresent(spanName)`), never on a
session-id substring alone. Ids are interpolated into query literals and are validated
by `SESSION_ID_RE` / `TRACE_ID_RE` first (router and service, defense in depth).

## Tokens only — no USD estimates (R13, N13-obs3)

Dashboard tiles / `tokens_by_model`, trace rows, span rows, trace meta, session rows and
the session summary carry **token counts only** (`tokens.{input,output,total}` plus
`cache_read` / `cache_write` where the span reports them). The `est_cost_usd` field, the
`prices_meta` block, `GET /api/observability/prices`, `POST /api/observability/prices/refresh`,
`Settings.model_prices*` and `services/model_prices.py` (litellm price map + refresher,
off since R10) were removed in round 13 by user decision. Cost reporting returns with the
per-person budgets family (gap report Family A: `docs/cn/reports/upstream-gap-analysis-2026-10-16.md`
§1), where model prices are admin-maintained platform settings — not a scraped public file.

## Malformed rows in a trace (N16-obs53, rev-r16, round 16)

`GET /api/observability/traces/{trace_id}` builds the waterfall from the `@message` rows of
one Logs Insights query. A row that is not an OTEL span object (a plain-text log line, a
JSON array / string / number / `null`, or a span whose `attributes` / `status` / `resource`
is not an object — a correlated log record, a truncated export) used to be a bare 500; it is
now skipped or rendered with empty fields, and **never silently**: `meta.malformed =
{rows, attrs, status, resource}` counts every skipped row and coerced field (all `0` for a
clean export), and the service logs ONE `WARNING` per trace naming the shapes
(`launchpad.observability`: `trace <id>: malformed span rows … rows=1, attrs=2`) — shapes
only, never row content. A non-zero count means the waterfall shows fewer or barer spans
than the trace had; the console may surface it as a notice. Pinned by
`backend/tests/property/test_trace_properties.py::test_a_malformed_span_row_never_turns_the_trace_detail_into_a_500`
(ten row shapes, the count per shape and the single warning).

## Credential attributes are masked (N17-obs307, round 17)

ADOT's botocore instrumentation (`aws-opentelemetry-distro`, `patches/_botocore_patches.py`)
records the caller's STS access-key id as `aws.auth.account.access_key` on every AWS SDK
span a runtime emits; the attribute is unconditional (no OTEL setting turns it off). The
read side masks it: one helper, `backend/app/services/strands_eval/redaction.py`, replaces
the value of any key matching `access_key` / `secret` / `session_token` / `authorization` /
`x-amz-security-token` / `password` (case-insensitive, any nesting level) with
`[redacted]` — the key stays, so an operator sees that something was removed — and masks
any `A(S|K)IA[A-Z0-9]{16}` access-key id inside string values. Applied at:

- `observability.build_span_tree` — every span's `attributes` in
  `GET /api/observability/traces/{trace_id}` (console 可观测 → trace → 原始属性), masked
  before the trace cache. The trace/session list views, the session detail and the Chat
  trace rail return no raw attributes.
- `LaunchpadSpansProvider._run_logs_insights_query` — every record the evaluation engine
  maps into a strands `Session` (judge prompts, detectors, recommendation trajectories),
  on the backend and in the evaluation worker zip, which now ships `redaction.py`.
- `evaluation/log_streams._snippet` — the evaluation-task log-stream keyword excerpt,
  masked before the excerpt is cut.

Recommendation tool evidence and from-sessions datasets read named fields only (tool
name / call id / status / description, transcript turns), never the attribute bag. Spans
already in CloudWatch keep the raw value; the CloudWatch console is outside the platform.
Pinned by `backend/tests/test_span_redaction.py`,
`tests/test_observability.py::test_trace_detail_masks_credential_attributes`,
`tests/evaluation/test_spans_provider.py::test_records_reach_the_mapper_with_credentials_masked`
and `tests/evaluation/test_log_streams.py::test_snippet_masks_access_key_ids_before_the_cut`.

## Session transcript sources (`observability.session_transcript`, N17-obs432)

The session detail (`GET /api/observability/sessions/{id}`), the evaluation result drawer
(`.../transcript`) and the trajectory picker (`POST /session-previews`) share one reader.
It tries these sources in order:

1. **A chat ledger row** (console chat): Memory events under the agent-scoped actor,
   reconciled against `chat_messages`. `source: "chat"`.
2. **An evaluation run that replayed the session** (`replay_run_of`): the session id is a
   key of a run's `scenario_map`, i.e. a dataset scenario's pre-assigned session. The run
   names the agent it drove (over any `agent_id` the caller passed) and supplies the
   content-log start (its `created_at`) when the caller sent no `since`. Memory is read
   first, then that agent's runtime content records (`eval_turns_from_content_logs`). These
   are the same records the run's `extraction_check` compares, so a memory-disabled agent's
   replay still shows exactly what was asked and answered. The answer is never rebuilt from
   the dataset's `expected_response`. `source: "eval"`, `run_id` = the run. A session that is
   only listed in a run's `session_ids` (a run over existing sessions) is not claimed this
   way. A scenario retried under the same session id (N17-obs323) shows every attempt's
   turns, because each was really sent.
3. **Anything else** (`/v1`, direct invokes): Memory probes, then the content logs when the
   caller gave the agent and the start. `source: "external"`. With nothing found:
   `{available: false, reason: "not_platform_session"}`.

Visibility: the run is used only when the viewer may see it (`visibility.sees` on its
`created_by`). Under `isolated`, a member who cannot see the run gets the transcript route's
existing `404 observability.session_not_found`, even on an agent they do see. The session
detail and the previews read such a session as before (no run, so
`not_platform_session` unless the caller supplied the agent and start). The run's agent is in
the payload, so a hidden agent is the existing 404 too. Pinned by
`tests/test_observability.py::test_transcript_replay_*` and
`tests/test_agent_visibility.py::test_replay_session_transcript_follows_the_run_visibility`.

## Token counts include the prompt cache (N17-obs439)

Every token block (trace rows, session rows, a trace's `meta`, a session's `summary`, the
dashboard tile, `tokens_by_model`) is `{input, output, cache_read, cache_write, total}`.
- `input` is the providers' UNCACHED prompt tokens (Bedrock's `inputTokens`).
- `total` = input + output + cache read + cache write. That is what Bedrock's own
  `total_tokens` adds up to on every real record checked: 55 Kimi K3 / GLM-5.3 span records
  from research17 (six with cache writes) and live spans on the default host, 2026-10-25.

Where the cache counts live. Strands emits each Bedrock call twice:
- the terminal provider span (`gen_ai.system=aws.bedrock`) carries input / output only;
- its wrapper chat span (`gen_ai.system=strands-agents`) adds `cache_read_input_tokens`,
  `cache_write_input_tokens` and `total_tokens`.

The aggregates count the provider span, so a call's tokens are not counted twice. So the
cache counts read 0 and every total missed them: a live trace of 5,074 tokens showed 2,875.
Now:
- the Logs Insights queries (`_IS_LLM_FIELDS`, `q_trace_aggregates`, `q_session_aggregates`,
  `q_tokens_by_model`) also sum the cache counts over the wrapper chat spans
  (`wrapper_cache_read` / `wrapper_cache_write`);
- `_cache_tokens` prefers the provider sums and falls back to the wrapper's, the same rule
  `q_tokens_by_model` applies to input / output;
- the trace detail's span rollup does the same per component.

OpenInference spans (`llm.token_count.*`, native Claude SDK) count the WHOLE prompt in
`prompt`, with `prompt_details.cache_*` as its breakdown, so the uncached `input` is
prompt − cache. This follows the OpenInference semantic conventions; no such span with cache
counts has been recorded on this platform yet. The CloudWatch token metric (the dashboard's
fallback when the spans are empty) has no cache token type, so it reports none.

Console: wherever input is shown (dashboard tile and per-model bars, trace / Traces-page /
session token lines, a span's token block), "input" is the whole prompt
(`v2/judgeTokens.tokenSplit`: input + cache reads + cache writes), with a tooltip that splits
uncached / cache reads / cache writes. This is the same meaning SCORE NOW and the run's judge
line give it (N17-obs436). Pinned on the real records by
`backend/tests/test_observability_cache_tokens.py` (fixture
`tests/fixtures/obs439_cache_spans.json`) and
`frontend/src/v2/pages/observability/TraceDetail.test.tsx`.

## Chat trace rail — `GET /api/traces/{session_id}`

Restored in round 11 (N-CHAT-obs2, upstream parity). The route lives in
`routers/observability.py` as a second router with prefix `/api` (upstream kept the path
outside `/api/observability`; the frontend `chatApi.trace()` depends on it) and is
`MEMBER` in `core/route_policy.py`. The handler applies the chat-history ownership rule
before any query: the id must be a `chat_sessions` row of the caller's workspace, and
`PrivateSessions.require_visible` hides other principals' assistant sessions — both 404.
`services.traces.session_trace(session_id, workspace, lookback_hours)` runs ONE Insights
query through the shared source, keeps only JSON rows that are spans, and returns
`{session_id, span_count, spans[{name, category, start_ms, duration_ms, trace_id}],
log_groups, log_group, lookback_hours, cloudwatch_url, log_group_url, unavailable_reason}`.
A query failure is **not** an HTTP error for this side panel: `spans = []` and
`unavailable_reason = <AppError code>`.

## Partition-aware console deep links (C2)

The module never writes a console hostname. `WorkspaceContext.console_host`
(`aws_clients.console_host_for_partition`) maps the partition to the Management Console
host — `console.aws.amazon.com` (aws), `console.amazonaws.cn` (aws-cn),
`console.amazonaws-us-gov.com` — because console hosts do **not** follow `dns_suffix`
(`console.amazonaws.com.cn` does not exist). Regional URLs are
`https://{region}.{console_host}/cloudwatch/home?region={region}#…`.

- `traces.insights_url(workspace, session_id, log_groups, lookback_hours)` — Logs Insights
  pre-filled with the rail's own pipeline (editor text), the contributing log groups as
  `source` (fallback `aws/spans`), relative window `lookback_hours × 3600 s`. Encoding is
  the console's own: `?`/`=` as `$3F`/`$3D`; inside `queryDetail` strings every byte
  outside `[A-Za-z0-9._-]` is `*xx`; the structural `~ ( ) '` are double percent-encoded
  with `%` written `$` (`~` → `$257E`).
- `traces.log_group_url(workspace, log_group)` — the group page; `/` is `$252F`.

`tests/test_chat_trace_route.py` pins both hosts and the encoding. `scripts/cn_lint.sh`
flags `console.aws.amazon.com` / `signin.aws.amazon.com` (plain or regex-escaped) since
cn/r11-i — until then it matched only `arn:aws:` / `.amazonaws.com`, which is how the
literal survived in `services/traces.py` for months — so the table in `aws_clients.py`
is the only place these literals may appear (marked `cn-lint: allow`).

## Frontend

V2: `frontend/src/v2/pages/chat/TracePanel.tsx` (tab `trace` of `Inspector`, default tab
stays `memory`), loaded by `v2/pages/Chat.tsx` when the TRACE tab looks at a session it
has no trace for (opening the tab, restoring a session, the first completed turn) and on
LOAD TRACE; links: OPEN IN OBSERVABILITY (`/v2/observability?tab=sessions&view=session&id=`)
and CloudWatch (`cloudwatch_url` verbatim). Locales: `v2.chat.tabTrace / traceLoad /
traceIdle / traceEmpty / tracePlaceholder / traceMore / traceUnavailable / openInObs /
spanCat.*` and `chatPage.traceTitle / tracePlaceholder / traceLoad / traceEmpty / openInObs`.

## Live re-test (pending)

On `default`: send one message to an active agent, open Inspector → TRACE, LOAD TRACE
about a minute later → `span_count ≥ 1`; the CloudWatch link must open
`cn-northwest-1.console.amazonaws.cn` with the query pre-filled and the session's spans
listed. Record the result on the N-CHAT-obs2 row of `docs/cn/reports/bugs-round-5.md`.
