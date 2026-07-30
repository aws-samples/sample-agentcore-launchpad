# Session trace rail reads all span log groups

## Goal

Make `/api/traces/{session_id}` — the Chat page's trace rail — read spans the same way
the Observability views do, so it stops going blank on anything but a just-finished
session, and stops being a second, divergent span reader.

## The defect

`app/services/traces.py` has its own span reader, independent of
`app/services/observability.py`:

```python
SPANS_LOG_GROUP = "aws/spans"          # traces.py:16
kwargs = {"logGroupName": SPANS_LOG_GROUP, "filterPattern": f'"{session_id}"', ...}
response = logs_client.filter_log_events(**kwargs)   # traces.py:48
```

Two independent problems, both verified 2026-07-30:

1. ~~It reads only `aws/spans`, and a session's spans are split across groups.~~
   **CORRECTED during implementation — this claim was wrong.** The per-agent counts I
   measured (7 on prod, 21 on dev for one session, 862 for `lab_fund_assistant`) were
   **application logs and OTEL log records, not spans**: those groups mix all three, and
   the log records carry a `session.id` too. A direct check —
   `filter ispresent(name) and ispresent(traceId) and ispresent(startTimeUnixNano)` over
   all per-agent groups, 3 days — returns **0**. No spans are being dropped today.

   The multi-group source is still the right target, as **forward compatibility**: AWS
   documents the per-agent span destination as the default for newer agents, and
   `observability.py` already relies on it. But it must be labelled as such, not as an
   observed loss. It also creates a new requirement: because those groups contain log
   records that match a session-id substring, the query must accept only real spans, or
   16 nameless rows land in the rail (verified).
2. **The actual defect — `FilterLogEvents` scans forward from `startTime` with bounded
   pagination**
   (`max_pages=20`). With a long `lookback_hours` the target spans are never reached:
   `/api/traces/{sid}?lookback_hours=72` returned **0 spans** for a session whose 32
   spans were sitting in `aws/spans` the whole time — the same spans that Logs Insights
   finds, and that `FilterLogEvents` itself finds when given a ±3 minute window.

`observability.py` already reads spans correctly, across both group prefixes:

```python
SPANS_SOURCE = ("SOURCE logGroups(namePrefix: "
                "['aws/spans', '/aws/bedrock-agentcore/runtimes/'])")
```

All eight Observability queries use it — and none use `filter_log_events`, which is why
the Observability views return data where this rail returns nothing. The fix is to stop
having two span readers.

### Ruled out during investigation

Recorded so nobody re-derives them: it is **not** a log-group-class problem (`aws/spans`
is `STANDARD` in both regions, so `FilterLogEvents` is supported), and **not** an
absent-data problem (the spans exist; two different queries find them).

## Severity, stated honestly

In the normal flow — chat, then open the rail immediately — the default 3 h lookback
with fresh spans will usually work, which is why this has gone unnoticed. The symptom
appears when the rail is opened on an older session: it goes **blank**, not partial.
Measured: 0 spans returned where 32 existed.

## Requirements

### R1 — One reader, not two

- `traces.py` reads spans through `observability.run_insights_queries` with
  `observability.SPANS_SOURCE`, replacing `filter_log_events`.
- The session filter must stay as permissive as the old term filter: the old code
  matched **any** span whose raw message contained the session id, not only
  `attributes.session.id`. Use a `@message`-substring clause so no span the rail used to
  show is lost.
- `normalize_spans` / `categorize` / `_span_times` are unchanged — they already work on
  parsed span JSON, and one existing test covers them.

### R2 — Preserve the response contract

`frontend/src/pages/Chat.tsx` consumes `span_count`, `spans[]`
(`name`/`category`/`start_ms`/`duration_ms`), and `cloudwatch_url`. All must keep their
names and meaning. `log_group` is returned today but unused by the frontend.

### R3 — Report which groups actually contributed

The current `cloudwatch_url` hardcodes a deep link to `aws/spans`, which is misleading
once spans come from several groups. Return the groups that actually contributed spans
and point the deep link at a group that really holds some.

### R4 — Degrade, don't 500

A Logs Insights failure should leave the rail empty with a stated reason rather than
turning the Chat page's trace panel into an error, matching how the Governance decision
rows handle the same dependency.

### R5 — Tests

- Spans from **both** `aws/spans` and a per-agent group appear in one rail response
  (forward compatibility — hypothetical fixture, not observed data).
- Log records that merely mention the session id are **not** turned into rail rows.
- The query string contains `SPANS_SOURCE` — a regression to a single log group fails.
- `lookback_hours` reaches the query as hours.
- Query failure → empty rail + reason, no exception.
- The existing `test_normalize_spans_categories_and_offsets` keeps passing untouched.

## Out of scope

- `governance_spans.py` pinning to `aws/spans` — that is **correct**: gateway vended
  Policy spans are delivered to `aws/spans` by the XRAY delivery, never to a per-agent
  group.
- The Chat rail's UI.
- Any AWS mutation.

## Acceptance criteria

- [x] Against real AWS on dev: `?lookback_hours=72` on a session that returned **0**
      before now returns **32**, matching an independent Logs Insights count of that
      session's spans in `aws/spans` exactly (the 21 further matching records in the
      agent's group are log records, correctly excluded).
- [x] `?lookback_hours=168` likewise returns 32 instead of 0.
- [x] Log records that merely mention the session id do not become rail rows.
- [x] Response keeps `span_count`, `spans`, `cloudwatch_url`; contributing groups are
      reported as `log_groups`, and `log_group` now names the biggest contributor.
- [x] `make verify` passes (934 backend tests).
- [x] Only one span-reading implementation remains: `filter_log_events` is gone from the
      codebase's span path.


## Deviations and corrections during implementation

1. **The primary premise was wrong and is corrected above.** I counted log records as
   spans. Per-agent runtime groups hold no spans in this account; the real defect is
   `FilterLogEvents`' oldest-first bounded pagination returning 0. The fix stands, its
   justification changed. Corrected in `prd.md`, both module docstrings, the test
   docstrings, and the spec.
2. **A new requirement emerged from widening the source**: those groups' log records match
   a session-id substring, so without `filter ispresent(name) or ispresent(spanName)` plus
   a client-side `_is_span` guard, 16 nameless rows entered the rail. Verified against real
   data (48 rows → 32 real spans).
3. **The spec already required this.** `observability-log-groups.md` documented the
   `SOURCE logGroups(...)` contract and "must read both layouts" before this task —
   `traces.py` was in violation, not merely behind. Reframed accordingly.
4. **A test was strengthened after the review gate exposed it as decorative.** Narrowing
   the query to one log group initially failed only the query-string assertion, because the
   stub returned rows regardless. The stub now honours the query's `namePrefix` list, and
   the gate then failed 4 tests including the primary one.
