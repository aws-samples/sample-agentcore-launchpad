# Execution notes — fix container OTEL events import crash

## Verification ladder actually run

1. **Root cause reproduced.** `aws-opentelemetry-distro>=0.10,<1` resolves to
   `0.19.0` → `opentelemetry-api/sdk 1.44.0`, where `opentelemetry._events` is
   gone. Same `ModuleNotFoundError` as the container's CloudWatch traceback.
2. **Baseline failure reproduced on AWS** before touching anything:
   `POST /api/chat/<lab-fund-packager>` → `RuntimeClientError: An error occurred
   when starting the runtime.`
3. **Wire-equivalence proven** against the removed API. The probe ran the real
   template module and the old `Event` path into one `InMemoryLogExporter` on
   `opentelemetry 1.43.0` (the last wheel in reach that ships both) and compared
   the exported records field by field → `EQUIVALENT: True` on scope, resource,
   body, attributes (incl. `event.name`), severity_number, trace_id, span_id,
   trace_flags, timestamp.
4. **Pinned set resolves and imports** on a clean py3.12 venv: the module that
   used to crash imports fine on `opentelemetry 1.44.0`, and emits the identical
   record shape there.
5. **Real rebuild + invoke** of `lab-fund-packager` (see `prd.md` R5).

## Why `event_name` is deliberately unset

Upstream's replacement for `Event(name=…)` is the `LogRecord.event_name` field.
The old events API never populated it — it only merged `event.name` into
attributes — so setting it would add a field to records that AWS-side evaluation
parsing has never been observed to accept. The equivalence probe is the whole
argument for keeping the record byte-identical; adopting `event_name` should
follow a live AWS-side verification, not precede it.

Related trap found by the linter: `LogRecord(trace_id=…, span_id=…,
trace_flags=…)` is *itself* deprecated (since 1.35.0). Using it would have
re-committed the exact sin being fixed, so correlation goes through
`context=set_span_in_context(span)` — which also pins the record to the span the
caller passed instead of the ambient current span.

## Still open (recorded, not fixed)

- **`strands_agent` and `strands_a2a_agent` templates keep
  `aws-opentelemetry-distro>=0.10,<1`.** Neither imports a private OTel API, so
  neither is broken today — but the open minor range is the same drift hazard.
  Left alone on purpose: those templates are 方式C's build path (the lab's whole
  main line), and pinning them without a real zip deploy + invoke would be the
  same unverified change that caused this defect. Worth its own task.
- **Deploy reports green while the runtime cannot start.** The user explicitly
  deferred the post-deploy smoke invoke this turn (it would let a transient
  invoke failure fail a healthy deploy). Kept as a follow-up in the issue file.

## Spec-update judgment (Trellis 3.3)

**No spec change.** `.trellis/spec/launchpad/evaluation-agent-eligibility.md`
states the load-bearing contract — `strands.telemetry.tracer` is the only
evaluation-parseable scope — and the fix preserves it exactly; that is what the
equivalence probe measured. No other spec describes the template's telemetry
mechanics, and no API, schema, ledger, or frontend surface moved. The defect and
its fix belong in `docs/issues/`, which is where the repo already records them.
