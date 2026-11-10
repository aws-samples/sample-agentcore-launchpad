"""E05 Observability — span-tree properties and Logs Insights fault shapes (round 16,
r16-g; `docs/cn/plans/BUG-DISCOVERY-PLAN.md` §E05: "property test of the trace parsing:
random span trees → tree rendering loses nothing, no cycles").

Contract sources:

* `docs/cn/acceptance/E05-observability.md` AC-E05-01 (a trace row carries `status ok` /
  `error`, `tokens.{input,output,total}`), AC-E05-03 (the trace **waterfall**: every span of
  the trace nested under its parent), NC-E05-09 (Logs Insights query failure → lists /
  detail `502 observability.query_failed`; the Chat rail `GET /api/traces/{sid}` stays
  `200` with `spans []` and `unavailable_reason`), §6 ("empty `aws/spans`" is ENV, not an
  error).
* `docs/cn/interfaces/observability.md` "Chat trace rail" ("A query failure is **not** an
  HTTP error for this side panel: `spans = []` and `unavailable_reason = <AppError code>`").
* `core/errors.py` ("every 4xx / 5xx body is the `{code, message, detail}` envelope") —
  a bare 500 on a malformed upstream row is a contract breach, whatever the row.

The tree oracle is the raw span list itself: every span id drawn must appear in the
rendered tree exactly once, under its parent when that parent exists, at depth
parent + 1, with siblings ordered by start offset (ties: arrival order). Fault shapes are
the real botocore `ClientError` codes and `GetQueryResults` statuses, never a
`RuntimeError("boom")`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatSession
from app.services import observability as obs
from tests.test_observability import FakeCW, FakeLogs

HEX = "0123456789abcdef"
BASE_NS = 1_760_000_000_000_000_000  # 2025-10-09T…Z as nanoseconds


@pytest.fixture(autouse=True)
def fresh_cache():
    obs.reset_cache()
    yield
    obs.reset_cache()


# ─── span-tree strategy ──────────────────────────────────────────────────────


def _hex(n: int) -> st.SearchStrategy[str]:
    return st.text(HEX, min_size=n, max_size=n)


@st.composite
def span_trees(draw, max_spans: int = 40) -> dict[str, Any]:
    """An acyclic forest with orphans, shuffled arrival, missing / string timestamps."""
    ids = draw(st.lists(_hex(16), min_size=0, max_size=max_spans, unique=True))
    n = len(ids)
    spans: list[dict[str, Any]] = []
    for i, span_id in enumerate(ids):
        kind = draw(st.sampled_from(["root", "child", "orphan"]) if i else st.just("root"))
        if kind == "child":
            parent: str | None = ids[draw(st.integers(0, i - 1))]
        elif kind == "orphan":
            parent = "f" * 16  # a span this query never returned
            assume(parent not in ids)
        else:
            parent = None
        start = draw(st.integers(0, 10_000)) * 1_000_000  # ms-aligned ns offsets
        duration = draw(st.integers(0, 5_000)) * 1_000_000
        shape = draw(st.sampled_from(["int", "str", "missing"]))
        span: dict[str, Any] = {
            "traceId": "a" * 32, "spanId": span_id, "name": draw(st.sampled_from(
                ["POST /invocations", "invoke_agent Strands Agents", "chat model", "tool x"])),
            "kind": draw(st.sampled_from(["SERVER", "INTERNAL", "CLIENT"])),
            "attributes": {},
        }
        if parent is not None:
            span["parentSpanId"] = parent
        if shape == "int":
            span["startTimeUnixNano"] = BASE_NS + start
            span["endTimeUnixNano"] = BASE_NS + start + duration
        elif shape == "str":
            span["startTimeUnixNano"] = str(BASE_NS + start)
            span["endTimeUnixNano"] = str(BASE_NS + start + duration)
        if draw(st.booleans()):
            span["status"] = {"code": draw(st.sampled_from(["OK", "ERROR", "UNSET"]))}
        spans.append(span)
    order = draw(st.permutations(range(n))) if n else []
    return {"spans": [spans[i] for i in order], "ids": set(ids)}


def _walk(nodes: list[dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


@settings(max_examples=300, deadline=None)
@given(tree=span_trees())
def test_the_waterfall_keeps_every_span_once_under_its_parent(tree):
    spans = tree["spans"]
    built = obs.build_span_tree(spans)
    flat, roots = built["spans"], built["tree"]
    # (1) nothing lost, nothing duplicated — flat and tree alike
    assert len(flat) == len(spans)
    rendered = list(_walk(roots))
    assert sorted(n["span_id"] for n in rendered) == sorted(tree["ids"])
    by_id = {n["span_id"]: n for n in rendered}
    # (2) nesting: a known parent nests its child at depth + 1; an unknown / absent parent
    #     makes a root
    arrival = {s["spanId"]: i for i, s in enumerate(spans)}
    for span in spans:
        node = by_id[span["spanId"]]
        parent_id = span.get("parentSpanId")
        if parent_id in by_id:
            parent = by_id[parent_id]
            assert node in parent["children"]
            assert node["depth"] == parent["depth"] + 1
        else:
            assert node in roots and node["depth"] == 0
    # (3) children (and roots) ordered by start offset; ties keep arrival order (stable)
    for siblings in [roots, *(n["children"] for n in rendered)]:
        offsets = [c["start_offset_ms"] for c in siblings]
        assert offsets == sorted(offsets)
        for a, b in zip(siblings, siblings[1:], strict=False):
            if a["start_offset_ms"] == b["start_offset_ms"]:
                assert arrival[a["span_id"]] < arrival[b["span_id"]]
    # (4) offsets are relative to the earliest span and never negative; a timed span's
    #     duration is the drawn one
    timed = [s for s in spans if isinstance(s.get("startTimeUnixNano"), int | str)]
    for row in flat:
        assert row["start_offset_ms"] >= 0 and row["duration_ms"] >= 0
    if timed:
        starts = [int(s["startTimeUnixNano"]) for s in timed]
        ends = [int(s["endTimeUnixNano"]) for s in timed]
        assert built["duration_ms"] == round(max(1.0, max(ends) - min(starts)) / 1e6, 1)
        earliest = min(timed, key=lambda s: int(s["startTimeUnixNano"]))
        assert by_id[earliest["spanId"]]["start_offset_ms"] == 0.0


def test_a_five_hundred_deep_chain_renders_without_recursion_failure():
    """SPANS_PER_TRACE caps one trace query, so the deepest chain the API can hand the
    builder is that long; it must annotate to the leaf."""
    ids = [f"{i:016x}" for i in range(obs.SPANS_PER_TRACE)]
    spans = [{"spanId": s, **({"parentSpanId": ids[i - 1]} if i else {}),
              "name": "n", "startTimeUnixNano": BASE_NS + i, "endTimeUnixNano": BASE_NS + i + 1}
             for i, s in enumerate(ids)]
    built = obs.build_span_tree(spans)
    assert len(built["tree"]) == 1
    deepest = max(n["depth"] for n in _walk(built["tree"]))
    assert deepest == len(ids) - 1


@pytest.mark.parametrize("cycle", [2, 3, 7])
def test_spans_in_a_parent_cycle_still_render(cycle):
    """N16-obs52 (found by the property above's cycle probe, fixed in r16-g): spans whose
    parent pointers form a cycle used to vanish from the waterfall `tree` while `spans` /
    `span_count` still listed them. Now the first-arrived member becomes a root."""
    ids = [f"{i:016x}" for i in range(cycle)]
    spans = [{"spanId": s, "parentSpanId": ids[(i + 1) % cycle], "name": "n",
              "startTimeUnixNano": BASE_NS + i, "endTimeUnixNano": BASE_NS + i + 1}
             for i, s in enumerate(ids)]
    built = obs.build_span_tree(spans)
    assert len(built["spans"]) == cycle
    rendered = list(_walk(built["tree"]))
    assert sorted(n["span_id"] for n in rendered) == sorted(ids)
    assert len(rendered) == cycle  # each exactly once — the cycle is broken, not unrolled
    assert max(n["depth"] for n in rendered) == cycle - 1


def test_a_self_parented_span_is_a_root_not_a_cycle():
    span = {"spanId": "a" * 16, "parentSpanId": "a" * 16, "name": "n",
            "startTimeUnixNano": BASE_NS, "endTimeUnixNano": BASE_NS + 1}
    built = obs.build_span_tree([span])
    assert [n["span_id"] for n in built["tree"]] == ["a" * 16]
    assert built["tree"][0]["children"] == []


# ─── the trace detail over the API with random trees ─────────────────────────

TRACE = "b" * 32


def _logs_for(rows_by_marker: dict[str, list[dict[str, Any]]]) -> FakeLogs:
    return FakeLogs(rows_by_marker)


def _patch_clients(monkeypatch, logs, cw=None):
    monkeypatch.setattr(obs, "logs_client", lambda _ws=None: logs)
    monkeypatch.setattr(obs, "cw_client", lambda _ws=None: cw or FakeCW())


@settings(
    max_examples=40, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tree=span_trees(max_spans=12))
def test_trace_detail_reports_the_tree_and_the_error_status(client, monkeypatch, tree):
    obs.reset_cache()
    spans = [{**s, "traceId": TRACE} for s in tree["spans"]]
    _patch_clients(monkeypatch, _logs_for({
        f'filter traceId = "{TRACE}"': [{"@message": json.dumps(s)} for s in spans],
    }))
    res = client.get(f"/api/observability/traces/{TRACE}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["meta"]["span_count"] == len(spans)
    assert sorted(n["span_id"] for n in _walk(body["tree"])) == sorted(tree["ids"])
    any_error = any((s.get("status") or {}).get("code") == "ERROR" for s in spans)
    assert body["meta"]["status"] == ("error" if any_error else "ok")
    assert set(body["meta"]["tokens"]) == {"input", "output", "cache_read", "cache_write", "total"}
    assert body["meta"]["llm_count"] == 0  # no token-bearing span was drawn
    assert body["meta"]["malformed"] == ZERO_MALFORMED  # a clean export counts nothing


# ─── Logs Insights fault shapes → 502 observability.query_failed, never 500 ─────────


def _client_error(code: str, operation: str = "StartQuery") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"{code} injected"}}, operation)


class FaultyLogs(FakeLogs):
    """The real failure modes of `StartQuery` / `GetQueryResults`."""

    def __init__(self, *, start_error: str | None = None, poll_error: str | None = None,
                 poll_status: str | None = None, rows: list[dict[str, Any]] | None = None):
        super().__init__({"": rows or []})
        self.start_error = start_error
        self.poll_error = poll_error
        self.poll_status = poll_status
        self.stopped: list[str] = []

    def start_query(self, **kwargs):
        if self.start_error:
            raise _client_error(self.start_error)
        return super().start_query(**kwargs)

    def get_query_results(self, queryId):
        if self.poll_error:
            raise _client_error(self.poll_error, "GetQueryResults")
        if self.poll_status:
            return {"status": self.poll_status, "results": []}
        return super().get_query_results(queryId)

    def stop_query(self, queryId):
        self.stopped.append(queryId)


LIST_ROUTES = [
    "/api/observability/traces",
    f"/api/observability/traces/{TRACE}",
    "/api/observability/sessions",
    f"/api/observability/sessions/{'s' * 64}",
    "/api/observability/dashboard",
]
START_FAULTS = ["ThrottlingException", "LimitExceededException", "ServiceUnavailableException",
                "AccessDeniedException", "InvalidParameterException", "MalformedQueryException"]
POLL_STATUSES = ["Failed", "Cancelled", "Timeout"]


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(obs.time, "sleep", lambda _s: None)


@pytest.mark.parametrize("route", LIST_ROUTES)
@pytest.mark.parametrize("code", START_FAULTS)
def test_start_query_faults_are_a_502_envelope(client, monkeypatch, no_sleep, route, code):
    logs = FaultyLogs(start_error=code)
    _patch_clients(monkeypatch, logs)
    res = client.get(route)
    assert res.status_code == 502, (route, code, res.text)
    body = res.json()
    assert body["code"] == "observability.query_failed"
    assert code in body["message"]
    if code in ("ThrottlingException", "LimitExceededException"):
        assert logs.start_calls == 0  # the retry happens inside the stub's raise path
    assert set(body) == {"code", "message", "detail"}


@pytest.mark.parametrize("route", LIST_ROUTES)
@pytest.mark.parametrize("status", POLL_STATUSES)
def test_terminal_query_statuses_are_a_502_envelope(client, monkeypatch, no_sleep, route, status):
    _patch_clients(monkeypatch, FaultyLogs(poll_status=status))
    res = client.get(route)
    assert res.status_code == 502, (route, status, res.text)
    assert res.json()["code"] == "observability.query_failed"
    assert status in res.json()["message"]


@pytest.mark.parametrize("route", LIST_ROUTES)
def test_polling_faults_are_a_502_envelope(client, monkeypatch, no_sleep, route):
    _patch_clients(monkeypatch, FaultyLogs(poll_error="ThrottlingException"))
    res = client.get(route)
    assert res.status_code == 502, (route, res.text)
    assert res.json()["code"] == "observability.query_failed"


@pytest.mark.parametrize("route", LIST_ROUTES)
def test_a_query_that_never_completes_is_stopped_and_a_502(client, monkeypatch, no_sleep, route):
    """Past the deadline the service stops the query (no runaway billing) and answers 502."""
    logs = FaultyLogs(poll_status="Running")
    _patch_clients(monkeypatch, logs)
    monkeypatch.setattr(obs, "QUERY_DEADLINE_SECONDS", -1)
    res = client.get(route)
    assert res.status_code == 502, (route, res.text)
    assert res.json()["code"] == "observability.query_failed"
    assert "timed out" in res.json()["message"]
    assert logs.stopped, "the lost query was not stopped"


@pytest.mark.parametrize("route", [r for r in LIST_ROUTES if "{" not in r and
                                   r.endswith(("/traces", "/sessions", "/dashboard"))])
def test_a_missing_log_group_is_an_empty_view_not_an_error(client, monkeypatch, no_sleep, route):
    """E05 §6: an account before any agent traffic has no `aws/spans` yet — ENV, not 5xx.
    The lists answer empty, the dashboard renders zeros."""
    _patch_clients(monkeypatch, FaultyLogs(start_error="ResourceNotFoundException"))
    res = client.get(route)
    assert res.status_code == 200, res.text
    if not route.endswith("/dashboard"):
        assert res.json()["count"] == 0


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs56: an unknown trace / session id answers 200 with an empty shell "
    "(`span_count 0`, `status ok`, `agent unknown`) instead of NC-E05-03's 404 "
    "`observability.trace_not_found` / `session_not_found`; only a HIDDEN id gets the 404, so "
    "under isolation a member can tell hidden from missing (users-and-ownership.md §7) — "
    "docs/cn/reports/bugs-round-16.md",
)
@pytest.mark.parametrize(
    ("route", "code"),
    [(f"/api/observability/traces/{TRACE}", "observability.trace_not_found"),
     (f"/api/observability/sessions/{'s' * 64}", "observability.session_not_found")],
    ids=["trace", "session"],
)
def test_an_unknown_id_is_the_contract_404(client, monkeypatch, no_sleep, route, code):
    """NC-E05-03: unknown trace → 404 observability.trace_not_found; unknown session detail →
    404 observability.session_not_found. Probed with no rows at all (the query ran and
    returned nothing) and with the log group missing."""
    for logs in (FaultyLogs(rows=[]), FaultyLogs(start_error="ResourceNotFoundException")):
        obs.reset_cache()
        _patch_clients(monkeypatch, logs)
        res = client.get(route)
        assert res.status_code == 404, res.text
        assert res.json()["code"] == code


def _seed_chat_session(session_id: str) -> None:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="rail-agent", method="harness",
                      status="active", spec={})
        db.add(agent)
        db.flush()
        db.add(ChatSession(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
                           session_id=session_id, turns=1, last_at=datetime.now(UTC)))
        db.commit()
    finally:
        db.close()


RAIL_SESSION = "rail-" + "c" * 20


@pytest.mark.parametrize(
    "logs",
    [
        FaultyLogs(start_error="ThrottlingException"),
        FaultyLogs(start_error="AccessDeniedException"),
        FaultyLogs(poll_status="Failed"),
        FaultyLogs(poll_error="ThrottlingException"),
    ],
    ids=["start-throttle", "start-denied", "poll-failed", "poll-throttle"],
)
def test_the_chat_rail_degrades_instead_of_erroring(client, monkeypatch, no_sleep, logs):
    """observability.md: the rail is a side panel on the Chat page — `spans []` and
    `unavailable_reason = observability.query_failed`, HTTP 200."""
    _seed_chat_session(RAIL_SESSION)
    _patch_clients(monkeypatch, logs)
    res = client.get(f"/api/traces/{RAIL_SESSION}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["spans"] == [] and body["span_count"] == 0
    assert body["unavailable_reason"] == "observability.query_failed"
    assert body["session_id"] == RAIL_SESSION
    assert body["cloudwatch_url"].startswith("https://")


def test_the_chat_rail_on_a_missing_log_group_is_empty_with_no_reason(client, monkeypatch,
                                                                       no_sleep):
    _seed_chat_session(RAIL_SESSION)
    _patch_clients(monkeypatch, FaultyLogs(start_error="ResourceNotFoundException"))
    body = client.get(f"/api/traces/{RAIL_SESSION}").json()
    assert body["spans"] == [] and body["unavailable_reason"] is None


# ─── malformed result rows: the envelope, never a bare 500 ────────────────────

WELL_FORMED = {"traceId": TRACE, "spanId": "1" * 16, "name": "POST /invocations",
               "startTimeUnixNano": BASE_NS, "endTimeUnixNano": BASE_NS + 1_000_000,
               "attributes": {}, "status": {"code": "OK"}}

MALFORMED_MESSAGES: dict[str, Any] = {
    "plain-text-log-line": "2026-10-19 09:00:00 INFO not json at all",
    "json-array": json.dumps([1, 2, 3]),
    "json-string": json.dumps("a span?"),
    "json-number": json.dumps(42),
    "json-null": json.dumps(None),
    "status-as-string": json.dumps({**WELL_FORMED, "status": "OK"}),
    "attributes-as-list": json.dumps({**WELL_FORMED, "attributes": [{"k": "v"}]}),
    "resource-as-string": json.dumps({**WELL_FORMED, "resource": "svc"}),
    "timestamps-as-words": json.dumps({**WELL_FORMED, "startTimeUnixNano": "soon",
                                       "endTimeUnixNano": "later"}),
    "name-as-object": json.dumps({**WELL_FORMED, "name": {"x": 1}}),
}
# N16-obs53 (fixed in r16-g): these seven shapes used to turn the trace detail into a bare
# 500 — a non-object JSON row is skipped, a non-object `status` / `attributes` / `resource`
# is read as empty. The set stays so a regression names the shape.
FIVE_HUNDREDS: set[str] = set()
# rev-r16: the skip / coercion is never silent — `meta.malformed` counts it per shape and
# one WARNING per trace names the shapes (observability.md "Malformed rows")
EXPECTED_MALFORMED: dict[str, dict[str, int]] = {
    "plain-text-log-line": {"rows": 1},
    "json-array": {"rows": 1},
    "json-string": {"rows": 1},
    "json-number": {"rows": 1},
    "json-null": {"rows": 1},
    "status-as-string": {"status": 1},
    "attributes-as-list": {"attrs": 1},
    "resource-as-string": {"resource": 1},
    "timestamps-as-words": {},  # tolerated by `_span_ns` (offset 0), not a shape fault
    "name-as-object": {},  # `str()`-ed, not a shape fault
}
ZERO_MALFORMED = {"rows": 0, "attrs": 0, "status": 0, "resource": 0}


@pytest.fixture
def lenient_client():
    """A client that returns the 500 instead of re-raising it, so the status is observable."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("shape", sorted(MALFORMED_MESSAGES))
def test_a_malformed_span_row_never_turns_the_trace_detail_into_a_500(lenient_client, monkeypatch,
                                                                       no_sleep, shape, caplog):
    client = lenient_client
    caplog.set_level("WARNING", logger="launchpad.observability")
    rows = [{"@message": json.dumps(WELL_FORMED)}, {"@message": MALFORMED_MESSAGES[shape]}]
    _patch_clients(monkeypatch, _logs_for({f'filter traceId = "{TRACE}"': rows}))
    res = client.get(f"/api/observability/traces/{TRACE}")
    if shape in FIVE_HUNDREDS:
        if res.status_code != 500:
            pytest.fail(f"N16-obs53: {shape} no longer 500s ({res.status_code}) — "
                        "remove it from FIVE_HUNDREDS")
        pytest.xfail(f"N16-obs53: a {shape} row in the trace query result is a bare 500")
    assert res.status_code == 200, (shape, res.text)
    body = res.json()
    # the well-formed span is rendered whatever rode along with it
    assert any(n["span_id"] == "1" * 16 for n in _walk(body["tree"]))
    assert body["meta"]["span_count"] >= 1
    # … and the skip / coercion is counted, never silent
    want = {**ZERO_MALFORMED, **EXPECTED_MALFORMED[shape]}
    assert body["meta"]["malformed"] == want, (shape, body["meta"]["malformed"])
    warnings = [r for r in caplog.records if r.name == "launchpad.observability"]
    if any(want.values()):
        assert len(warnings) == 1, [r.getMessage() for r in warnings]
        message = warnings[0].getMessage()
        assert TRACE in message
        assert all(f"{k}={v}" in message for k, v in want.items() if v)
        assert "POST /invocations" not in message  # shapes only, never row content
    else:
        assert warnings == []
