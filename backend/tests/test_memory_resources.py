"""Memory resource management API — hermetic contract tests.

AWS is stubbed at the client-factory boundary (``memory_admin.control_client``),
mirroring ``test_memory_console``. These pin the lifecycle surface the console's
resource module depends on: list marks the workspace default, create maps the
strategy picks onto the CreateMemory shape, and delete is guarded twice — the
bootstrap memory is protected, and a memory a live agent's spec pins refuses
deletion.

The read-only stance of the *console* router is untouched: mutations live in
``routers/memory_resources`` / ``services/memory_admin`` only (see
``test_memory_console.test_console_exposes_no_memory_mutation``).
"""

from datetime import UTC, datetime

import pytest

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
