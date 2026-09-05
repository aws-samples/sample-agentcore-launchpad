"""Memory resource management API — hermetic contract tests.

AWS is stubbed at the client-factory boundary (``memory_admin.control_client``),
mirroring ``test_memory_console``. These pin the lifecycle surface the console's
resource module depends on: list marks the workspace default, create maps the
strategy picks onto the CreateMemory shape, update sends UpdateMemory exactly
``memoryId`` + the changed fields (never ``namespaceKeys``, which the API
replaces wholesale) and reads the detail back, and delete is guarded twice — the
bootstrap memory is protected, and a memory a live agent's spec pins refuses
deletion.

The read-only stance of the *console* router is untouched: mutations live in
``routers/memory_resources`` / ``services/memory_admin`` only (see
``test_memory_console.test_console_exposes_no_memory_mutation``).
"""

from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

import app.services.memory_admin as ma
from app.core.db import SessionLocal
from app.models.ledger import Agent

from .conftest import set_default_resources

MEM_ID = "launchpad_memory-ABC123"
AT = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


class StubControl:
    """Records every call so tests can assert the exact AWS request shape."""

    def __init__(self, memories=None, memory=None, fail=None):
        self.memories = memories if memories is not None else [
            {"id": MEM_ID, "arn": "arn:mem:1", "status": "ACTIVE", "createdAt": AT},
            {"id": "team_notes-XYZ789", "arn": "arn:mem:2", "status": "CREATING"},
        ]
        self.memory = memory or {
            "memory": {
                "id": MEM_ID,
                "arn": "arn:mem:1",
                "name": "launchpad_memory",
                "status": "ACTIVE",
                "eventExpiryDuration": 30,
                "memoryExecutionRoleArn": "arn:aws:iam::1:role/launchpad-memory",
                "strategies": [
                    {
                        "strategyId": "strat-facts",
                        "name": "semantic_facts",
                        "type": "SEMANTIC",
                        "status": "ACTIVE",
                        "namespaces": ["/facts/{actorId}"],
                    }
                ],
            }
        }
        self.fail = fail
        self.calls: list[tuple[str, dict]] = []

    def _reply(self, op, kw, value):
        self.calls.append((op, kw))
        if self.fail == op:
            raise RuntimeError("aws exploded")
        return value

    def list_memories(self, **kw):
        return self._reply("list_memories", kw, {"memories": self.memories})

    def get_memory(self, **kw):
        return self._reply("get_memory", kw, self.memory)

    def create_memory(self, **kw):
        return self._reply(
            "create_memory",
            kw,
            {"memory": {"id": f"{kw['name']}-NEW00001", "status": "CREATING", **kw}},
        )

    def update_memory(self, **kw):
        # the real service applies the change; mirror that so the GetMemory
        # readback the route relies on reflects it (and bumps updatedAt)
        memory = self.memory["memory"]
        if "description" in kw:
            memory["description"] = kw["description"]
        if "eventExpiryDuration" in kw:
            memory["eventExpiryDuration"] = kw["eventExpiryDuration"]
        memory["updatedAt"] = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)
        return self._reply("update_memory", kw, {"memory": dict(memory)})

    def delete_memory(self, **kw):
        return self._reply("delete_memory", kw, {"memoryId": kw["memoryId"]})

    def kwargs_for(self, op: str) -> dict:
        return next(kw for name, kw in self.calls if name == op)


@pytest.fixture
def configured(client):
    set_default_resources({"memory_id": MEM_ID})


def wire(monkeypatch, control=None):
    control = control or StubControl()
    monkeypatch.setattr(ma, "control_client", lambda _ws=None: control)
    return control


def make_agent(name="mem-pinned-agent", memory_id=None) -> str:
    db = SessionLocal()
    agent = Agent(
        workspace_id="default", name=name, method="zip_runtime", status="active",
        spec={"name": name, "memory": {"short_term": True, "memory_id": memory_id}},
    )
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    return agent_id


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #


def test_list_marks_default_and_derives_names(client, configured, monkeypatch):
    wire(monkeypatch)
    body = client.get("/api/memory/resources").json()

    assert body["default_id"] == MEM_ID
    by_id = {m["id"]: m for m in body["items"]}
    assert by_id[MEM_ID]["is_default"] is True
    assert by_id[MEM_ID]["name"] == "launchpad_memory"  # ListMemories has no name
    assert by_id["team_notes-XYZ789"]["is_default"] is False
    assert by_id["team_notes-XYZ789"]["name"] == "team_notes"
    # default sorts first for the console table
    assert body["items"][0]["id"] == MEM_ID


def test_list_annotates_agents_pinning_each_memory(client, configured, monkeypatch):
    wire(monkeypatch)
    agent_id = make_agent(memory_id="team_notes-XYZ789")
    body = client.get("/api/memory/resources").json()
    by_id = {m["id"]: m for m in body["items"]}
    assert by_id["team_notes-XYZ789"]["agents"] == [
        {"id": agent_id, "name": "mem-pinned-agent"}
    ]
    assert by_id[MEM_ID]["agents"] == []  # default users carry no explicit pin


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def test_create_maps_strategy_picks_onto_the_aws_shape(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.post(
        "/api/memory/resources",
        json={
            "name": "team_notes",
            "description": "per-team memory",
            "event_expiry_days": 14,
            "strategies": ["semantic", "user_preference"],
        },
    )
    assert res.status_code == 201
    assert res.json()["id"] == "team_notes-NEW00001"
    assert res.json()["status"] == "CREATING"

    kw = control.kwargs_for("create_memory")
    assert kw["name"] == "team_notes"
    assert kw["eventExpiryDuration"] == 14
    strategy_keys = [next(iter(s)) for s in kw["memoryStrategies"]]
    assert strategy_keys == ["semanticMemoryStrategy", "userPreferenceMemoryStrategy"]
    # namespaces mirror the bootstrap memory so scoping + console reads work
    assert kw["memoryStrategies"][0]["semanticMemoryStrategy"]["namespaces"] == [
        "/facts/{actorId}"
    ]
    # long-term extraction reuses the bootstrap memory's execution role
    assert kw["memoryExecutionRoleArn"] == "arn:aws:iam::1:role/launchpad-memory"


def test_create_without_strategies_skips_the_role_lookup(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.post(
        "/api/memory/resources", json={"name": "events_only", "strategies": []}
    )
    assert res.status_code == 201
    kw = control.kwargs_for("create_memory")
    assert "memoryStrategies" not in kw
    assert "memoryExecutionRoleArn" not in kw
    assert not any(op == "get_memory" for op, _ in control.calls)


def test_create_rejects_an_invalid_name_before_aws(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.post("/api/memory/resources", json={"name": "bad-name!"})
    assert res.status_code == 422  # CreateMemory names allow [a-zA-Z0-9_] only
    assert control.calls == []


def test_create_maps_episodic_with_its_reflection_config(client, configured, monkeypatch):
    """Episodic is the fourth built-in: episodes per session, reflections on the
    per-actor prefix — the live API rejects any reflection namespace that is not
    a hierarchical prefix of the episode namespace (found against real AWS)."""
    control = wire(monkeypatch)
    res = client.post(
        "/api/memory/resources",
        json={"name": "episodes_mem", "strategies": ["episodic"]},
    )
    assert res.status_code == 201
    kw = control.kwargs_for("create_memory")
    episodic = kw["memoryStrategies"][0]["episodicMemoryStrategy"]
    assert episodic["namespaces"] == ["/episodes/{actorId}/{sessionId}"]
    assert episodic["reflectionConfiguration"] == {
        "namespaceTemplates": ["/episodes/{actorId}"]
    }


def test_create_rejects_unknown_strategies(client, configured, monkeypatch):
    wire(monkeypatch)
    res = client.post(
        "/api/memory/resources", json={"name": "ok_name", "strategies": ["telepathic"]}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "memory.invalid_strategy"


def test_create_maps_namespace_keys_onto_the_aws_shape(client, configured, monkeypatch):
    """Flexible namespace variables: keys + optional validation rules become
    CreateMemory ``namespaceKeys``; the created detail echoes them back."""
    control = wire(monkeypatch)
    res = client.post(
        "/api/memory/resources",
        json={
            "name": "tenant_mem",
            "strategies": [],
            "namespace_keys": [
                {"key": "orgname", "allowed_values": ["acme", "globex"]},
                {"key": "teamname", "regex_pattern": "^[a-z][a-z0-9-]*$"},
                {"key": "env"},
            ],
        },
    )
    assert res.status_code == 201
    kw = control.kwargs_for("create_memory")
    assert kw["namespaceKeys"] == [
        {"key": "orgname", "validation": {"allowedValues": ["acme", "globex"]}},
        {"key": "teamname", "validation": {"regexPattern": "^[a-z][a-z0-9-]*$"}},
        {"key": "env"},  # no rules → no validation object at all
    ]
    # the console shape round-trips the definitions
    assert res.json()["namespace_keys"] == [
        {"key": "orgname", "allowed_values": ["acme", "globex"], "regex_pattern": None},
        {"key": "teamname", "allowed_values": None, "regex_pattern": "^[a-z][a-z0-9-]*$"},
        {"key": "env", "allowed_values": None, "regex_pattern": None},
    ]


def test_create_omits_namespace_keys_when_none_are_defined(
    client, configured, monkeypatch
):
    control = wire(monkeypatch)
    res = client.post("/api/memory/resources", json={"name": "plain_mem"})
    assert res.status_code == 201
    assert "namespaceKeys" not in control.kwargs_for("create_memory")


@pytest.mark.parametrize(
    "keys",
    [
        [{"key": "OrgName"}],  # keys must be lowercase alphanumeric
        [{"key": "actorid" * 5}],  # > 32 chars
        [{"key": "org"}, {"key": "org"}],  # duplicates
        [{"key": f"k{i}"} for i in range(6)],  # > 5 keys per resource
        [{"key": "org", "allowed_values": ["Acme!"]}],  # bad value charset
        [{"key": "org", "regex_pattern": "x" * 65}],  # regex > 64 chars
    ],
)
def test_create_rejects_invalid_namespace_keys_before_aws(
    client, configured, monkeypatch, keys
):
    control = wire(monkeypatch)
    res = client.post(
        "/api/memory/resources", json={"name": "ok_name", "namespace_keys": keys}
    )
    assert res.status_code == 422
    assert control.calls == []


# --------------------------------------------------------------------------- #
# Update
# --------------------------------------------------------------------------- #

UPDATE_URL = f"/api/memory/resources/{MEM_ID}"


def test_update_description_only_sends_exactly_memory_id_and_description(
    client, configured, monkeypatch
):
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={"description": "shared HR memory"})
    assert res.status_code == 200
    kw = control.kwargs_for("update_memory")
    # the exact key set: no namespaceKeys (the API replaces the set wholesale —
    # any key omitted is REMOVED), no memoryStrategies, no execution role
    assert set(kw) == {"memoryId", "description"}
    assert kw == {"memoryId": MEM_ID, "description": "shared HR memory"}


def test_update_expiry_only_sends_exactly_memory_id_and_expiry(
    client, configured, monkeypatch
):
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={"event_expiry_days": 90})
    assert res.status_code == 200
    kw = control.kwargs_for("update_memory")
    assert set(kw) == {"memoryId", "eventExpiryDuration"}
    assert kw["eventExpiryDuration"] == 90


def test_update_both_fields_and_returns_the_refreshed_detail(
    client, configured, monkeypatch
):
    """The reply is the GetMemory readback *after* UpdateMemory — the same
    projection as ``GET /api/memory/resources/{id}`` — not the update echo."""
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={"description": "renamed", "event_expiry_days": 7})
    assert res.status_code == 200

    kw = control.kwargs_for("update_memory")
    assert set(kw) <= {"memoryId", "description", "eventExpiryDuration", "clientToken"}
    assert set(kw) == {"memoryId", "description", "eventExpiryDuration"}
    assert "namespaceKeys" not in kw and "memoryStrategies" not in kw

    ops = [op for op, _ in control.calls]
    assert ops == ["update_memory", "get_memory"]
    assert control.kwargs_for("get_memory") == {"memoryId": MEM_ID}

    body = res.json()
    assert body["id"] == MEM_ID
    assert body["description"] == "renamed"
    assert body["event_expiry_days"] == 7
    assert body["updated_at"] == "2026-09-05T09:30:00+00:00"
    assert body["is_default"] is True
    # same shape as the GET detail route
    assert body == client.get(UPDATE_URL).json()


@pytest.mark.parametrize("days", [6, 366])
def test_update_rejects_expiry_outside_7_to_365_before_aws(
    client, configured, monkeypatch, days
):
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={"event_expiry_days": days})
    assert res.status_code == 422
    assert control.calls == []


def test_update_requires_at_least_one_field(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={})
    assert res.status_code == 422
    assert control.calls == []


def test_update_rejects_an_empty_description(client, configured, monkeypatch):
    """UpdateMemory's description shape is 1–4096 chars: it can be replaced,
    never cleared — surfaced as a 422 rather than a mid-request ValidationException."""
    control = wire(monkeypatch)
    res = client.put(UPDATE_URL, json={"description": ""})
    assert res.status_code == 422
    assert control.calls == []


def test_update_unknown_memory_maps_to_the_not_found_envelope(
    client, configured, monkeypatch
):
    class Missing(StubControl):
        def update_memory(self, **kw):
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "no such memory"}},
                "UpdateMemory",
            )

    wire(monkeypatch, Missing())
    res = client.put("/api/memory/resources/ghost-000000", json={"description": "x"})
    assert res.status_code == 404
    assert res.json()["code"] == "aws.not_found"


def test_update_is_not_blocked_by_referencing_agents_or_the_default(
    client, configured, monkeypatch
):
    """Unlike delete, edit has no 409 guard: a new description or expiry cannot
    break the agents on the memory (the console names them in its confirm)."""
    control = wire(monkeypatch)
    make_agent(memory_id="team_notes-XYZ789")
    control.memory["memory"]["id"] = "team_notes-XYZ789"
    res = client.put(
        "/api/memory/resources/team_notes-XYZ789", json={"event_expiry_days": 14}
    )
    assert res.status_code == 200
    assert control.kwargs_for("update_memory") == {
        "memoryId": "team_notes-XYZ789",
        "eventExpiryDuration": 14,
    }
    # the platform default is editable too (only delete protects it)
    control.memory["memory"]["id"] = MEM_ID
    assert client.put(UPDATE_URL, json={"description": "default memory"}).status_code == 200


def test_update_aws_failure_stays_the_memory_unavailable_envelope(
    client, configured, monkeypatch
):
    wire(monkeypatch, StubControl(fail="update_memory"))
    res = client.put(UPDATE_URL, json={"description": "x"})
    assert res.status_code == 502
    assert res.json()["code"] == "memory.unavailable"


# --------------------------------------------------------------------------- #
# Delete
# --------------------------------------------------------------------------- #


def test_delete_refuses_the_platform_default(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.delete(f"/api/memory/resources/{MEM_ID}")
    assert res.status_code == 409
    assert res.json()["code"] == "memory.platform_protected"
    assert not any(op == "delete_memory" for op, _ in control.calls)


def test_delete_refuses_a_memory_a_live_agent_pins(client, configured, monkeypatch):
    control = wire(monkeypatch)
    make_agent(memory_id="team_notes-XYZ789")
    res = client.delete("/api/memory/resources/team_notes-XYZ789")
    assert res.status_code == 409
    assert res.json()["code"] == "memory.in_use"
    assert not any(op == "delete_memory" for op, _ in control.calls)


def test_delete_removes_an_unreferenced_memory(client, configured, monkeypatch):
    control = wire(monkeypatch)
    res = client.delete("/api/memory/resources/team_notes-XYZ789")
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "id": "team_notes-XYZ789"}
    assert control.kwargs_for("delete_memory") == {"memoryId": "team_notes-XYZ789"}


# --------------------------------------------------------------------------- #
# Failure mapping
# --------------------------------------------------------------------------- #


def test_aws_failure_maps_to_the_memory_unavailable_envelope(
    client, configured, monkeypatch
):
    wire(monkeypatch, StubControl(fail="list_memories"))
    res = client.get("/api/memory/resources")
    assert res.status_code == 502
    assert res.json()["code"] == "memory.unavailable"
