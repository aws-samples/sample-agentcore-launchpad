"""N17-obs463 — every time the API returns is in the display zone (Asia/Shanghai, +08:00).

User ruling 2026-10-26: the console and the API show and return times in Beijing time.
Storage stays UTC (naive ledger columns, AWS / artefact strings with ``+00:00``); the API
serialises the SAME instant with the display offset, in one place
(``app.core.display_time``): the app's default response class, every explicit JSON response
and the SSE encoder. The live value that started it: run bdd265252e9f, created at
``2026-10-24T10:29:54.079198`` (naive UTC) = ``18:29:54`` Beijing.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from starlette.responses import Response, StreamingResponse

from app.core import display_time
from app.core.config import get_settings
from app.core.display_time import (
    DisplayTimeJSONResponse,
    display_iso,
    to_display_payload,
    to_utc_naive,
)
from app.main import create_app
from app.services.chat import sse_encode
from tests.test_assistant import harness, ready  # noqa: F401 — fixtures registered here

APP = Path(__file__).resolve().parents[1] / "app"
LIVE = "2026-10-24T18:29:54.079198+08:00"
INSTANT = datetime(2026, 10, 24, 10, 29, 54, 79198, tzinfo=UTC)


@pytest.mark.parametrize("value", [
    datetime(2026, 10, 24, 10, 29, 54, 79198),                       # naive ledger column
    INSTANT,                                                          # aware UTC
    INSTANT.astimezone(timezone(timedelta(hours=-7))),                # aware, another zone
    "2026-10-24T10:29:54.079198",                                     # naive ISO string
    "2026-10-24 10:29:54.079198",                                     # space-naive
    "2026-10-24T10:29:54.079198+00:00",                               # AWS / artefact
    "2026-10-24T10:29:54.079198Z",
    "2026-10-24T10:29:54.079198000Z",                                 # nanoseconds
    "2026-10-24T18:29:54.079198+08:00",                               # already displayed
])
def test_every_input_shape_serialises_to_the_same_instant_at_plus_eight(value):
    out = to_display_payload({"created_at": value})["created_at"]
    assert out == LIVE
    assert datetime.fromisoformat(out) == INSTANT


def test_a_whole_body_is_walked_and_text_around_a_time_is_left_alone():
    body = {"run": {"created_at": "2026-10-24T10:29:54.079198", "id": "bdd265252e9f",
                    "events": [{"ts": "2026-10-24T00:59:30.000001+00:00", "msg": "generate"}]},
            "note": "started 2026-10-24T10:29:54 by the scheduler",
            "day": "2026-10-24", "count": 3, "ok": True, "none": None,
            "version": "2026.10.24", "empty": ""}
    out = to_display_payload(body)
    assert out["run"]["created_at"] == LIVE
    assert out["run"]["events"][0]["ts"] == "2026-10-24T08:59:30.000001+08:00"
    assert {k: out[k] for k in ("note", "day", "count", "ok", "none", "version", "empty")} == {
        k: body[k] for k in ("note", "day", "count", "ok", "none", "version", "empty")}
    assert display_iso("not a time") == "not a time"


def test_the_display_zone_is_a_setting(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_DISPLAY_TIMEZONE", "UTC")
    get_settings.cache_clear()
    try:
        assert to_display_payload({"at": "2026-10-24T10:29:54.079198"}) == \
            {"at": "2026-10-24T10:29:54.079198+00:00"}
    finally:
        monkeypatch.delenv("LAUNCHPAD_DISPLAY_TIMEZONE")
        get_settings.cache_clear()
    assert get_settings().display_timezone == "Asia/Shanghai"


def test_an_unknown_display_zone_is_refused_at_startup(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_DISPLAY_TIMEZONE", "Mars/Olympus")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match="display_timezone"):
            get_settings()
    finally:
        monkeypatch.delenv("LAUNCHPAD_DISPLAY_TIMEZONE")
        get_settings.cache_clear()


def test_inbound_times_are_converted_never_relabelled():
    beijing = datetime.fromisoformat(LIVE)
    assert to_utc_naive(beijing) == datetime(2026, 10, 24, 10, 29, 54, 79198)
    naive = datetime(2026, 10, 24, 10, 29, 54)
    assert to_utc_naive(naive) is naive
    from app.services.users import as_utc
    assert as_utc(beijing) == INSTANT and as_utc(beijing).utcoffset() == timedelta(0)


# ── only OUR times: content is data (lead review of N17-obs463) ─────────────────────────

ISO = "2026-10-24 10:00"  # a wall-clock time a user TYPED — not UTC, never rewritten
ISO_T = "2026-10-12T09:00:00"


def test_the_time_key_rule_is_the_enumerated_one():
    for key in ("created_at", "last_used_at", "publishedAt", "lastUpdatedAt", "createdAt",
                "keyModifiedAt", "at", "ts", "time", "timestamp", "bucket", "bin", "first",
                "last", "since", "locked_until", "last_seen", "first_event"):
        assert display_time.is_time_key(key), key
    for key in ("date", "text", "name", "format", "At", "status", "data", "value", "chat"):
        assert not display_time.is_time_key(key), key
    assert display_time.TIME_KEYS == {
        "at", "ts", "time", "timestamp", "start", "end", "since", "until", "first", "last",
        "first_event", "last_event", "last_seen", "locked_until", "bucket", "bin"}


@pytest.mark.parametrize(("name", "body", "path"), [
    ("dataset scenario", {"items": [{"scenario_id": "s1", "deadline_at": ISO,
                                     "turns": [{"input": ISO, "expected_response": ISO_T}]}]},
     ("items", 0)),
    ("dataset prompt item", {"items": [{"prompt": ISO, "at": ISO_T}]}, ("items", 0)),
    ("chat message text", {"messages": [{"role": "user", "text": ISO, "at": ISO_T}]},
     ("messages", 0, "text")),
    ("tool argument in a trace", {"spans": [{"name": "execute_tool",
                                             "attributes": {"gen_ai.tool.call.arguments":
                                                            '{"date": "x"}', "start": ISO_T},
                                             "input": {"date": ISO_T, "created_at": ISO_T}}]},
     ("spans", 0, "attributes")),
    ("tool input in a transcript", {"turns": [{"tools": [{"name": "book", "input":
                                                          {"start_at": ISO_T}}]}]},
     ("turns", 0, "tools", 0, "input")),
    ("proposal content", {"proposal": {"created_at": ISO_T, "content": {
        "name": "x", "golden_tests": [{"input": ISO_T, "created_at": ISO_T}],
        "schedule_at": ISO}}}, ("proposal", "content")),
    ("memory record content", {"records": [{"id": "r", "created_at": ISO_T,
                                            "content": {"text": ISO, "due_at": ISO_T},
                                            "metadata": {"updated_at": ISO_T}}]},
     ("records", 0, "content")),
    ("KB document metadata", {"documents": [{"key": "a.md", "metadata": {"published_at": ISO}}]},
     ("documents", 0, "metadata")),
    ("evaluator config", {"evaluator": {"config": {"rating_scale_at": ISO_T, "at": ISO}}},
     ("evaluator", "config")),
    ("agent spec env", {"agent": {"spec": {"env": {"START_AT": ISO_T}, "created_at": ISO_T}}},
     ("agent", "spec")),
])
def test_content_comes_back_unchanged(name, body, path):
    out = to_display_payload(body)
    want, got = body, out
    for step in path:
        want, got = want[step], got[step]
    assert got == want, name
    if name in ("chat message text",):
        assert out["messages"][0]["at"] == "2026-10-12T17:00:00+08:00"  # the message's own time
    if name == "proposal content":
        assert out["proposal"]["created_at"] == "2026-10-12T17:00:00+08:00"
    if name == "memory record content":
        assert out["records"][0]["created_at"] == "2026-10-12T17:00:00+08:00"


def test_stream_text_events_pass_untouched_and_the_envelope_converts():
    for event in ("delta", "text"):
        frame = sse_encode({"event": event, "data": {"text": ISO_T, "at": ISO_T}})
        assert json.loads(frame.split("data: ", 1)[1]) == {"text": ISO_T, "at": ISO_T}
    frame = sse_encode({"event": "tool", "data": {"name": "t", "input": {"at": ISO_T},
                                                  "at": ISO_T}})
    assert json.loads(frame.split("data: ", 1)[1]) == {
        "name": "t", "input": {"at": ISO_T}, "at": "2026-10-12T17:00:00+08:00"}


def test_a_dataset_get_put_round_trip_keeps_its_content_byte_for_byte(client):
    items = [
        {"prompt": ISO, "expected": ISO_T, "deadline_at": ISO_T, "notes": {"created_at": ISO}},
        {"scenario_id": "s2", "turns": [{"input": f"meet at {ISO}", "expected_response": ISO_T}],
         "metadata": {"due_at": ISO_T}, "starts_at": ISO},
    ]
    created = client.post("/api/eval/datasets", json={"name": "obs463-roundtrip", "items": items})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["items"] == items
    assert body["created_at"].endswith("+08:00")  # the dataset's own time is ours
    def read() -> dict:
        listed = client.get("/api/eval/datasets").json()["datasets"]
        return next(d for d in listed if d["id"] == body["id"])

    got = read()
    assert got["items"] == items
    put = client.put(f"/api/eval/datasets/{body['id']}", json={"items": got["items"]})
    assert put.status_code == 200, put.text
    assert json.dumps(read()["items"], sort_keys=True) == json.dumps(items, sort_keys=True)
    from app.core.db import SessionLocal
    from app.evaluation.models import EvalDataset
    db = SessionLocal()
    try:  # the STORED items too: nothing converted was written back
        assert db.get(EvalDataset, body["id"]).items == items
    finally:
        db.close()


# ── the API, end to end ─────────────────────────────────────────────────────────────────


def test_a_ledger_endpoint_answers_in_beijing_time(client):
    created = client.post("/api/announcements", json={
        "title": "t", "body": "b", "link_url": "/videos", "link_label": "v"})
    assert created.status_code == 201, created.text
    for row in (created.json(), client.get("/api/announcements/manage").json()["announcements"][0]):
        for key in ("created_at", "updated_at"):
            assert row[key].endswith("+08:00"), (key, row[key])
            assert abs(datetime.fromisoformat(row[key]) - datetime.now(UTC)) < timedelta(minutes=5)


def test_errors_and_capabilities_go_through_the_same_response(client):
    assert client.get("/api/announcements/nope").status_code == 404
    caps = client.get("/api/capabilities").json()
    assert caps["display_timezone"] == "Asia/Shanghai"


def test_stream_events_carry_display_times():
    frame = sse_encode({"event": "message",
                        "data": {"at": "2026-10-24T10:29:54.079198", "text": "hi"}})
    data = json.loads(frame.split("data: ", 1)[1])
    assert data == {"at": LIVE, "text": "hi"}


# ── guards ──────────────────────────────────────────────────────────────────────────────

def _walk(routes) -> list:
    """FastAPI 0.139 keeps included routers as ``_IncludedRouter`` wrappers (test_route_policy)."""
    found = []
    for route in routes:
        if type(route).__name__ == "_IncludedRouter":
            found += _walk(route.original_router.routes)
        else:
            found.append(route)
    return found


def test_every_json_route_serialises_through_the_display_response():
    """A route either inherits the app default (a placeholder) or names the display class /
    a stream; no route opts out to a plain JSON class."""
    app = create_app()
    assert app.router.default_response_class is DisplayTimeJSONResponse
    routes = [r for r in _walk(app.routes) if isinstance(r, APIRoute)]
    assert len(routes) > 300
    wrong = []
    for route in routes:
        cls = route.response_class
        if type(cls).__name__ == "DefaultPlaceholder":
            continue  # the app's default_response_class applies
        if isinstance(cls, type) and issubclass(cls, (DisplayTimeJSONResponse, StreamingResponse)):
            continue
        if cls is Response and route.status_code == 204:
            continue  # no body
        wrong.append((route.path, cls))
    assert wrong == []


def test_no_app_code_builds_a_plain_json_response():
    """A route or handler that returns ``JSONResponse(...)`` itself would skip the walk."""
    offenders = []
    for path in APP.rglob("*.py"):
        if path.name == "display_time.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "JSONResponse":
                offenders.append(f"{path.relative_to(APP)}:{node.lineno}")
            if isinstance(node, ast.ImportFrom) and any(
                    a.name in ("JSONResponse", "ORJSONResponse") for a in node.names):
                offenders.append(f"{path.relative_to(APP)}:{node.lineno} (import)")
    assert offenders == []


def _datetime_fields(schema, components, seen=None):
    """Every ``format: date-time`` property path reachable from an OpenAPI schema."""
    seen = set() if seen is None else seen
    if not isinstance(schema, dict):
        return
    ref = schema.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return
        seen.add(name)
        yield from _datetime_fields(components.get(name, {}), components, seen)
        return
    if schema.get("format") == "date-time":
        yield schema.get("title") or "?"
    for key in ("properties",):
        for sub in (schema.get(key) or {}).values():
            yield from _datetime_fields(sub, components, seen)
    for key in ("items", "additionalProperties"):
        yield from _datetime_fields(schema.get(key), components, seen)
    for key in ("anyOf", "allOf", "oneOf"):
        for sub in schema.get(key) or []:
            yield from _datetime_fields(sub, components, seen)


def test_every_openapi_response_model_with_a_datetime_serialises_at_plus_eight():
    """Walk the response models: each one that declares a date-time field must come out of a
    route whose response class is the display one, and an instance must serialise with +08:00."""
    app = create_app()
    spec = app.openapi()
    components = spec.get("components", {}).get("schemas", {})
    covered = 0
    by_endpoint = {}
    for item in spec["paths"].values():
        for method, operation in item.items():
            by_endpoint[(operation.get("operationId"), method)] = operation
    for route in _walk(app.routes):
        if not isinstance(route, APIRoute) or route.response_model is None:
            continue
        operation = next((op for (op_id, _m), op in by_endpoint.items()
                          if op_id == route.unique_id), {})
        fields = [f for resp in (operation.get("responses") or {}).values()
                  for media in (resp.get("content") or {}).values()
                  for f in _datetime_fields(media.get("schema") or {}, components)]
        if not fields:
            continue
        covered += 1
        cls = route.response_class
        assert type(cls).__name__ == "DefaultPlaceholder" or issubclass(
            cls, DisplayTimeJSONResponse), route.path
    rendered = DisplayTimeJSONResponse({"at": datetime(2026, 10, 24, 10, 29, 54, 79198)
                                        .isoformat()}).body
    assert json.loads(rendered) == {"at": LIVE}
    assert covered >= 1  # at least one response model declares a date-time


def test_the_module_zone_cache_follows_the_setting():
    assert display_time.display_zone().key == "Asia/Shanghai"  # type: ignore[attr-defined]


def _times_without_display_offset(value, path="$"):
    if isinstance(value, str):
        if display_time._ISO_DATETIME.fullmatch(value) and not value.endswith("+08:00"):
            yield path, value
    elif isinstance(value, dict):
        for key, sub in value.items():
            yield from _times_without_display_offset(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, sub in enumerate(value):
            yield from _times_without_display_offset(sub, f"{path}[{i}]")


def test_every_parameterless_get_answers_only_plus_eight_times(client):
    """A live sweep of the console and public surfaces over a ledger with rows in it."""
    client.post("/api/announcements", json={
        "title": "t", "body": "b", "link_url": "/videos", "link_label": "v"})
    seen, bad = 0, []
    for path, item in client.app.openapi()["paths"].items():
        if "get" not in item or "{" in path:
            continue
        try:
            response = client.get(path)
        except Exception:  # noqa: BLE001 — a hermetic route that needs AWS raises; not a time
            continue
        if not response.headers.get("content-type", "").startswith("application/json"):
            continue
        seen += 1
        bad += [(path, *hit) for hit in _times_without_display_offset(response.json())]
    assert seen > 50
    assert bad == []


def test_a_proposal_edit_save_and_approve_keeps_its_content_and_hash_pin(client, request):
    """Proposal content with exact-ISO strings: the SSE proposal event, the conversation GET and
    an edit-save of what the console read all keep the content as written, the hash the console
    received pins the stored revision, and approve with that pin starts the deploy."""
    from app.assistant import service as assistant_service
    from app.core.db import SessionLocal
    from tests.test_assistant import BASE, VALID_PROPOSAL, _approve, _latest, _open, _propose

    arch = request.getfixturevalue("harness")  # tests.test_assistant's fixtures, by name
    request.getfixturevalue("ready")

    content = {**VALID_PROPOSAL, "summary": ISO_T,
               "golden_tests": [{"id": "GT-1", "input": ISO, "expected_response": ISO_T,
                                 "source": "customer_pain_point"}]}
    cid = _open(client)
    r1 = _propose(client, arch, cid, content)
    assert r1["content"]["summary"] == ISO_T
    assert r1["content"]["golden_tests"][0]["input"] == ISO
    shown = _latest(client, cid)["proposals"][-1]
    assert shown["content"] == r1["content"] and shown["content_hash"] == r1["content_hash"]
    edited = client.put(f"{BASE}/conversations/{cid}/proposal",
                        json={"content": {**shown["content"], "name": "hr-helpdesk-v2"}})
    assert edited.status_code == 200, edited.text
    r2 = edited.json()["proposal"]
    assert r2["content"]["summary"] == ISO_T and r2["content"]["golden_tests"][0]["input"] == ISO
    db = SessionLocal()
    try:  # what was stored is what was written, and the shown hash is the stored pin
        stored = assistant_service.latest_proposal(db, cid)
        assert stored.content_hash == r2["content_hash"]
        assert stored.content["summary"] == ISO_T
        assert stored.content["golden_tests"][0]["input"] == ISO
    finally:
        db.close()
    approved = _approve(client, cid, r2)
    assert approved.status_code in (200, 202), approved.text  # the pin matched


def test_obs464_recommendation_edits_convert_their_times_and_never_the_prompt():
    """obs464's shapes (merged later): `accepted.edits[].at` is ours; every `system_prompt`
    (the card's text, the edits, the revise route's 409 body) is content."""
    body = {"recommendation": {"created_at": ISO_T, "system_prompt": ISO_T, "accepted": {
        "at": ISO_T, "system_prompt": ISO, "edits": [
            {"at": ISO_T, "by": "demo-admin", "system_prompt": ISO_T, "version_before": "3"}]}},
        "code": "recommendation.edit_conflict", "detail": {"system_prompt": ISO_T}}
    out = to_display_payload(body)
    rec = out["recommendation"]
    assert rec["created_at"] == rec["accepted"]["at"] == rec["accepted"]["edits"][0]["at"] == \
        "2026-10-12T17:00:00+08:00"
    assert rec["system_prompt"] == ISO_T and rec["accepted"]["system_prompt"] == ISO
    assert rec["accepted"]["edits"][0]["system_prompt"] == ISO_T
    assert out["detail"] == {"system_prompt": ISO_T}
