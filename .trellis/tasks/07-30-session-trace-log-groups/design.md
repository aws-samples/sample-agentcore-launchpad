# Design — one span reader for the session trace rail

## Boundaries

| File | Change |
|---|---|
| `backend/app/services/traces.py` | `find_session_spans` → Logs Insights via `SPANS_SOURCE`; `session_trace` returns contributing groups |
| `backend/tests/test_traces.py` (new) | R5 cases |
| `.trellis/spec/launchpad/observability-log-groups.md` | record the one-reader rule |

`traces.py` keeps ownership of span **shaping** (`normalize_spans`, `categorize`,
`_span_times`) — that logic is fine and tested. Only the **fetch** moves.

Importing `observability` from `traces.py` is the right direction: `observability.py`
owns the Logs Insights transport, and `governance_spans.py` already depends on it the
same way. No new client construction site — `run_insights_queries` builds the `logs`
client itself when not injected.

## The query

```python
def _session_query(session_id: str) -> str:
    return (
        f"{SPANS_SOURCE}"
        " | fields @message, @log"
        f' | filter @message like "{session_id}"'
        " | sort @timestamp desc"
        f" | limit {SPAN_LIMIT}"
    )
```

Three deliberate choices:

- **`@message like "<sid>"`, not `attributes.session.id = "<sid>"`.** The old
  `filterPattern: '"<sid>"'` matched any span whose raw text contained the id, and the
  rail's value is showing the *whole* call tree — including spans that carry the id
  somewhere other than that attribute. A stricter equality filter would quietly show
  fewer spans than before, turning a bug fix into a regression.
- **Select `@message`** and `json.loads` it, exactly as `governance_spans.py` does:
  Logs Insights flattens nested arrays into `attributes.x.0`, which cannot reconstruct a
  span.
- **Select `@log`** so the response can name the groups that really contributed (R3).

Session ids are 64-char hex from the platform, so interpolating one into the query is
safe; the route already constrains the path parameter.

## Response shape

```python
{
  "session_id": ...,
  "span_count": len(spans),
  "spans": spans,                       # unchanged shape
  "log_groups": ["aws/spans", "/aws/bedrock-agentcore/runtimes/…"],   # new
  "log_group": <primary>,               # kept, now = the biggest contributor
  "cloudwatch_url": <deep link to primary>,
  "unavailable_reason": None | str,     # new
}
```

`log_group` is kept rather than dropped: it costs nothing, and something outside this
repo may read it. It now means "the group that contributed the most spans" instead of
"always aws/spans", which is strictly more truthful. The deep link is built from that
group with the same `$252F` escaping the current code uses, so a link to a per-agent
group works too.

When no spans are found, `log_groups` is empty and the deep link falls back to
`aws/spans` — the panel needs a link to *somewhere*, and that is the group an operator
would check first.

## Failure handling

`run_insights_queries` raises `AppError` on start/poll failure, timeout, and cancel.
Catch it, return an empty rail with `unavailable_reason = exc.code`. Rationale in the
PRD (R4): the trace rail is a side panel on the Chat page; a Logs Insights hiccup must
not turn chatting into an error state. This mirrors `governance_evidence._with_spans`.

`run_insights_queries` already returns `[]` (not an error) when a log group is missing,
so a fresh account with no `aws/spans` yet degrades on its own.

## What does NOT change

- `normalize_spans`, `categorize`, `_span_times`, `CATEGORY_RULES` — untouched, and the
  existing `test_normalize_spans_categories_and_offsets` must keep passing unmodified.
- `governance_spans.py` stays pinned to `aws/spans`: the gateway's XRAY vended delivery
  writes Policy spans only there, so widening it would scan per-agent groups for spans
  that can never be in them.

## Tradeoffs

- **Logs Insights costs per scan** where `FilterLogEvents` was cheaper per call. Bounded
  by `lookback_hours` (default 3) and a row limit, and it is what every other span read
  in this codebase already does. Correctness over a micro-optimisation on a panel the
  user opens by hand.
- **`@message like` is a substring scan**, so it cannot use a field index. Same cost
  class as the term filter it replaces, and it preserves behavior.
- **`limit` caps a very long session's rail.** The old code capped at 100 too; keep a
  comparable cap and let `span_count` reflect what was returned.

## Rollback

Pure revert — read-only change, no AWS mutation, no contract removal.
