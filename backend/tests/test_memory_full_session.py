"""Full session summaries: exact namespaces, AWS filters and migration read safety."""

import copy
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.routers import memory as router
from app.services import memory_console as mc
from app.services.project_memory import DualRead

from .conftest import set_default_resources
from .test_memory_console import AT, MEM_ID, MEMORY_PAYLOAD, StubControl, StubData, wire

KEY = "x-amz-agentcore-memory-recordType"
FILTER = [{"left": {"metadataKey": KEY}, "operator": "EQUALS_TO",
           "right": {"metadataValue": {"stringValue": "FULL_SESSION"}}}]

@pytest.fixture(autouse=True)
def configured_memory(client):
    set_default_resources({"memory_id": MEM_ID})


def summary_control(template="/summaries/{actorId}/{sessionId}", strategy_id="summary"):
    payload = copy.deepcopy(MEMORY_PAYLOAD)
    payload["memory"]["strategies"] = [{
        "strategyId": strategy_id, "name": "session_summary", "type": "SUMMARIZATION",
        "namespaceTemplates": [template], "namespaces": [template], "status": "ACTIVE",
    }]
    return StubControl(memory=payload)


def record(record_id="full", metadata=None):
    return {"memoryRecordId": record_id, "memoryStrategyId": "summary",
            "content": {"text": '<topic name="Decision">Ship the fix.</topic>'},
            "metadata": (metadata if metadata is not None
                         else {KEY: {"stringValue": "FULL_SESSION"}}),
            "createdAt": AT, "namespaces": ["/summaries/a__human/session-1"]}


def test_list_and_retrieval_forward_aws_type_filters(client, monkeypatch):
    data, _ = wire(monkeypatch, StubData(
        list_memory_records={"memoryRecordSummaries": [record()], "nextToken": "next"},
        retrieve_memory_records={"memoryRecordSummaries": [record()]},
    ), summary_control())
    page = client.get("/api/memory/records", params={
        "actor_id": "a__human", "strategy_id": "summary", "record_type": "FULL_SESSION",
        "next_token": "previous", "max_results": 7,
    })
    assert page.status_code == 200
    assert page.json()["next_token"] == "next"
    projected = page.json()["items"][0]
    assert projected["record_type"] == "FULL_SESSION"
    assert projected["metadata"] == {KEY: {"stringValue": "FULL_SESSION"}}
    assert projected["raw_text"] == record()["content"]["text"]
    assert data.kwargs_for("list_memory_records") == {
        "memoryId": MEM_ID, "namespacePath": "/summaries/a__human", "memoryStrategyId": "summary",
        "maxResults": 7, "nextToken": "previous", "metadataFilters": FILTER,
    }
    res = client.post("/api/memory/records/search", json={
        "query": "decision", "actor_id": "a__human", "strategy_id": "summary",
        "record_type": "FULL_SESSION", "top_k": 3,
    })
    assert res.status_code == 200
    assert data.kwargs_for("retrieve_memory_records")["searchCriteria"] == {
        "searchQuery": "decision", "topK": 3,
        "memoryStrategyId": "summary", "metadataFilters": FILTER,
    }


@pytest.mark.parametrize("metadata", [{}, {KEY: None}, {KEY: "FULL_SESSION"},
                                     {KEY: {"stringValue": 7}}, {KEY: {"stringValue": "FUTURE"}}])
def test_type_projection_is_safe_and_lossless(metadata):
    projected = mc._record(record(metadata=metadata))
    expected = "FUTURE" if metadata == {KEY: {"stringValue": "FUTURE"}} else None
    assert projected["record_type"] == expected
    assert projected["metadata"] == metadata
    assert projected["raw_text"] == record()["content"]["text"]


def test_future_top_level_type_and_unfiltered_requests(client, monkeypatch):
    raw = {**record(metadata={}), "memoryRecordType": "FULL_SESSION"}
    assert mc._record(raw)["record_type"] == "FULL_SESSION"
    data, _ = wire(monkeypatch, control=summary_control())
    assert client.get("/api/memory/records", params={"actor_id": "human"}).status_code == 200
    assert "metadataFilters" not in data.kwargs_for("list_memory_records")
    res = client.post("/api/memory/records/search", json={"query": "q", "actor_id": "human"})
    assert res.status_code == 200
    assert "metadataFilters" not in data.kwargs_for("retrieve_memory_records")["searchCriteria"]

@pytest.mark.parametrize("template, expected", [
    ("/summaries/{actorId}/{sessionId}", "/summaries/a__human/session-1"),
    ("/summaries/{actorId}/{sessionId}/", "/summaries/a__human/session-1/"),
    ("/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/",
     "/strategy/summary/actor/a__human/session/session-1/"),
    ("/summaries/{sessionId}/actor/{actorId}", "/summaries/session-1/actor/a__human"),
])
def test_session_reads_bind_exact_namespaces(client, monkeypatch, template, expected):
    data, _ = wire(monkeypatch, StubData(list_memory_records={
        "memoryRecordSummaries": [record()], "nextToken": "next",
    }), summary_control(template))
    res = client.get("/api/memory/namespaces", params={
        "actor_id": "a__human", "session_id": "session-1",
    })
    assert res.status_code == 200
    candidate = res.json()["items"][0]
    assert candidate["namespace"] == expected
    assert candidate["resolvable"] and not candidate["prefix"]
    res = client.get("/api/memory/records", params={
        "actor_id": "a__human", "session_id": "session-1", "namespace": expected,
        "strategy_id": "summary", "record_type": "FULL_SESSION", "next_token": "previous",
    })
    assert res.status_code == 200 and res.json()["next_token"] == "next"
    kwargs = data.kwargs_for("list_memory_records")
    assert kwargs["namespace"] == expected and "namespacePath" not in kwargs
    assert kwargs["metadataFilters"] == FILTER and kwargs["nextToken"] == "previous"


@pytest.mark.parametrize("template", ["/summaries/{actorId}/{sessionId}/{custom}",
                                    "/summaries/{actorId}", "/summaries/{sessionId}"])
def test_session_resolution_never_broadens_an_unsupported_template(client, monkeypatch, template):
    data, _ = wire(monkeypatch, control=summary_control(template))
    params = {"actor_id": "a__human", "session_id": "session-1"}
    candidate = client.get("/api/memory/namespaces", params=params).json()["items"][0]
    assert not candidate["resolvable"] and not candidate["prefix"]
    assert client.get("/api/memory/records", params=params).status_code == 400
    assert data.calls == []


@pytest.mark.parametrize("patch, expected", [
    ({"namespace": "/summaries/a__human/session-2"}, 400),
    ({"namespace": "/summaries/a__human"}, 400),
    ({"actor_id": None}, 400),
    ({"actor_id": "a/other"}, 400),
    ({"session_id": "s/other"}, 422),
    ({"session_id": "{other}"}, 422),
    ({"session_id": ""}, 422),
    ({"record_type": "CHUNK"}, 422),
    ({"strategy_id": "missing"}, 400),
])
def test_invalid_session_scope_is_refused_before_data_reads(client, monkeypatch, patch, expected):
    data, _ = wire(monkeypatch, control=summary_control())
    params = {"actor_id": "a__human", "session_id": "session-1", "strategy_id": "summary", **patch}
    params = {k: v for k, v in params.items() if v is not None}
    assert client.get("/api/memory/records", params=params).status_code == expected
    assert data.calls == []


def test_unknown_search_filter_is_validated(client, monkeypatch):
    data, _ = wire(monkeypatch)
    res = client.post("/api/memory/records/search", json={
        "query": "q", "namespace": "/facts/human", "record_type": "INVALID",
    })
    assert res.status_code == 422 and data.calls == []


def test_multiple_session_strategies_require_selection(client, monkeypatch):
    control = summary_control()
    control.memory["memory"]["strategies"].append({
        "strategyId": "other", "name": "other_summary", "type": "SUMMARIZATION",
        "namespaceTemplates": ["/recaps/{actorId}/{sessionId}"],
    })
    data, _ = wire(monkeypatch, control=control)
    params = {"actor_id": "human", "session_id": "session-1"}
    assert len(client.get("/api/memory/namespaces", params=params).json()["items"]) == 2
    assert client.get("/api/memory/records", params=params).status_code == 400
    assert data.calls == []
    res = client.get("/api/memory/records", params={**params, "strategy_id": "other"})
    assert res.status_code == 200
    assert data.kwargs_for("list_memory_records")["namespace"] == "/recaps/human/session-1"

def dual_memory(
    monkeypatch, template="/strategy/{memoryStrategyId}/actor/{actorId}/session/{sessionId}/"
):
    monkeypatch.setattr(router, "_dual", lambda ws: DualRead(
        "project", "old-memory", frozenset({"a"})
    ))
    control = summary_control(template)

    def get_memory(**kwargs):
        control.calls.append(("get_memory", kwargs))
        payload = copy.deepcopy(control.memory)
        if kwargs["memoryId"] == "old-memory":
            payload["memory"]["strategies"][0]["strategyId"] = "old-summary"
        return payload

    monkeypatch.setattr(control, "get_memory", get_memory)
    data, _ = wire(monkeypatch, StubData(
        list_memory_records=lambda kw: {"memoryRecordSummaries": [record(kw["memoryId"])],
                                       "nextToken": "next"},
        retrieve_memory_records=lambda kw: {"memoryRecordSummaries": [record(kw["memoryId"])]},
    ), control)
    return data


def test_session_dual_reads_resolve_each_memorys_own_strategy_id(client, monkeypatch):
    data = dual_memory(monkeypatch)
    params = {"actor_id": "a__human", "session_id": "session-1", "record_type": "FULL_SESSION"}
    candidates = client.get("/api/memory/namespaces", params={
        "actor_id": "a__human", "session_id": "session-1",
    }).json()["items"]
    assert [c["source"] for c in candidates] == ["project", "shared"]
    assert candidates[1]["namespace"] == "/strategy/old-summary/actor/a__human/session/session-1/"
    res = client.get("/api/memory/records", params={**params, "strategy_id": "summary"})
    assert res.status_code == 200
    assert [r["source"] for r in res.json()["items"]] == ["project", "shared"]
    reads = [kw for op, kw in data.calls if op == "list_memory_records"]
    assert [kw["memoryStrategyId"] for kw in reads] == ["summary", "old-summary"]
    assert reads[1]["namespace"] == candidates[1]["namespace"]
    assert all(kw["metadataFilters"] == FILTER for kw in reads)
    data.calls.clear()
    res = client.get("/api/memory/records", params={
        **params, "strategy_id": "old-summary", "namespace": candidates[1]["namespace"],
        "source": "shared", "next_token": "old-next",
    })
    assert res.status_code == 200
    assert data.kwargs_for("list_memory_records")["memoryId"] == "old-memory"
    assert data.kwargs_for("list_memory_records")["nextToken"] == "old-next"
    assert res.json()["items"][0]["source"] == "shared"
    data.calls.clear()
    assert client.get("/api/memory/records", params={**params, "strategy_id": "summary",
                                                  "next_token": "next"}).status_code == 200
    assert len(data.calls) == 1  # no replay of the legacy first page


@pytest.mark.parametrize("source", [None, "shared", "project"])
def test_actor_prefix_migration_listing_preserves_filters(client, monkeypatch, source):
    data = dual_memory(monkeypatch, "/summaries/{actorId}/{sessionId}")
    params = {"actor_id": "a__human", "strategy_id": "summary", "record_type": "FULL_SESSION"}
    if source:
        params["source"] = source
    assert client.get("/api/memory/records", params=params).status_code == 200
    reads = [kw for op, kw in data.calls if op == "list_memory_records"]
    assert len(reads) == (2 if source is None else 1)
    assert all(kw["metadataFilters"] == FILTER for kw in reads)


def test_migration_retrieval_filters_both_resources(client, monkeypatch):
    data = dual_memory(monkeypatch, "/summaries/{actorId}/{sessionId}")
    res = client.post("/api/memory/records/search", json={
        "actor_id": "a__human", "strategy_id": "summary", "query": "q",
        "record_type": "FULL_SESSION",
    })
    assert res.status_code == 200
    reads = [kw for op, kw in data.calls if op == "retrieve_memory_records"]
    assert len(reads) == 2
    assert all(kw["searchCriteria"]["metadataFilters"] == FILTER for kw in reads)


def test_other_project_cannot_select_shared_session_memory(client, monkeypatch):
    data = dual_memory(monkeypatch)
    res = client.get("/api/memory/records", params={
        "actor_id": "foreign__human", "session_id": "session-1", "source": "shared",
        "record_type": "FULL_SESSION",
    })
    assert res.status_code == 404 and data.calls == []


@pytest.mark.parametrize("code", ["ResourceNotFoundException", "AccessDeniedException"])
def test_legacy_discovery_failure_keeps_primary_candidates_and_records(client, monkeypatch, code):
    data = dual_memory(monkeypatch)
    control = mc.control_client()
    original = control.get_memory

    def get_memory(**kwargs):
        if kwargs["memoryId"] == "old-memory":
            raise ClientError(
                {"Error": {"Code": code, "Message": "legacy unavailable"}}, "GetMemory"
            )
        return original(**kwargs)

    monkeypatch.setattr(control, "get_memory", get_memory)
    params = {"actor_id": "a__human", "session_id": "session-1"}
    discovery = client.get("/api/memory/namespaces", params=params)
    assert discovery.status_code == 200
    assert len(discovery.json()["items"]) == 1
    assert "legacy unavailable" in discovery.json()["shared_error"]
    res = client.get("/api/memory/records", params={**params, "record_type": "FULL_SESSION"})
    assert res.status_code == 200
    assert [r["record_id"] for r in res.json()["items"]] == [MEM_ID]
    assert "legacy unavailable" in res.json()["shared_error"]
    assert all(kw["memoryId"] == MEM_ID for op, kw in data.calls if op == "list_memory_records")
    # An explicit shared read still reports its failure; no success is fabricated.
    shared = client.get("/api/memory/records", params={**params, "source": "shared"})
    assert shared.status_code != 200


def test_legacy_record_failure_does_not_hide_the_primary_recap(client, monkeypatch):
    data = dual_memory(monkeypatch)

    def list_memory_records(kwargs):
        if kwargs["memoryId"] == "old-memory":
            raise ClientError({"Error": {"Code": "AccessDeniedException",
                                         "Message": "legacy denied"}}, "ListMemoryRecords")
        return {"memoryRecordSummaries": [record(MEM_ID)]}

    data.responses["list_memory_records"] = list_memory_records
    params = {"actor_id": "a__human", "session_id": "session-1", "record_type": "FULL_SESSION"}
    res = client.get("/api/memory/records", params=params)
    assert res.status_code == 200
    assert [r["record_id"] for r in res.json()["items"]] == [MEM_ID]
    assert "legacy denied" in res.json()["shared_error"]
    shared = client.get("/api/memory/records", params={**params, "source": "shared"})
    assert shared.status_code == 403


def test_resolved_namespace_visibility_is_checked_before_reading(client, monkeypatch):
    data, _ = wire(monkeypatch, control=summary_control("/blocked/{actorId}/{sessionId}"))
    hidden = SimpleNamespace(
        hides_actor=lambda actor: False, hides_session=lambda session: False,
        hides_namespace=lambda namespace: bool(namespace and "blocked" in namespace),
    )
    monkeypatch.setattr(router, "_hidden", lambda *args: hidden)
    params = {"actor_id": "a__human", "session_id": "session-1"}
    assert client.get("/api/memory/namespaces", params=params).status_code == 404
    assert client.get("/api/memory/records", params=params).status_code == 404
    assert data.calls == []
