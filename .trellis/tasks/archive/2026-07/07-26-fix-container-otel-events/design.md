# Design — fix container OTEL events import crash

## Evidence gathered before designing

All of this was reproduced locally in throwaway venvs (`uv venv --python 3.12`),
which resolves exactly what CodeBuild's `pip install -r requirements.txt` would:

| Probe | Result |
|---|---|
| `aws-opentelemetry-distro>=0.10,<1` resolves to | `0.19.0` → `opentelemetry-api/sdk 1.44.0` |
| `import opentelemetry._events` on 1.44.0 | `ModuleNotFoundError` — reproduces the container crash |
| `import opentelemetry._logs` on 1.44.0 | OK |
| Upstream deprecation text (1.39.0) | "You should use `LogRecord` with the `event_name` field set instead" |
| Old `sdk._events.EventLogger.emit` source (1.33.1) | builds an SDK `LogRecord` and calls `logger_provider.get_logger(name).emit(record)` |
| Old API `_events.Event.__init__` source (1.33.1) | `attributes = {**attributes, "event.name": name}`; nothing else |
| ADOT `_init_logging` in 0.10.1 vs 0.19.0 | both call `set_logger_provider(provider)`, both gated on `OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED=true`; only 0.19.0 dropped `set_event_logger_provider` |
| Old-vs-new record diff on 1.43.0 (ships both APIs) | **byte-identical** — see below |

The decisive probe emitted the same body through both paths into an
`InMemoryLogExporter` and compared the exported records field by field
(scope, resource, body, attributes, severity_number, trace_id, span_id,
trace_flags, timestamp): `"equivalent": true`.

Consumer-side check: the console reads only `spanId` + `body.{input,output}
.messages` (`app/services/observability.py::parse_message_events`), so it is
insensitive to the change. AgentCore Evaluations is AWS-side and unverifiable
from here, which is why the design targets byte-equivalence rather than
"upstream-canonical".

## Decision 1 — migrate to the logs API, don't downgrade the pin

Rejected: pinning back to a distro that still ships `_events` (e.g. `==0.13.*`).
It works today but keeps the template on an API upstream already removed, so the
next forced bump re-breaks it.

Chosen: `opentelemetry._logs.get_logger(EVAL_SCOPE)` +
`LogRecord(timestamp, trace_id, span_id, trace_flags, severity_number=INFO,
body, attributes={..., "event.name": EVAL_SCOPE})`.

This is a mechanical substitution of the two lines the old SDK EventLogger ran
on our behalf: add the `event.name` attribute, and emit through a logger carrying
the same instrumentation scope.

### Deliberately NOT setting `event_name`

Upstream's replacement for `Event(name=...)` is the `event_name` **field**. The
old path did not populate it (it only wrote the `event.name` attribute), so
setting it would add a field to records that AWS's evaluation parser has never
been observed to accept. R2 (unchanged wire shape) outranks following the
upstream idiom here, and the module docstring already warns that shape drift
breaks parsing silently. A comment records `event_name` as the follow-up to
adopt once AWS-side parsing is verified.

## Decision 2 — pin to tested windows, matching the file's own convention

`requirements.txt` already uses `bedrock-agentcore==1.17.*`. Extend that style:

| Dependency | Before | After | Why |
|---|---|---|---|
| `aws-opentelemetry-distro` | `>=0.10,<1` | `==0.19.*` | the unpinned minor range is the actual root cause |
| `claude-agent-sdk` | `>=0.2,<1` | `==0.2.*` | identical hazard: a future `0.3` could change the `ClaudeSDKClient`/`ResultMessage` surface `main.py` depends on, with the same deploy-green/invoke-broken signature |
| `bedrock-agentcore` | `==1.17.*` | unchanged | already pinned |

Note the migration also needs a floor, not just reproducibility:
`opentelemetry-sdk` only accepts an **API** `LogRecord` in `Logger.emit()` from
the release that introduced `ReadWriteLogRecord._from_api_log_record`. Probed
distro `0.13.0` (sdk 1.37.0) and up: works. `0.10.1` (sdk 1.33.1): the API
LogRecord has no `resource` attribute, so exporters break. `==0.19.*` sits well
inside the working range.

## Decision 3 — regression cover in `backend/tests/`

Extend `tests/test_claude_sdk_template.py` (already owns the template's
`test_tracing_module_compiles_and_uses_eval_scope`):

1. **Static guard** — assert `opentelemetry._events` does not appear in
   `tracing.py`, and assert the two pins are present in `requirements.txt`.
2. **Behavioral guard** — import the template module, point its module-level
   logger at an SDK `LoggerProvider` + `InMemoryLogExporter`, run
   `traced_invocation` → `record_result` / `record_tool_call`, and assert the
   exported record's scope, `event.name`, `session.id`, body shape, severity and
   trace/span correlation.

The behavioral test injects the provider by replacing the module attribute
rather than calling `set_logger_provider`, whose `Once` guard makes global state
order-dependent across a pytest session.

Backend venv currently ships `opentelemetry-api/sdk 1.43.0` (a transitive of
`bedrock-agentcore`), which supports the new call shape — no new backend
dependency needed.

## Decision 4 — real-AWS verification path

`lab-fund-packager` (kept alive by the parent task) is re-published from the
console/API. That re-runs `generate → package(CodeBuild) → provision → deploy →
register` against the fixed template, so the new image proves R1 and R5. Then a
real invoke plus a trace lookup proves the telemetry still lands.

Rollback shape: if the rebuilt image still fails, the previous image tag stays
in ECR and the runtime's prior version is untouched by a failed deploy, so the
agent is no worse off than it is today (already un-invokable).

## Blast radius

- `tracing.py` and `requirements.txt` ship **into generated build contexts only**
  (`assemble_build_context` copies them verbatim); nothing in the backend imports
  `tracing.py` at runtime.
- Existing container agents keep running their old images until re-published.
- No API, schema, ledger, or frontend surface changes. No migration.
