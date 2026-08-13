"""Memory console API — hermetic contract tests.

AWS is stubbed at the client-factory boundary (``memory_console.data_client`` /
``control_client``), so these tests pin the *projection* the console depends on:
how compound actor ids decode back to agents, how namespace templates resolve,
how payloads are normalized, how pagination round-trips, and how failures map to
error envelopes. They also pin the read-only stance.

The real-AWS counterpart lives in ``backend/scripts/e2e_*.py`` and is not part of
the verify gate.
"""

import inspect
import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

import app.services.memory_console as mc
from app.core.db import SessionLocal
from app.models.ledger import Agent, ChatMessage, ChatSession

from .conftest import set_default_resources

MEM_ID = "launchpad_memory-ABC123"
AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)

MEMORY_PAYLOAD = {
    "memory": {
        "id": MEM_ID,
        "arn": f"arn:aws:bedrock-agentcore:us-west-2:1:memory/{MEM_ID}",
        "name": "launchpad_memory",
        "description": "Launchpad shared memory",
        "status": "ACTIVE",
        "eventExpiryDuration": 30,
        "memoryExecutionRoleArn": "arn:aws:iam::1:role/launchpad-memory",
        "createdAt": AT,
        "updatedAt": AT,
        "strategies": [
            {
                "strategyId": "strat-facts",
                "name": "semantic_facts",
                "type": "SEMANTIC",
                "status": "ACTIVE",
                "namespaces": ["/facts/{actorId}"],
                "namespaceTemplates": ["/facts/{actorId}"],
            },
            {
                "strategyId": "strat-prefs",
                "name": "user_preferences",
                "type": "USER_PREFERENCE",
                "status": "ACTIVE",
                "namespaces": ["/preferences/{actorId}"],
                "namespaceTemplates": ["/preferences/{actorId}"],
            },
        ],
    }
}


class StubControl:
    def __init__(self, memory=None, memories=None):
        self.memory = memory if memory is not None else MEMORY_PAYLOAD
        self.memories = memories if memories is not None else [
            {"id": MEM_ID, "arn": "arn:1", "status": "ACTIVE"},
            {"id": "other-mem-9", "arn": "arn:2", "status": "ACTIVE"},
        ]
        self.calls: list[tuple[str, dict]] = []

    def get_memory(self, **kw):
        self.calls.append(("get_memory", kw))
        return self.memory

    def list_memories(self, **kw):
        self.calls.append(("list_memories", kw))
        return {"memories": self.memories}


class StubData:
    """Records every call so tests can assert the exact AWS request shape."""

    def __init__(self, **responses):
        self.responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _reply(self, op, kw):
        self.calls.append((op, kw))
        value = self.responses.get(op, {})
        return value(kw) if callable(value) else value

    def list_actors(self, **kw):
        return self._reply("list_actors", kw)

    def list_sessions(self, **kw):
        return self._reply("list_sessions", kw)

    def list_events(self, **kw):
        return self._reply("list_events", kw)

    def list_memory_records(self, **kw):
        return self._reply("list_memory_records", kw)

    def retrieve_memory_records(self, **kw):
        return self._reply("retrieve_memory_records", kw)

    def list_memory_extraction_jobs(self, **kw):
        return self._reply("list_memory_extraction_jobs", kw)

    def kwargs_for(self, op: str) -> dict:
        return next(kw for name, kw in self.calls if name == op)


@pytest.fixture
def configured(client):
    """Bootstrap has run: memory_id on the workspace the request resolves to."""
    set_default_resources({"memory_id": MEM_ID})


@pytest.fixture
def unconfigured(client):
    set_default_resources({})


def wire(monkeypatch, data=None, control=None):
    data = data or StubData()
    control = control or StubControl()
    monkeypatch.setattr(mc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(mc, "control_client", lambda _ws=None: control)
    return data, control


def make_agent(name="mem-console-agent") -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id="default", name=name, method="zip_runtime", status="active",
        spec={"name": name},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #


def test_overview_projects_resource_strategies_and_platform_marker(
    client, configured, monkeypatch
):
    wire(
        monkeypatch,
        StubData(list_actors={"actorSummaries": [{"actorId": "a__river"}]}),
    )
    body = client.get("/api/memory/overview").json()

    assert body["configured"] is True
    assert body["memory"]["id"] == MEM_ID
    assert body["memory"]["event_expiry_days"] == 30
    assert body["memory"]["created_at"] == AT.isoformat()  # datetime → ISO string
    assert [s["name"] for s in body["strategies"]] == ["semantic_facts", "user_preferences"]
    assert body["strategies"][0]["namespace_templates"] == ["/facts/{actorId}"]
    # the singleton is marked among the account's other memory resources
    marked = {m["id"]: m["is_platform"] for m in body["other_memories"]}
    assert marked == {MEM_ID: True, "other-mem-9": False}


def test_overview_actor_count_declares_its_own_bound(client, configured, monkeypatch):
    """A capped count must announce the cap rather than under-report silently."""
    wire(
        monkeypatch,
        StubData(
            list_actors={
                "actorSummaries": [{"actorId": f"a{i}__river"} for i in range(100)],
                "nextToken": "more",
            }
        ),
    )
    body = client.get("/api/memory/overview").json()
    assert body["actor_count"] == 100
    assert body["actor_count_truncated"] is True


def test_overview_soft_state_before_bootstrap(client, unconfigured, monkeypatch):
    wire(monkeypatch)
    res = client.get("/api/memory/overview")
    assert res.status_code == 200
    assert res.json() == {
        "configured": False,
        "memory": None,
        "strategies": [],
        "actor_count": 0,
        "actor_count_truncated": False,
        "other_memories": [],
    }


@pytest.mark.parametrize(
    "path",
    [
        "/api/memory/actors",
        "/api/memory/sessions?actor_id=a__river",
        "/api/memory/events?actor_id=a__river&session_id=s1",
        "/api/memory/namespaces?actor_id=a__river",
        "/api/memory/records?actor_id=a__river",
        "/api/memory/extraction-jobs",
    ],
)
def test_other_endpoints_error_cleanly_before_bootstrap(
    client, unconfigured, monkeypatch, path
):
    wire(monkeypatch)
    res = client.get(path)
    assert res.status_code == 409
    assert res.json()["code"] == "memory.not_configured"


# --------------------------------------------------------------------------- #
# Actors — compound-id decoding
# --------------------------------------------------------------------------- #


def test_actor_decoding_resolves_agent_name(client, configured, monkeypatch):
    agent_id = make_agent(name="front-desk")
    wire(
        monkeypatch,
        StubData(list_actors={"actorSummaries": [{"actorId": f"{agent_id}__river"}]}),
    )
    item = client.get("/api/memory/actors").json()["items"][0]
    assert item == {
        "actor_id": f"{agent_id}__river",
        "agent_id": agent_id,
        "human_actor": "river",
        "scoped": True,
        "agent_name": "front-desk",
    }


def test_actor_decoding_survives_deleted_agent(client, configured, monkeypatch):
    """A memory partition outlives the agent row — stay scoped, name is null."""
    wire(
        monkeypatch,
        StubData(list_actors={"actorSummaries": [{"actorId": "deadbeef__river"}]}),
    )
    item = client.get("/api/memory/actors").json()["items"][0]
    assert item["scoped"] is True
    assert item["agent_id"] == "deadbeef"
    assert item["agent_name"] is None


def test_unscoped_actor_is_not_given_a_fake_agent(client, configured, monkeypatch):
    wire(monkeypatch, StubData(list_actors={"actorSummaries": [{"actorId": "river"}]}))
    item = client.get("/api/memory/actors").json()["items"][0]
    assert item["scoped"] is False
    assert item["agent_id"] is None
    assert item["human_actor"] == "river"


def test_actor_human_part_may_contain_the_separator(configured):
    """Split on the FIRST separator only — human actor ids are free-form."""
    decoded = mc.decode_actor("agent1__runtime__diagnostic")
    assert decoded["agent_id"] == "agent1"
    assert decoded["human_actor"] == "runtime__diagnostic"


def test_actor_names_resolved_in_one_query(client, configured, monkeypatch):
    """N actors sharing an agent must not mean N ledger queries."""
    agent_id = make_agent(name="shared")
    data, _ = wire(
        monkeypatch,
        StubData(
            list_actors={
                "actorSummaries": [
                    {"actorId": f"{agent_id}__river"},
                    {"actorId": f"{agent_id}__demo"},
                    {"actorId": f"{agent_id}__eval"},
                ]
            }
        ),
    )
    items = client.get("/api/memory/actors").json()["items"]
    assert {i["agent_name"] for i in items} == {"shared"}
    assert len(data.calls) == 1  # one AWS page, one ledger batch


# --------------------------------------------------------------------------- #
# Sessions — ledger join
# --------------------------------------------------------------------------- #


def test_sessions_join_ledger_when_console_wrote_them(client, configured, monkeypatch):
    agent_id = make_agent(name="chatty")
    session_id = "s" * 40
    db = SessionLocal()
    db.add(ChatSession(workspace_id="default", agent_id=agent_id,
                       session_id=session_id, actor_id="river", turns=2))
    db.add(ChatMessage(workspace_id="default", agent_id=agent_id,
                       session_id=session_id, role="user", text="hi"))
    db.add(ChatMessage(workspace_id="default", agent_id=agent_id,
                       session_id=session_id, role="agent", text="yo"))
    db.commit()
    db.close()

    wire(
        monkeypatch,
        StubData(
            list_sessions={
                "sessionSummaries": [
                    {"sessionId": session_id, "actorId": f"{agent_id}__river", "createdAt": AT},
                    {"sessionId": "eval-run-1", "actorId": f"{agent_id}__river"},
                ]
            }
        ),
    )
    items = client.get(
        "/api/memory/sessions", params={"actor_id": f"{agent_id}__river"}
    ).json()["items"]

    assert items[0]["ledger"] == {
        "agent_id": agent_id,
        "agent_name": "chatty",
        "human_actor": "river",
        "turns": 2,
        "message_count": 2,
    }
    # sessions the console never wrote (eval runs, /v1 callers) stay unjoined
    assert items[1]["ledger"] is None


# --------------------------------------------------------------------------- #
# Events — payload normalization
# --------------------------------------------------------------------------- #


def test_event_payloads_normalized_without_truncation_or_binary(
    client, configured, monkeypatch
):
    long_text = "x" * 500
    wire(
        monkeypatch,
        StubData(
            list_events={
                "events": [
                    {
                        "eventId": "e1",
                        "eventTimestamp": AT,
                        "payload": [
                            {
                                "conversational": {
                                    "role": "USER",
                                    "content": {"text": long_text},
                                }
                            },
                            {"blob": b"\x00\x01\x02"},
                            {"somethingNew": {"opaque": True}},
                        ],
                    }
                ]
            }
        ),
    )
    event = client.get(
        "/api/memory/events", params={"actor_id": "a__river", "session_id": "s1"}
    ).json()["items"][0]

    assert event["at"] == AT.isoformat()
    # full text survives (the UI clamps); blob reduced to a size; unknown dropped
    assert event["payload"][0] == {
        "kind": "conversational",
        "role": "USER",
        "text": long_text,
        "parts": [],
        "blob_bytes": None,
    }
    assert event["payload"][1] == {
        "kind": "blob",
        "role": None,
        "text": None,
        "parts": [],
        "blob_bytes": 3,
    }
    assert len(event["payload"]) == 2


def conversational(text: str, role: str = "ASSISTANT") -> dict:
    return {"conversational": {"role": role, "content": {"text": text}}}


def test_harness_message_envelope_is_decoded(client, configured, monkeypatch):
    """Harness agents persist a whole message envelope as the event text —
    showing that raw JSON in a memory inspector is unreadable."""
    envelope = json.dumps(
        {"message": {"role": "assistant", "content": [{"text": "Here's what I found"}]}}
    )
    wire(
        monkeypatch,
        StubData(list_events={"events": [{"eventId": "e", "payload": [conversational(envelope)]}]}),
    )
    payload = client.get(
        "/api/memory/events", params={"actor_id": "a__r", "session_id": "s"}
    ).json()["items"][0]["payload"][0]
    assert payload["text"] == "Here's what I found"
    assert payload["parts"] == ["text"]


def test_tool_only_envelope_keeps_its_part_kinds(client, configured, monkeypatch):
    """Observability drops tool-only turns from its transcript; a memory
    inspector must not hide a payload that exists — no text, but the kinds stay
    so the UI can render the turn as itself instead of a blank bubble."""
    envelope = json.dumps(
        {
            "message": {
                "role": "assistant",
                "content": [{"toolUse": {"name": "retrieve", "input": {}}}],
            }
        }
    )
    wire(
        monkeypatch,
        StubData(list_events={"events": [{"eventId": "e", "payload": [conversational(envelope)]}]}),
    )
    payload = client.get(
        "/api/memory/events", params={"actor_id": "a__r", "session_id": "s"}
    ).json()["items"][0]["payload"][0]
    assert payload["text"] == ""
    assert payload["parts"] == ["toolUse"]


@pytest.mark.parametrize(
    "text",
    [
        "{not json at all",
        '{"message": {"content": "a string, not a list"}}',
        '{"unrelated": true}',
    ],
)
def test_non_envelope_json_is_shown_verbatim(client, configured, monkeypatch, text):
    """Anything that is not a recognisable envelope is displayed as stored —
    never swallowed."""
    wire(
        monkeypatch,
        StubData(list_events={"events": [{"eventId": "e", "payload": [conversational(text)]}]}),
    )
    payload = client.get(
        "/api/memory/events", params={"actor_id": "a__r", "session_id": "s"}
    ).json()["items"][0]["payload"][0]
    assert payload["text"] == text
    assert payload["parts"] == []


# --------------------------------------------------------------------------- #
# Long-term — namespace resolution, listing, semantic search
# --------------------------------------------------------------------------- #


def test_namespaces_substitute_actor_into_templates(client, configured, monkeypatch):
    wire(monkeypatch)
    items = client.get("/api/memory/namespaces", params={"actor_id": "ag__river"}).json()[
        "items"
    ]
    assert [i["namespace"] for i in items] == ["/facts/ag__river", "/preferences/ag__river"]
    assert all(i["resolvable"] for i in items)


def test_unknown_placeholder_marks_namespace_unresolvable(client, configured, monkeypatch):
    payload = {
        "memory": {
            **MEMORY_PAYLOAD["memory"],
            "strategies": [
                {
                    "strategyId": "s1",
                    "name": "per_session",
                    "type": "SUMMARIZATION",
                    "status": "ACTIVE",
                    "namespaces": [],
                    "namespaceTemplates": ["/summaries/{actorId}/{sessionId}"],
                }
            ],
        }
    }
    wire(monkeypatch, control=StubControl(memory=payload))
    item = client.get("/api/memory/namespaces", params={"actor_id": "ag__river"}).json()[
        "items"
    ][0]
    # {sessionId} is not resolvable from an actor alone — say so instead of
    # sending a broken namespace to AWS
    assert item["resolvable"] is False
    assert item["namespace"] == "/summaries/ag__river/{sessionId}"


def test_records_resolve_namespace_from_actor_and_strategy(client, configured, monkeypatch):
    data, _ = wire(
        monkeypatch,
        StubData(
            list_memory_records={
                "memoryRecordSummaries": [
                    {
                        "memoryRecordId": "r1",
                        "content": {"text": "prefers window seats"},
                        "memoryStrategyId": "strat-prefs",
                        "namespaces": ["/preferences/ag__river"],
                        "createdAt": AT,
                    }
                ]
            }
        ),
    )
    body = client.get(
        "/api/memory/records",
        params={"actor_id": "ag__river", "strategy_id": "strat-prefs"},
    ).json()

    assert body["namespace"] == "/preferences/ag__river"
    assert data.kwargs_for("list_memory_records")["namespacePath"] == "/preferences/ag__river"
    assert data.kwargs_for("list_memory_records")["memoryStrategyId"] == "strat-prefs"
    assert body["items"][0]["text"] == "prefers window seats"
    assert body["items"][0]["created_at"] == AT.isoformat()


def test_structured_preference_record_is_made_readable(client, configured, monkeypatch):
    """USER_PREFERENCE strategies store a JSON object in content.text while
    SEMANTIC stores prose. Rendering the object verbatim is unreadable, so the
    display field is extracted — without losing the original payload."""
    stored = json.dumps(
        {
            "context": "The user explicitly stated they want numbered lists.",
            "preference": "Always wants answers formatted as a numbered list",
            "categories": ["formatting", "communication"],
        }
    )
    wire(
        monkeypatch,
        StubData(
            list_memory_records={
                "memoryRecordSummaries": [
                    {"memoryRecordId": "r1", "content": {"text": stored}}
                ]
            }
        ),
    )
    item = client.get("/api/memory/records", params={"namespace": "/preferences/a"}).json()[
        "items"
    ][0]
    assert item["text"] == "Always wants answers formatted as a numbered list"
    assert item["structured"]["categories"] == ["formatting", "communication"]
    assert item["raw_text"] == stored  # original never discarded


def test_prose_record_passes_through_unstructured(client, configured, monkeypatch):
    wire(
        monkeypatch,
        StubData(
            list_memory_records={
                "memoryRecordSummaries": [
                    {"memoryRecordId": "r1", "content": {"text": "Favourite planet is Saturn."}}
                ]
            }
        ),
    )
    item = client.get("/api/memory/records", params={"namespace": "/facts/a"}).json()["items"][0]
    assert item["text"] == "Favourite planet is Saturn."
    assert item["structured"] is None


def test_structured_record_without_a_display_field_keeps_raw_json(
    client, configured, monkeypatch
):
    """No known display key: show the payload rather than invent a summary, but
    still expose the parsed fields."""
    stored = json.dumps({"unexpected": {"nested": 1}})
    wire(
        monkeypatch,
        StubData(
            list_memory_records={
                "memoryRecordSummaries": [{"memoryRecordId": "r", "content": {"text": stored}}]
            }
        ),
    )
    item = client.get("/api/memory/records", params={"namespace": "/x"}).json()["items"][0]
    assert item["text"] == stored
    assert item["structured"] == {"unexpected": {"nested": 1}}


def test_explicit_namespace_wins_over_derivation(client, configured, monkeypatch):
    data, _ = wire(monkeypatch, StubData(list_memory_records={}))
    body = client.get("/api/memory/records", params={"namespace": "/custom/ns"}).json()
    assert body["namespace"] == "/custom/ns"
    assert data.kwargs_for("list_memory_records")["namespacePath"] == "/custom/ns"


def test_records_without_derivable_namespace_is_a_400(client, configured, monkeypatch):
    wire(monkeypatch)
    res = client.get("/api/memory/records")
    assert res.status_code == 400
    assert res.json()["code"] == "memory.namespace_required"


def test_records_reject_unknown_strategy_rather_than_guessing(client, configured, monkeypatch):
    wire(monkeypatch)
    res = client.get(
        "/api/memory/records", params={"actor_id": "ag__river", "strategy_id": "nope"}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "memory.namespace_required"


def test_semantic_search_passes_criteria_and_surfaces_scores(client, configured, monkeypatch):
    data, _ = wire(
        monkeypatch,
        StubData(
            retrieve_memory_records={
                "memoryRecordSummaries": [
                    {
                        "memoryRecordId": "r9",
                        "content": {"text": "allergic to shellfish"},
                        "memoryStrategyId": "strat-facts",
                        "score": 0.87,
                    }
                ]
            }
        ),
    )
    body = client.post(
        "/api/memory/records/search",
        json={
            "query": "dietary restrictions",
            "actor_id": "ag__river",
            "strategy_id": "strat-facts",
            "top_k": 4,
        },
    ).json()

    kw = data.kwargs_for("retrieve_memory_records")
    assert kw["namespace"] == "/facts/ag__river"
    assert kw["searchCriteria"] == {
        "searchQuery": "dietary restrictions",
        "topK": 4,
        "memoryStrategyId": "strat-facts",
    }
    assert body["query"] == "dietary restrictions"
    assert body["items"][0]["score"] == 0.87


def test_search_rejects_empty_query(client, configured, monkeypatch):
    wire(monkeypatch)
    res = client.post(
        "/api/memory/records/search", json={"query": "", "actor_id": "ag__river"}
    )
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Extraction jobs
# --------------------------------------------------------------------------- #


def test_extraction_jobs_omit_empty_filters(client, configured, monkeypatch):
    """The preview API rejects empty strings inside `filter` — send only real ones."""
    data, _ = wire(
        monkeypatch,
        StubData(
            list_memory_extraction_jobs={
                "jobs": [
                    {
                        "jobID": "job-1",
                        "status": "SUCCEEDED",
                        "strategyId": "strat-facts",
                        "actorId": "ag__river",
                        "sessionId": "s1",
                        "messages": {"messagesList": ["extracted 2 records"]},
                    }
                ]
            }
        ),
    )
    body = client.get(
        "/api/memory/extraction-jobs", params={"actor_id": "ag__river", "status": ""}
    ).json()

    assert data.kwargs_for("list_memory_extraction_jobs")["filter"] == {
        "actorId": "ag__river"
    }
    assert body["items"][0]["job_id"] == "job-1"
    assert body["items"][0]["messages"] == ["extracted 2 records"]


def test_extraction_jobs_send_no_filter_key_when_unfiltered(client, configured, monkeypatch):
    data, _ = wire(monkeypatch, StubData(list_memory_extraction_jobs={"jobs": []}))
    client.get("/api/memory/extraction-jobs")
    assert "filter" not in data.kwargs_for("list_memory_extraction_jobs")


def test_unsupported_status_filter_is_a_typed_400(client, configured, monkeypatch):
    """The API's status filter accepts only FAILED. Anything else must fail as an
    actionable 400 here, not as a 502-shaped AWS ValidationException."""
    data, _ = wire(monkeypatch, StubData(list_memory_extraction_jobs={"jobs": []}))
    res = client.get("/api/memory/extraction-jobs", params={"status": "SUCCEEDED"})
    assert res.status_code == 400
    assert res.json()["code"] == "memory.invalid_status_filter"
    assert res.json()["detail"] == {"allowed": ["FAILED"]}
    assert data.calls == []  # rejected before reaching AWS


def test_supported_status_filter_reaches_aws(client, configured, monkeypatch):
    data, _ = wire(monkeypatch, StubData(list_memory_extraction_jobs={"jobs": []}))
    client.get("/api/memory/extraction-jobs", params={"status": "FAILED"})
    assert data.kwargs_for("list_memory_extraction_jobs")["filter"] == {"status": "FAILED"}


def test_extraction_job_messages_degrade_on_shape_drift(client, configured, monkeypatch):
    """`messages` is preview-volatile: an unexpected shape must not 500 the page."""
    wire(
        monkeypatch,
        StubData(
            list_memory_extraction_jobs={
                "jobs": [{"jobID": "j", "status": "FAILED", "messages": "oops"}]
            }
        ),
    )
    body = client.get("/api/memory/extraction-jobs").json()
    assert body["items"][0]["messages"] == []


# --------------------------------------------------------------------------- #
# Pagination + failure mapping + read-only stance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "path,params,op",
    [
        ("/api/memory/actors", {}, "list_actors"),
        ("/api/memory/sessions", {"actor_id": "a__r"}, "list_sessions"),
        (
            "/api/memory/events",
            {"actor_id": "a__r", "session_id": "s"},
            "list_events",
        ),
        ("/api/memory/records", {"namespace": "/facts/a"}, "list_memory_records"),
        ("/api/memory/extraction-jobs", {}, "list_memory_extraction_jobs"),
    ],
)
def test_next_token_round_trips(client, configured, monkeypatch, path, params, op):
    data, _ = wire(monkeypatch, StubData(**{op: {"nextToken": "page-2"}}))
    body = client.get(path, params={**params, "next_token": "page-1"}).json()
    assert data.kwargs_for(op)["nextToken"] == "page-1"  # request token forwarded
    assert body["next_token"] == "page-2"  # response token exposed


def test_max_results_capped_at_the_aws_page_limit(client, configured, monkeypatch):
    data, _ = wire(monkeypatch, StubData(list_actors={"actorSummaries": []}))
    client.get("/api/memory/actors", params={"max_results": 100})
    assert data.kwargs_for("list_actors")["maxResults"] == mc.PAGE_MAX


def test_extraction_jobs_use_the_lower_page_cap(client, configured, monkeypatch):
    """ListMemoryExtractionJobs rejects maxResults > 50 with a
    ValidationException — a bound the botocore service model does not declare,
    so a shared 100 default would 502 every unfiltered request."""
    data, _ = wire(monkeypatch, StubData(list_memory_extraction_jobs={"jobs": []}))
    client.get("/api/memory/extraction-jobs", params={"max_results": 100})
    assert data.kwargs_for("list_memory_extraction_jobs")["maxResults"] == 50
    assert mc.EXTRACTION_PAGE_MAX == 50


def test_aws_failure_becomes_a_502_envelope(client, configured, monkeypatch):
    class Boom(StubData):
        def list_actors(self, **kw):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
                "ListActors",
            )

    wire(monkeypatch, Boom())
    res = client.get("/api/memory/actors")
    assert res.status_code == 502
    assert res.json()["code"] == "memory.unavailable"


def test_console_exposes_no_memory_mutation():
    """Read-only is structural: no mutating handler exists to be reached."""
    import app.routers.memory as memory_router

    forbidden = {
        "create_event",
        "delete_event",
        "delete_memory_record",
        "batch_create_memory_records",
        "batch_update_memory_records",
        "batch_delete_memory_records",
        "start_memory_extraction_job",
        "create_memory",
        "update_memory",
        "delete_memory",
    }
    for module in (mc, memory_router):
        source = inspect.getsource(module)
        assert not (forbidden & set(source.split())), module.__name__

    methods = {
        method
        for route in memory_router.router.routes
        for method in getattr(route, "methods", set())
    }
    # the only non-GET is the retrieval search, which carries its query in a body
    assert methods <= {"GET", "POST"}
    assert not {"PUT", "PATCH", "DELETE"} & methods
