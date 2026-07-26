# New Claude SDK container builds crash on start: `opentelemetry._events` gone

| | |
|---|---|
| **Status** | **Fixed 2026-07-26** — verified by a real rebuild + invoke |
| **Severity** | High (every newly built 方式A container agent was un-invokable) |
| **Component** | `backend/app/templates/claude_sdk_agent/` (`tracing.py`, `requirements.txt`) |
| **Affected area in Launchpad** | Create Agent 方式A (Claude Agent SDK container) -> Chat / `/v1` invoke |
| **Date recorded** | 2026-07-26 |

## Summary

A container agent created on 2026-07-26 (`lab-fund-packager`) **deployed
successfully** — all five pipeline stages green, CodeBuild pushed
`launchpad-agents:lab-fund-packager-v1`, runtime reached `READY` — but every
invocation failed immediately:

```
RuntimeClientError: An error occurred (RuntimeClientError) when calling the
InvokeAgentRuntime operation: An error occurred when starting the runtime.
Please check your CloudWatch logs for more information.
```

`/aws/bedrock-agentcore/runtimes/lab_fund_packager_88c7cd-fMOWwcBt9f-DEFAULT`
showed the container dying during import:

```
Traceback (most recent call last):
  File "/app/main.py", line 27, in <module>
    import tracing
  File "/app/tracing.py", line 34, in <module>
    from opentelemetry._events import Event, get_event_logger
ModuleNotFoundError: No module named 'opentelemetry._events'
```

## Root cause

`requirements.txt` pinned only a major range:

```
aws-opentelemetry-distro>=0.10,<1
```

`tracing.py` imported `opentelemetry._events` (`Event`, `get_event_logger`), an
experimental API that upstream deprecated in 1.39.0 and has since removed. A
fresh CodeBuild run resolved `aws-opentelemetry-distro 0.19.0` →
`opentelemetry-api/sdk 1.44.0`, where the module no longer exists, so the
generated telemetry module failed to import and took `main.py` down with it.

Existing images built before the upstream removal were unaffected — they carry
the older wheel — which is why previously deployed container agents (e.g.
`cc-knowledge`, `fs-verify-agent`) kept working. Only **new builds** broke.

## Fix

1. **`tracing.py` migrated off the events API onto the logs API.**
   `get_logger(EVAL_SCOPE)` + `LogRecord(...)` now do by hand the two things the
   old SDK `EventLogger` did on our behalf: merge `event.name` into the record
   attributes, and emit through a logger carrying the evaluation-parseable
   instrumentation scope.

   Two deliberate choices, both documented in the module:
   - **`LogRecord.event_name` is left unset.** It is upstream's canonical
     replacement for `Event(name=…)`, but the old path never populated it, and
     AWS-side evaluation parsing has only ever been verified against the
     attribute form. Adopting it should follow a live verification.
   - **Correlation goes through `context=set_span_in_context(span)`**, not the
     `trace_id`/`span_id`/`trace_flags` kwargs — those are themselves deprecated
     since 1.35.0, so using them would have re-created this same failure mode
     one release later.

2. **Template dependencies pinned to tested minor windows**
   (`aws-opentelemetry-distro==0.19.*`, `claude-agent-sdk==0.2.*`, alongside the
   existing `bedrock-agentcore==1.17.*`), with a comment naming the failure mode.
   CodeBuild resolves these fresh on every image build, so an open upper bound is
   exactly how a green deploy turns into an agent that fails every invoke.

3. **Regression cover** in `backend/tests/test_claude_sdk_template.py`: the
   forbidden import is asserted against the module's *parsed* imports (its
   docstring names the removed API when explaining why it is gone), the pins are
   asserted, and two tests emit through an in-memory exporter to lock the record
   shape — scope, `event.name`, `session.id`, severity, span correlation, and the
   `input`/`output` message bodies.

### Wire-shape equivalence

The migration had to keep the record byte-identical, since AgentCore Evaluations
parses it AWS-side. Verified by running the real template module and the old
`Event` path into a single `InMemoryLogExporter` on `opentelemetry 1.43.0` (the
last wheel shipping both APIs) and diffing the exported records field by field —
scope, resource, body, attributes, `severity_number`, `trace_id`, `span_id`,
`trace_flags`, `timestamp` — all equal.

## Verification (real AWS, us-west-2)

| Step | Result |
|---|---|
| Baseline invoke before the fix | `RuntimeClientError` · import traceback at 10:18:09 UTC |
| Re-publish `lab-fund-packager` | CodeBuild arm64 1.7m → `UpdateAgentRuntime` → runtime **v2** `READY` |
| Container start after the fix | clean startup at 10:21:24 UTC, no traceback |
| Real invoke | streamed a correct answer in **5.5 s** |
| Spans | 7 spans incl. the manual `invoke_agent lab-fund-packager` and `chat global.anthropic.claude-sonnet-4-6` (3 in / 58 out tokens) |
| Content event in CloudWatch | scope `strands.telemetry.tracer`, `event.name` attribute, `session.id`, span-correlated, `body.input.messages` + `body.output.messages` in the required shape |
| Console trace drawer | `invoke_agent` span shows both message sides (the console's own parser, end to end) |

## Still open

- **Deploy reports green while the runtime cannot start.** The pipeline has no
  post-deploy smoke invoke, so a container that dies on import is recorded as an
  `active` agent. That reporting gap is what made this defect confusing, and it
  is **deliberately still open** — adding a smoke gate changes deploy semantics
  for every container agent (a transient invoke failure could fail a healthy
  deploy) and needs its own decision.
- **`strands_agent` / `strands_a2a_agent` templates still carry
  `aws-opentelemetry-distro>=0.10,<1`.** Neither imports a private OTel API, so
  neither is broken today, but the open minor range is the same drift hazard.
  Pinning them warrants its own task with a real 方式C deploy + invoke behind it.

## How it was found

While running the hands-on lab end to end (`docs/lab/`, task
`07-26-hands-on-lab-guide`). The lab guide records the failure verbatim in
[docs/lab/05-chat-memory.md](../lab/05-chat-memory.md#关于容器-agent-调用失败本次实测)
so readers who hit it on an unfixed checkout are not left guessing; the lab's
main line (zip runtime + harness) never depended on the container agent.
Fixed under task `07-26-fix-container-otel-events`.
