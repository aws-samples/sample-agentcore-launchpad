# Implementation plan

## Step 1 - Global source routing

- [x] Add constants for the runtime-group prefix and combined CWLI `SOURCE`.
- [x] Make `_start_query` omit log-group parameters for source-bearing global
      queries while preserving explicit `logGroupNames`.
- [x] Prefix every account-wide observability query with the combined source.

## Step 2 - Span-only query semantics

- [x] Add `ispresent(startTimeUnixNano)` to trace aggregate/detail, root,
      dashboard, distinct-agent/session, and top-tool queries.
- [x] Use `latest()` for trace/session string metadata.
- [x] Keep message-event and eval content-log queries scoped to their explicit
      runtime groups and able to read non-span records.

## Step 3 - Tests

- [x] Assert global start calls have no `logGroupName(s)` and carry `SOURCE`.
- [x] Assert explicit runtime-group calls retain `logGroupNames`.
- [x] Assert all span-derived query builders contain the span predicate.
- [x] Assert session/trace metadata uses `latest()` and never `max()` on strings.
- [x] Run focused backend tests.

## Step 4 - Documentation and spec

- [x] Update the architecture service mapping.
- [x] Add the durable unified-log-group query contract and index it.

## Step 5 - Verification

- [x] Run backend ruff and `tests/test_observability.py`.
- [x] Run a live combined-source session query in `us-west-2`.
- [x] Run `make verify`.

## Review gates

- Confirm every use of `run_insights_queries` is either a source-bearing global
  query or supplies explicit `log_groups`.
- Confirm no deployment/runtime configuration changed.
