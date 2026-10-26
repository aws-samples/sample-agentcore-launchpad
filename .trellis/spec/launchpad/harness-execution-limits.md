# Harness execution limits and failed evaluation replay

## Defaults and scope

Ordinary AgentSpec, assistant ProposalContent and creation UI default to 180 seconds.
Explicit values and system-preset-specific budgets remain unchanged. Deployment maps
`timeout_seconds` to the managed Harness `timeoutSeconds`, independently of SDK socket
read timeouts. `max_tokens` is a separate per-model-call output budget; reasoning may
consume it before any visible answer. Do not infer an iteration failure from a generic
limit error; inspect the concrete service stop reason or provider span.

## Stream contract

`services/agentcore/harness.py::iter_harness_stream` is the shared raw-event validator
for sync invocation, architect discussion and Chat/public SSE. It drains and closes
the stream, preserves normal tool_use/tool_result cycles, refuses unsuccessful stop
reasons after partial text, and gives an outer timeout precedence over inner cancelled.
Text-only legacy streams remain compatible; empty streams and unfinished tool cycles
are incomplete. AppError codes: harness.execution_timeout (504),
harness.execution_cancelled / execution_limit / incomplete_response (502), with
`detail.stop_reason`. SSE errors retain code/message/detail and emit no successful done.
Cancelled/timed-out evaluator evidence is INTERRUPTED, never a complete answer.

## Batch and display

A failed Harness replay does not call StartBatchEvaluation. EvalRun.session_ids is
currently appended after each scenario completes, so a run with one saved session
may have failed in its second scenario. The run error now retains the error code,
stop reason, scenario ID and attempted Harness session ID, so inspect that failed
session rather than assuming the last completed session is the failed one. COMPLETED_WITH_ERRORS persists as completed
plus error; evaluationRunPresentation exposes a warning display state while retaining
scores/errors and excluding it from the suggested clean experiment baseline.

## Validation

`test_harness_execution.py` covers both sync/SSE entrances, late failures, complete
tool cycles, transport closure, error envelopes and no-batch-on-replay-failure.
`test_agent_timeout_defaults.py` covers omission vs explicit values. Production
c5fc84c7 diagnosis proved a 30-second native budget produced timeout_exceeded outside
Strands, cancelled inside traces, and no final text. The repair's first rerun also
exposed a separate 4096-token model limit in its second scenario; only that Agent's
output budget was explicitly raised to 8192, while its timeout stayed 180 and its
iteration limit stayed 8. StopRuntimeSession for a Harness diagnostic must use the
Harness ARN, not the managed backing Runtime ARN.
