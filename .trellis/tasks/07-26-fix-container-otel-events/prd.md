# Fix container OTEL events import crash

Found during the hands-on lab run (parent task `07-26-hands-on-lab-guide`) and
recorded as `docs/issues/2026-07-26-container-otel-events-import.md`.

## Goal

Make newly built 方式A (Claude Agent SDK container) agents invokable again, and
stop the same class of silent dependency drift from breaking future builds.

## Problem

`backend/app/templates/claude_sdk_agent/tracing.py` imports
`opentelemetry._events`, an experimental API that upstream OpenTelemetry
deprecated in 1.39.0 and has since removed. `requirements.txt` pinned only
`aws-opentelemetry-distro>=0.10,<1`, so a fresh CodeBuild resolves a wheel
without that module and the container dies during `import tracing`:

```
File "/app/tracing.py", line 34, in <module>
    from opentelemetry._events import Event, get_event_logger
ModuleNotFoundError: No module named 'opentelemetry._events'
```

Deploy reports all five stages green; every invoke then fails with
`RuntimeClientError`. Images built before the upstream removal still work, so
the breakage only appears on new builds and re-publishes.

## Requirements

- **R1 — container starts.** A newly built 方式A image must start and serve
  `/invocations`.
- **R2 — telemetry unchanged on the wire.** The emitted gen_ai content records
  must keep the shape AgentCore Evaluations and the Observability console parse:
  instrumentation scope `strands.telemetry.tracer`, `body.input.messages` /
  `body.output.messages` as documented in the module docstring, the
  `event.name` attribute, `session.id`, severity INFO, and trace/span
  correlation to the enclosing span.
- **R3 — no silent drift.** Template dependencies must be pinned so a future
  upstream release cannot change the resolved API surface without a deliberate
  bump. Applies to `aws-opentelemetry-distro` and to `claude-agent-sdk`, which
  carries the identical `>=0.2,<1` hazard.
- **R4 — regression cover.** `make verify` must fail if the template goes back
  to the removed API or if the emitted record shape changes.
- **R5 — proven on real AWS.** Re-publish `lab-fund-packager` through CodeBuild
  and invoke it for real; confirm the runtime starts and produces gen_ai
  spans/events.
- **R6 — docs reconciled.** Update the issue file to its real post-fix state and
  correct the lab guide chapters that documented the failure
  (`docs/lab/03-deploy-harness.md`, `05-chat-memory.md`, `docs/lab/README.md`).

## Non-goals

- **No post-deploy smoke invoke in the pipeline.** Explicitly deferred by the
  user this turn: it would change deploy semantics for every container agent
  (a transient invoke failure could mark a healthy deploy failed). The
  "deploys green, invokes broken" reporting gap therefore remains open and must
  stay recorded in the issue file as a known follow-up.
- No changes to 方式B (harness) or 方式C (zip/Strands) telemetry — unaffected.
- No rewrite of the console-side trace/event parser
  (`app/services/observability.py`); R2 exists precisely so it needs none.

## Acceptance Criteria

- [ ] `tracing.py` contains no reference to `opentelemetry._events`
- [ ] Emitted record verified equal to the pre-fix record on scope, body,
      attributes (incl. `event.name`), severity, trace id, span id, trace flags
- [ ] `requirements.txt` pins `aws-opentelemetry-distro` and `claude-agent-sdk`
      to tested version windows, with a comment saying why
- [ ] New backend test covers the emitted shape and the forbidden import
- [ ] `make verify` passes
- [ ] `lab-fund-packager` re-published from a fresh CodeBuild image, invoked
      successfully, and its trace shows the gen_ai spans/events
- [ ] Issue file and the three lab documents reflect the fixed state; the
      deferred smoke-invoke gap is still recorded
