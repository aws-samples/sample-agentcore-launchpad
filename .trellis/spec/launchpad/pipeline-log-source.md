# Pipeline Log Source (数据处理 · 运行日志)

> A V2 数据处理 pipeline can read CloudWatch log groups instead of observed
> traces. A format rule converts each log record into `{role, text}` turns; the
> rest of the pipeline (exchange extraction, min chars, dedupe, dataset write) is
> the trace path unchanged. Status: Active (2026-10-10).

## Why logs only feed datasets

AgentCore batch evaluation reads OTel spans only, so custom-format logs can never
be evaluated directly. Their value is logs → dataset (questions + reference
replies) → replay against a platform agent. That is exactly a pipeline's job.

## Scope (phase 1)

- Presets: `genai` (OTel GenAI content records, e.g. `otel-rt-logs`), `message`
  (one record = one message), `exchange` (one record = one question/answer).
- Field addressing is **JSON dot paths only** (`payload.messages.0.text`); no
  user regex (ReDoS on the backend). A line with a text prefix before the JSON
  (`INFO 2026-… {...}`) is decoded from its first `{`.
- The format rule is embedded in the pipeline config (no separate resource).
- Not in phase 1: unstructured stdout, regex parsing, tool-call /
  `expected_trajectory` extraction, LLM-suggested rules.

## Config contract (`PipelineBody.source`)

```jsonc
{
  "type": "logs",                       // absent on pre-existing rows = "traces"
  "range": "24h", "max_sessions": 20,   // shared with traces
  "log_groups": ["/my/app"],            // 1..10, LOG_GROUP_NAME_RE
  "keyword": "chat_turn",               // optional, case-insensitive, ≤128
  "format": {
    "preset": "genai" | "message" | "exchange",
    "session_field": "conversation_id" | "@logStream" | null,
    "role_field": "role", "text_field": "content",          // message
    "user_roles": ["user", "human"], "assistant_roles": ["assistant", "ai", "bot"],
    "input_field": "query", "output_field": "answer"         // exchange (output optional)
  }
}
```

- `agent` / `status` are trace-only filters; a logs source ignores them.
- `message` needs `session_field` + `role_field` + `text_field`; `exchange`
  needs `input_field` (`session_field` null = each record is its own scenario).
- Paths match `^(@logStream|[A-Za-z0-9_$-]+(\.[A-Za-z0-9_$-]+)*)$`, ≤128 chars.
- `processing.keep_replies` (default true, both sources): false drops
  `expected_response` / `expected` (inputs-only dataset).

## Read path (`app/evaluation/pipeline_logs.py`)

1. One Logs Insights query over the log groups for the range:
   `fields @timestamp, @message, @logStream | filter @message like /(?i)kw/ |
   sort @timestamp desc | limit 10000`. Hitting the cap sets `truncated`.
2. Parse each record (JSON, prefix-tolerant); failures are counted by reason
   (`not_json`, `no_session`, `no_text`, `other_role`, `not_genai`).
3. Group by session key; the newest `max_sessions` sessions win; each session's
   records are ordered oldest-first by `@timestamp`.
4. Turns: `genai` reuses `observability.content_records_turns` (one invocation =
   one traceId; latest user input + `end_turn` reply — records repeat history);
   `message` maps roles via the role lists (case-insensitive); `exchange` emits a
   user turn + an assistant turn per record.
5. `pipelines.items_from_transcript(..., source="logs")` → scenario id
   `log-<session>` (records without a session: `log-<sha1(stream|ts|input)[:16]>`,
   stable across runs so dedupe holds), `metadata.source = "logs"` plus
   `log_group`.

Private-session visibility (`PrivateSessions.visible`) applies to the session
key exactly as for traces.

## Preview

`POST /api/eval/pipelines/preview-logs` `{source}` →
`{events, truncated, parsed, failed{reason: n}, sessions_found, samples[≤5 raw
{stream, timestamp, message≤2000}], sessions[≤8 {session_id, records, turns}],
paths[≤60 dotted paths seen in parsed samples]}`. Preview and run share the
converter, so the preview is what a run will produce.

## Run outcome

`last_run` gains `log` `{events, parsed, failed, truncated}` for a logs run;
`scanned` = sessions found, `matched` = sessions taken.
