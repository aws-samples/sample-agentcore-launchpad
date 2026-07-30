# Implement — one span reader for the session trace rail

## Step 0 — baseline

```bash
cd backend && uv run pytest -q | tail -2      # expect 925 passed
```

Capture the *current* broken behaviour on dev so the fix is provably a fix. Pick a
session that has spans in both group kinds:

```bash
# find one: which per-agent groups carry session ids, and a session id in one of them
aws logs start-query --region us-west-2 \
  --start-time $(( $(date +%s) - 3*86400 )) --end-time $(date +%s) \
  --query-string "SOURCE logGroups(namePrefix: ['/aws/bedrock-agentcore/runtimes/']) | fields \`attributes.session.id\` as sid | filter ispresent(sid) | stats count(*) by sid | limit 5"
```

Then, with the backend running, record `span_count` from
`/api/traces/<sid>` — that number is the "before".

## Step 1 — replace the fetch

`backend/app/services/traces.py`:

- Import `SPANS_SOURCE` and `run_insights_queries` from `app.services.observability`.
  Keep `SPANS_LOG_GROUP` (used for the fallback deep link).
- Replace `find_session_spans` with an Insights-based reader per `design.md`. Keep the
  function name and signature shape if practical so the diff stays legible, but it no
  longer needs a `logs_client`, `limit`, or `max_pages` in the old sense.
- `session_trace` collects contributing groups from `@log`, picks the biggest as
  primary, builds the deep link from it, and returns `log_groups` +
  `unavailable_reason`.
- Delete the now-unused `boto3` import if nothing else in the file needs it, and update
  the module docstring — it currently says "spans from CloudWatch Transaction Search
  (aws/spans)", which becomes wrong.
- Catch `AppError` and degrade to an empty rail.

```bash
cd backend && uv run ruff check . && uv run pytest tests/test_governance.py -q
```

`test_normalize_spans_categories_and_offsets` must pass **unmodified**.

## Step 2 — tests

New `backend/tests/test_traces.py`, stubbing `run_insights_queries`:

1. spans returned from `aws/spans` **and** a per-agent group both appear; `log_groups`
   lists both; primary is the bigger contributor
2. the query string contains `SPANS_SOURCE` — pin it so a regression to one group fails
3. `lookback_hours` is passed through as hours
4. `AppError` → `span_count: 0`, `unavailable_reason` set, no raise
5. no spans → empty `log_groups`, deep link falls back to `aws/spans`

```bash
cd backend && uv run pytest tests/test_traces.py -q
```

**Review gate:** narrow the query back to a single log group and confirm case 1 and
case 2 fail. If they pass either way they are not pinning the fix.

## Step 3 — verify + spec

```bash
make verify
```

Real AWS on dev — the decisive check:

```bash
cd backend && nohup uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 >/tmp/be.log 2>&1 &
curl -s "localhost:8000/api/traces/<sid>?lookback_hours=72" | python3 -c "
import json,sys; d=json.load(sys.stdin)
print('span_count:', d['span_count']); print('log_groups:', d['log_groups'])"
```

Expect `span_count` **higher than the Step 0 baseline** and `log_groups` naming more
than one group. Cross-check the total against an independent per-group Logs Insights
count for that session so the number is not just "bigger" but *right*.

Also confirm problem 2 is gone: `?lookback_hours=168` on the session that previously
returned 0 now returns spans.

Then update `.trellis/spec/launchpad/observability-log-groups.md`: session traces and the
Observability views share one reader; `governance_spans.py` stays pinned to `aws/spans`
because the gateway XRAY delivery writes only there.

> `pkill -f uvicorn` also matches the shell running it — kill by pid from `pgrep`.

## Step 4 — commit

Do **not** push or deploy unless asked; the last deployment was explicitly requested.

## Do not

- Widen `governance_spans.py` — Policy spans cannot be in a per-agent group.
- Tighten the session filter to `attributes.session.id` equality; that would show fewer
  spans than before.
- Change `normalize_spans` output, or the `span_count` / `spans` / `cloudwatch_url`
  field names.
- Let a Logs Insights failure surface as a 5xx on the Chat page.
