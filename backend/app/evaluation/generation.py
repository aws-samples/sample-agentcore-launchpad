"""AgentCore dataset generation as a 数据处理 source (`PipelineSource.type = "agentcore"`).

Pure helpers for the pipeline run in `pipeline_routers._run_agentcore`: which
observed sessions to send, the `traceGeneration` request, and which generated
examples to keep. Live facts behind every rule here (dev, 2026-11-26) are in the
spec `.trellis/spec/launchpad/pipeline-agentcore-source.md`:

* sessions are always sent as `selection.identifiers.sessionIds` — a filter-based
  generation writes every matching session (another principal's private ones too)
  into the shared AWS Draft, and deleting the generation does not remove them;
* the runtime's own log group is the source (`aws/spans` alone fails
  `SPANS_INCOMPLETE`), and sessions younger than ~10 minutes are not ready;
* DYNAMIC writes a persona (`actor_profile`) only for multi-turn sessions;
* a Managed Harness session that paused on an inline-function interrupt yields a
  junk transcript (the interrupt payload as a reply, the system prompt as input).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.evaluation import pipelines
from app.evaluation.scenarios import scenario_kind

MAX_SESSIONS = 25  # StartDatasetGeneration identifiers.sessionIds cap
FRESH_MINUTES = 10
WINDOW_MARGIN = timedelta(minutes=5)
INTERRUPT_PREFIX = "[{'id': 'v1:before_tool_call"
POLL_SECONDS = 5.0
POLL_MAX = 180  # 15 minutes; a generation of ≤25 sessions takes 10–50 s

# StartDatasetGeneration's SessionId shape (runtime session ids may be longer)
SESSION_ID_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,99}")

RESULT_KEYS = (
    "sessionsMatched", "sessionsSelected", "sessionsUnscored", "sessionsFailed",
    "scenariosGenerated", "scenariosDeduplicated", "examplesWritten",
    "examplesWithGroundTruth",
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _parse(ts: Any) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    try:
        value = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def present_session_ids(items: list[dict[str, Any]]) -> set[str]:
    """Sessions a dataset already holds: a generated example names it in
    `metadata.source.session_id`, the local extractor in `metadata.session_id`."""
    out: set[str] = set()
    for item in items:
        meta = _dict(item.get("metadata"))
        source = _dict(meta.get("source"))
        for sid in (source.get("session_id"), meta.get("session_id")):
            if sid:
                out.add(str(sid))
    return out


def select_sessions(
    rows: list[dict[str, Any]],
    *,
    service: str,
    status: str,
    dynamic: bool,
    present: set[str],
    limit: int,
    now: datetime,
    oldest: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], int]:
    """(picked rows newest first, skipped, matched) among the window's session rows
    of `service`. A row active in the last `FRESH_MINUTES` is `too_fresh` (its
    telemetry is still landing), one already in the dataset `already_in_dataset`;
    DYNAMIC keeps only multi-turn sessions (a single-turn one yields no persona).
    A session id AWS cannot take is `unsupported_session_id`, one whose last
    activity is older than `oldest` (the log group's retention) `expired`.
    `matched` counts the rows that passed the agent/status/turn filters."""
    candidates = []
    for row in rows:
        if row.get("service") != service or not row.get("session_id"):
            continue
        errors = int(row.get("errors") or 0)
        if status == "error" and errors == 0:
            continue
        if status == "ok" and errors > 0:
            continue
        if dynamic and int(row.get("traces") or 0) < 2:
            continue
        candidates.append(row)
    candidates.sort(key=lambda r: str(r.get("last") or r.get("first") or ""), reverse=True)
    fresh_after = now - timedelta(minutes=FRESH_MINUTES)
    picked: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for row in candidates:
        sid = str(row["session_id"])
        last = _parse(row.get("last") or row.get("first"))
        # no timestamp at all is treated like a fresh session: we cannot tell
        # whether its telemetry has landed (the console preview holds it back too)
        if not SESSION_ID_RE.fullmatch(sid):
            skipped.append({"session_id": sid, "reason": "unsupported_session_id"})
        elif last is None or last > fresh_after:
            skipped.append({"session_id": sid, "reason": "too_fresh"})
        elif oldest is not None and last < oldest:
            skipped.append({"session_id": sid, "reason": "expired"})
        elif sid in present:
            skipped.append({"session_id": sid, "reason": "already_in_dataset"})
        elif len(picked) < limit:
            picked.append(row)
    return picked, skipped, len(candidates)


def retention_floor(now: datetime, retention_days: int | None) -> datetime | None:
    """The oldest instant a generation window may start at (AWS rejects older)."""
    if not retention_days:
        return None
    return now - timedelta(days=retention_days) + timedelta(minutes=1)


def build_source(
    cfg: Any,
    *,
    service_name: str,
    log_group: str,
    sessions: list[dict[str, Any]],
    keep_replies: bool,
    now: datetime,
    retention_days: int | None,
) -> dict[str, Any]:
    """The `traceGeneration` source: the runtime log group + service, an explicit
    window around the picked sessions (never the 7-day default; never older than
    the group's retention, which AWS rejects), the sessions by id, one scenario per
    session, ground truth for FIXED, redaction passed through."""
    starts = [t for r in sessions if (t := _parse(r.get("first") or r.get("last")))]
    ends = [t for r in sessions if (t := _parse(r.get("last") or r.get("first")))]
    start = (min(starts) if starts else now - timedelta(days=7)) - WINDOW_MARGIN
    end = min(now, (max(ends) if ends else now) + WINDOW_MARGIN)
    floor = retention_floor(now, retention_days)
    if floor is not None:
        start = max(start, floor)
    source: dict[str, Any] = {
        "sourceConfig": {"cloudWatchLogs": {
            "logGroupNames": [log_group],
            "serviceNames": [service_name],
            "startTime": start,
            "endTime": end,
        }},
        "selection": {"identifiers": {"sessionIds": [str(r["session_id"]) for r in sessions]}},
        "sampling": {"maxScenarios": max(1, len(sessions))},
    }
    if cfg.scenario_type == "FIXED":
        fields = [f for f, on in (("expected_response", keep_replies),
                                  ("expected_trajectory", cfg.expected_trajectory)) if on]
        if fields:
            source["groundTruth"] = {"fields": fields}
    redaction = getattr(cfg, "redaction", None)
    if redaction and (redaction.identifiers or redaction.custom):
        source["redactionConfiguration"] = {
            "identifiers": list(redaction.identifiers),
            "customDataIdentifiers": [{"name": c.name, "regex": c.regex}
                                      for c in redaction.custom],
        }
    return source


def is_interrupt_transcript(example: dict[str, Any]) -> bool:
    """A Harness inline-function (approval) round trip: the interrupt payload was
    recorded as the agent's reply, so the scenario would replay nonsense."""
    for turn in example.get("turns") or []:
        reply = turn.get("expected_response") if isinstance(turn, dict) else None
        if isinstance(reply, str) and reply.startswith(INTERRUPT_PREFIX):
            return True
    return False


def example_session(example: dict[str, Any]) -> str | None:
    source = _dict(_dict(example.get("metadata")).get("source"))
    sid = source.get("session_id")
    return str(sid) if sid else None


def keep_examples(
    examples: list[dict[str, Any]], *, requested: set[str], validate: Any,
) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, str]]]]:
    """(kept items without `exampleId`, dropped [(exampleId, skipped entry)]).
    Dropped: a session we did not ask for, a Harness interrupt transcript, or an
    example the dataset validator refuses (`validate([item])` raises)."""
    kept: list[dict[str, Any]] = []
    dropped: list[tuple[str, dict[str, str]]] = []
    for example in examples:
        example_id = str(example.get("exampleId") or "")
        item = {k: v for k, v in example.items() if k != "exampleId"}
        sid = example_session(example) or ""
        reason = None
        if sid not in requested:
            reason = "unexpected_session"
        elif is_interrupt_transcript(item):
            reason = "interrupt_transcript"
        elif scenario_kind(item) == "legacy":
            reason = "invalid_example"
        else:
            try:
                validate([item])
            except Exception:  # noqa: BLE001 — any refusal drops the one example
                reason = "invalid_example"
        if reason:
            dropped.append((example_id, {"session_id": sid, "reason": reason}))
        else:
            kept.append(item)
    return kept, dropped


def merge(
    existing: list[dict[str, Any]], kept: list[dict[str, Any]], *, dedupe: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """(merged items, items merged, skipped for the ones left out) — the local
    dataset merge (`pipelines.merge_items`: scenario id, optional first-input
    dedupe, the 200-item cap) applied item by item so the caller knows which
    generated examples did not make it (and deletes them from the Draft)."""
    merged = list(existing)
    taken: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in kept:
        merged, added, reasons = pipelines.merge_items(merged, [item], dedupe=dedupe)
        if added:
            taken.append(item)
        else:
            skipped.append({"session_id": example_session(item) or "",
                            "reason": reasons[0] if reasons else "duplicate"})
    return merged, taken, skipped


def summary(response: dict[str, Any]) -> dict[str, Any]:
    """The `last_run.generation` fields read off a GetDatasetGeneration response."""
    results = response.get("generationResults") or {}
    redaction = response.get("redactionSummary")
    return {
        "status": response.get("status"),
        "results": {k: int(results.get(k) or 0) for k in RESULT_KEYS},
        "error_details": [
            {
                "code": d.get("errorCode"),
                "message": d.get("errorMessage"),
                "count": d.get("count"),
                "session_ids": list(d.get("sessionIds") or []),
            }
            for d in response.get("errorDetails") or []
        ],
        "redaction": (
            {"scanned": int(redaction.get("scenariosScanned") or 0),
             "findings": dict(redaction.get("findingCounts") or {})}
            if redaction else None
        ),
        "failure_reason": response.get("failureReason"),
    }


def failed_sessions(response: dict[str, Any]) -> list[dict[str, str]]:
    """One skipped entry per session AWS reported failing (`errorDetails` lists up
    to 10 ids per code), carrying the AWS error code."""
    out = []
    for detail in response.get("errorDetails") or []:
        for sid in detail.get("sessionIds") or []:
            out.append({"session_id": str(sid), "reason": "generation_failed",
                        "code": str(detail.get("errorCode") or "")})
    return out


def generation_name(pipeline_id: str) -> str:
    """`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`; the generationId starts with it."""
    safe = "".join(ch if ch.isalnum() else "_" for ch in pipeline_id)
    return f"lp_{safe}"[:48]


def client_token(pipeline_id: str, run_at: str) -> str:
    """Deterministic per run (33–256 chars): a retried start returns the same
    generation instead of starting a second one."""
    raw = f"launchpad-{pipeline_id}-{run_at}"
    # ClientToken pattern: alphanumerics joined by hyphens, alphanumeric at both ends
    token = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in raw).strip("-")
    return token.ljust(33, "0")[:256]
