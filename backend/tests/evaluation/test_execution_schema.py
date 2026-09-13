"""Schema + bounds of ``metadata.launchpad_execution`` (SE-046).

Every malformed shape must be refused at dataset create / upload / update and at
run create (cloud items), before any run row exists or any AWS call is made.
Scenarios without the key keep their old wire behaviour exactly.
"""

import copy
import json
import re

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import execution as ex
from app.evaluation.models import EvalDataset

# Synthetic fixture: actor A seeds a marker in a1, recalls it in a fresh session
# a2; actor B must not learn it. No real conversation content anywhere.
ISOLATION_SCENARIO = {
    "scenario_id": "gt_isolation",
    "turns": [
        {"input": "My favourite colour is amber. Please remember it."},
        {"input": "What is my favourite colour?", "expected_response": "amber"},
        {"input": "What is my favourite colour?"},
    ],
    "assertions": ["The agent never tells actor B what actor A said."],
    "metadata": {
        "launchpad_execution": {
            "version": 1,
            "repeat": 2,
            "steps": [
                {"turn": 0, "actor": "A", "session": "a1"},
                {"turn": 1, "actor": "A", "session": "a2"},
                {"turn": 2, "actor": "B", "session": "b1"},
            ],
            "checks": [
                {"id": "seed", "type": "contains", "turn": 0, "text": "amber"},
                {"id": "recall", "type": "contains", "turn": 1, "text": "amber",
                 "depends_on": ["seed"]},
                {"id": "no_leak", "type": "not_contains", "turn": 2, "text": "amber",
                 "depends_on": ["seed"]},
            ],
        }
    },
}


def scenario(**patch):
    item = copy.deepcopy(ISOLATION_SCENARIO)
    block = item["metadata"]["launchpad_execution"]
    for key, value in patch.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return item


def test_parse_plan_happy_path():
    plan = ex.parse_plan(ISOLATION_SCENARIO)
    assert plan.repeat == 2
    assert plan.actor_aliases == ["A", "B"]
    assert plan.session_keys == [("A", "a1"), ("A", "a2"), ("B", "b1")]
    assert plan.expanded_calls == 6 and plan.expanded_sessions == 6
    assert [c.id for c in plan.checks] == ["seed", "recall", "no_leak"]
    assert plan.checks[2].depends_on == ("seed",)


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"version": 2}, "version must be the integer 1"),
        ({"version": None}, "version must be the integer 1"),
        ({"version": True}, "version must be the integer 1"),
        ({"version": 1.0}, "version must be the integer 1"),
        ({"version": "1"}, "version must be the integer 1"),
        # alias with a trailing newline (re.match + $ used to accept it)
        ({"steps": [{"turn": 0, "actor": "A\n", "session": "a1"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "must be an alias"),
        ({"repeat": 0}, "repeat must be an integer 1..5"),
        ({"repeat": 6}, "repeat must be an integer 1..5"),
        ({"repeat": True}, "repeat must be an integer 1..5"),
        ({"repeat": "2"}, "repeat must be an integer 1..5"),
        ({"extra": 1}, "unknown key(s): extra"),
        ({"steps": None}, "steps must be a non-empty list"),
        ({"steps": []}, "steps must be a non-empty list"),
        # missing turn 2
        ({"steps": [{"turn": 0, "actor": "A", "session": "a1"},
                    {"turn": 1, "actor": "A", "session": "a2"}]}, "cover every turn"),
        # duplicate turn
        ({"steps": [{"turn": 0, "actor": "A", "session": "a1"},
                    {"turn": 0, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "repeats turn 0"),
        # out of range
        ({"steps": [{"turn": 0, "actor": "A", "session": "a1"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 3, "actor": "B", "session": "b1"}]}, "out of range"),
        # bad alias
        ({"steps": [{"turn": 0, "actor": "A B", "session": "a1"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "must be an alias"),
        # raw-id-looking alias (too long)
        ({"steps": [{"turn": 0, "actor": "a" * 33, "session": "a1"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "must be an alias"),
        # unknown step key
        ({"steps": [{"turn": 0, "actor": "A", "session": "a1", "actor_id": "x"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "unknown key(s): actor_id"),
        # missing step key
        ({"steps": [{"turn": 0, "actor": "A"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "b1"}]}, "missing session"),
        # one session alias shared by two actors
        ({"steps": [{"turn": 0, "actor": "A", "session": "s"},
                    {"turn": 1, "actor": "A", "session": "a2"},
                    {"turn": 2, "actor": "B", "session": "s"}]}, "belongs to exactly one actor"),
        ({"checks": "nope"}, "checks must be a list"),
        ({"checks": [{"id": "x", "type": "regex", "turn": 0, "text": "a"}]}, "type must be one of"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 9, "text": "a"}]}, "out of range"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": ""}]}, "non-empty string"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a" * 2001}]},
         "exceeds 2000"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a", "eval": "1"}]},
         "unknown key(s): eval"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a"},
                     {"id": "x", "type": "contains", "turn": 0, "text": "b"}]}, "duplicates id"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a",
                      "depends_on": ["later"]},
                     {"id": "later", "type": "contains", "turn": 0, "text": "b"}]},
         "not declared earlier"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a",
                      "depends_on": ["x"]}]}, "depends on itself"),
        ({"checks": [{"id": "x", "type": "contains", "turn": 0, "text": "a",
                      "case_sensitive": "yes"}]}, "case_sensitive must be a boolean"),
    ],
)
def test_parse_plan_rejects_malformed_blocks(patch, fragment):
    with pytest.raises(ex.ExecutionError, match=re.escape(fragment)):
        ex.parse_plan(scenario(**patch))


def test_parse_plan_rejects_persona_and_legacy_items():
    persona = {
        "scenario_id": "p", "input": "hi",
        "actor_profile": {"context": "c", "goal": "g"},
        "metadata": {"launchpad_execution": {"version": 1, "steps": []}},
    }
    with pytest.raises(ex.ExecutionError, match="simulated persona"):
        ex.parse_plan(persona)
    legacy = {"prompt": "hi", "metadata": {"launchpad_execution": {"version": 1}}}
    with pytest.raises(ex.ExecutionError, match="requires a predefined scenario"):
        ex.parse_plan(legacy)


def test_alias_and_session_caps():
    turns = [{"input": f"t{i}"} for i in range(11)]
    steps = [{"turn": i, "actor": f"A{i}", "session": f"s{i}"} for i in range(11)]
    item = {"scenario_id": "many", "turns": turns,
            "metadata": {"launchpad_execution": {"version": 1, "steps": steps}}}
    with pytest.raises(ex.ExecutionError, match="more than 10 actor aliases"):
        ex.parse_plan(item)


def test_validate_items_caps_expanded_calls_and_sessions():
    # 3 turns × repeat 5 = 15 calls per scenario; 14 scenarios = 210 > 200
    items = [dict(scenario(repeat=5), scenario_id=f"s{i}") for i in range(14)]
    with pytest.raises(AppError) as exc:
        ex.validate_items(items)
    assert exc.value.code == "dataset.execution_limits"
    assert "210 agent invocations" in exc.value.message
    # plain scenarios count as one session / their turn count
    ex.validate_items([dict(scenario(repeat=5), scenario_id=f"s{i}") for i in range(13)]
                      + [{"prompt": "x"}] * 5)


def test_validate_items_without_extension_is_a_noop():
    ex.validate_items([{"prompt": "x"}, {"scenario_id": "a", "turns": [{"input": "y"}]}])
    assert not ex.any_executable([{"scenario_id": "a", "turns": [{"input": "y"}],
                                   "metadata": {"tags": ["x"]}}])


def test_synthetic_actor_is_scoped_and_stable():
    kw = dict(workspace_id="default", agent_id="a" * 32, run_id="run123456789",
              scenario_id="gt_isolation", repeat=1, alias="A")
    a1 = ex.synthetic_actor(**kw)
    assert a1 == ex.synthetic_actor(**kw)  # same alias, same repeat → same identity
    assert a1.startswith("a" * 32 + "__eval__")  # memory.scoped_actor convention
    assert a1 != ex.synthetic_actor(**{**kw, "alias": "B"})
    assert a1 != ex.synthetic_actor(**{**kw, "repeat": 2})
    assert a1 != ex.synthetic_actor(**{**kw, "run_id": "otherrun00000"})
    assert a1 != ex.synthetic_actor(**{**kw, "workspace_id": "spoke-use1"})
    weird = ex.synthetic_actor(**{**kw, "scenario_id": "GT 008 / 会话 隔离!"})
    assert re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9-_/]*", weird) and len(weird) <= 255


# ─── API surfaces ────────────────────────────────────────────────────────────
def test_dataset_create_accepts_and_preserves_extension(client):
    res = client.post("/api/eval/datasets", json={"name": "iso", "items": [ISOLATION_SCENARIO]})
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["kind"] == "predefined"
    assert body["items"][0]["metadata"] == ISOLATION_SCENARIO["metadata"]
    # round-trip through update keeps it too
    upd = client.put(f"/api/eval/datasets/{body['id']}", json={"items": [ISOLATION_SCENARIO]})
    assert upd.status_code == 200
    assert upd.json()["items"][0]["metadata"]["launchpad_execution"]["repeat"] == 2


@pytest.mark.parametrize("route", ["create", "upload", "update"])
def test_dataset_routes_reject_malformed_extension(client, route):
    bad = scenario(repeat=9)
    if route == "create":
        res = client.post("/api/eval/datasets", json={"name": "bad", "items": [bad]})
    elif route == "upload":
        res = client.post("/api/eval/datasets/upload",
                          json={"name": "bad", "jsonl": json.dumps(bad)})
    else:
        ok = client.post("/api/eval/datasets", json={"name": "ok", "items": [ISOLATION_SCENARIO]})
        res = client.put(f"/api/eval/datasets/{ok.json()['id']}", json={"items": [bad]})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "dataset.invalid_execution"
    assert "repeat must be an integer 1..5" in res.json()["message"]


def test_dataset_rejects_top_level_key_and_persona_mixing(client):
    top = {**copy.deepcopy(ISOLATION_SCENARIO), "launchpad_execution": {"version": 1}}
    res = client.post("/api/eval/datasets", json={"name": "top", "items": [top]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_execution"
    persona = {
        "scenario_id": "p", "input": "hi",
        "actor_profile": {"context": "c", "goal": "g"},
        "metadata": {"launchpad_execution": {"version": 1, "steps": []}},
    }
    res = client.post("/api/eval/datasets", json={"name": "mix", "items": [persona]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_execution"


def test_dataset_rejects_expanded_limits(client):
    items = [dict(scenario(repeat=5), scenario_id=f"s{i}") for i in range(14)]
    res = client.post("/api/eval/datasets", json={"name": "big", "items": items})
    assert res.status_code == 422 and res.json()["code"] == "dataset.execution_limits"


def test_run_refuses_a2a_agent_before_any_aws_call(client, monkeypatch):
    from unittest.mock import MagicMock

    import app.evaluation.service as svc
    from tests.evaluation.test_runs_flow import make_agent

    db = SessionLocal()
    agent = make_agent(db, name="a2a-agent")
    agent.spec = {"name": "a2a-agent", "protocol": "a2a"}
    db.commit()
    ds = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="iso",
                     kind="predefined", items=[ISOLATION_SCENARIO])
    db.add(ds)
    db.commit()
    ds_id, agent_id = ds.id, agent.id
    db.close()
    submit = MagicMock()
    monkeypatch.setattr(svc, "submit_run", submit)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds_id,
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "run.execution_unsupported_protocol"
    submit.assert_not_called()
    assert client.get("/api/eval/runs").json()["total"] == 0


def test_run_on_cloud_dataset_validates_extension_before_run_row(client, monkeypatch):
    from unittest.mock import MagicMock

    import app.evaluation.routers as routers
    from tests.evaluation.test_runs_flow import make_agent

    db = SessionLocal()
    agent = make_agent(db, name="cloud-agent")
    agent_id = agent.id
    db.close()
    control = MagicMock()
    control.get_dataset.return_value = {
        "datasetId": "ds-cloud", "datasetName": "iso-cloud", "status": "ACTIVE",
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    }
    control.list_dataset_examples.return_value = {
        "examples": [{"exampleId": "e1", **scenario(repeat=7)}]
    }
    monkeypatch.setattr(routers, "control_client", lambda _ws=None: control)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent_id, "cloud_dataset_id": "ds-cloud",
        "evaluators": ["Builtin.Correctness"], "wait_seconds": 0,
    })
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "dataset.invalid_execution"
    assert client.get("/api/eval/runs").json()["total"] == 0


def test_sync_to_aws_forwards_metadata_verbatim(client, monkeypatch):
    """Cloud examples are free-form JSON documents (botocore SensitiveJson) —
    the validated extension is forwarded, never stripped. A service-side
    rejection would surface as dataset.sync_failed like any other."""
    from unittest.mock import MagicMock

    import app.evaluation.routers as routers

    created = client.post("/api/eval/datasets", json={"name": "iso", "items": [ISOLATION_SCENARIO]})
    ds_id = created.json()["id"]
    control = MagicMock()
    control.create_dataset.return_value = {"datasetId": "ds-1", "datasetArn": "arn:ds-1"}
    control.get_dataset.return_value = {"datasetId": "ds-1", "datasetArn": "arn:ds-1",
                                        "status": "ACTIVE"}
    control.list_dataset_versions.return_value = {"datasetVersions": []}
    monkeypatch.setattr(routers, "control_client", lambda _ws=None: control)
    res = client.post(f"/api/eval/datasets/{ds_id}/sync-to-aws")
    assert res.status_code == 200, res.text
    examples = control.create_dataset.call_args.kwargs["source"]["inlineExamples"]["examples"]
    assert examples[0]["metadata"] == ISOLATION_SCENARIO["metadata"]


# ─── correction pass 2 regressions ───────────────────────────────────────────
def test_ordinary_datasets_keep_old_acceptance_without_opt_in(client):
    """The expanded-call cap applies only to datasets with an opt-in item: a
    plain 201-turn scenario (and a 101×2 pair) is accepted exactly as before."""
    big = {"scenario_id": "long", "turns": [{"input": f"t{i}"} for i in range(201)]}
    res = client.post("/api/eval/datasets", json={"name": "long", "items": [big]})
    assert res.status_code == 201, res.text
    pair = [{"scenario_id": f"p{k}", "turns": [{"input": f"t{i}"} for i in range(101)]}
            for k in range(2)]
    res = client.post("/api/eval/datasets", json={"name": "pair", "items": pair})
    assert res.status_code == 201
    # ... but the same volume next to an opt-in item is budgeted
    res = client.post("/api/eval/datasets",
                      json={"name": "mixed", "items": [big, ISOLATION_SCENARIO]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.execution_limits"


def test_misplaced_execution_container_is_refused_not_ignored(client):
    listed = {**copy.deepcopy(ISOLATION_SCENARIO), "metadata": ["launchpad_execution"]}
    res = client.post("/api/eval/datasets", json={"name": "list", "items": [listed]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_execution"
    assert "metadata object" in res.json()["message"]
    # an unrelated non-dict metadata on an ordinary scenario is still accepted
    odd = {"scenario_id": "odd", "turns": [{"input": "x"}], "metadata": ["tag"]}
    res = client.post("/api/eval/datasets", json={"name": "odd", "items": [odd]})
    assert res.status_code == 201


@pytest.mark.parametrize("route", ["create", "upload", "update"])
def test_opt_in_item_size_is_bounded_on_every_ingress(client, route):
    fat = copy.deepcopy(ISOLATION_SCENARIO)
    fat["metadata"]["provenance"] = "x" * 17000
    if route == "create":
        res = client.post("/api/eval/datasets", json={"name": "fat", "items": [fat]})
    elif route == "upload":
        res = client.post("/api/eval/datasets/upload",
                          json={"name": "fat", "jsonl": json.dumps(fat)})
    else:
        ok = client.post("/api/eval/datasets", json={"name": "ok", "items": [ISOLATION_SCENARIO]})
        res = client.put(f"/api/eval/datasets/{ok.json()['id']}", json={"items": [fat]})
    assert res.status_code == 422, res.text
    # create is caught first by DatasetCreate's own item-size validator (generic
    # 422); upload/update reach the procedure gate and name the bound
    assert route == "create" or "16000" in res.json()["message"]


@pytest.mark.parametrize("order", ["advanced_first", "legacy_first"])
def test_normalized_id_collision_with_legacy_item_is_refused(client, order):
    """normalize_scenarios names legacy prompt items item_<N>; an opt-in
    scenario with that id would alias it. Refused on every ingress."""
    if order == "advanced_first":
        items = [dict(copy.deepcopy(ISOLATION_SCENARIO), scenario_id="item_2"), {"prompt": "bye"}]
    else:
        items = [{"prompt": "hi"}, dict(copy.deepcopy(ISOLATION_SCENARIO), scenario_id="item_1")]
    res = client.post("/api/eval/datasets", json={"name": "clash", "items": items})
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_execution"
    assert "collides" in res.json()["message"]
    res = client.post("/api/eval/datasets/upload",
                      json={"name": "clash", "jsonl": "\n".join(json.dumps(i) for i in items)})
    assert res.status_code == 422


def test_local_run_preflight_revalidates_stored_items(client, monkeypatch):
    """A row written before a rule tightened (repeat 6) never reaches
    submit_run / telemetry / the queue."""
    from unittest.mock import MagicMock

    import app.evaluation.service as svc
    from tests.evaluation.test_runs_flow import make_agent

    db = SessionLocal()
    agent = make_agent(db, name="stale-agent")
    ds = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="stale", kind="predefined",
                     items=[scenario(repeat=6)])
    db.add(ds)
    db.commit()
    ds_id, agent_id = ds.id, agent.id
    db.close()
    submit = MagicMock()
    monkeypatch.setattr(svc, "submit_run", submit)
    res = client.post("/api/eval/runs", json={"agent_id": agent_id, "dataset_id": ds_id,
                                              "evaluators": ["Builtin.Correctness"]})
    assert res.status_code == 422 and res.json()["code"] == "dataset.invalid_execution"
    submit.assert_not_called()


def test_synthetic_actor_prefix_collision_pair():
    kw = dict(workspace_id="default", agent_id="a" * 32, run_id="run123456789", repeat=1, alias="A")
    a = ex.synthetic_actor(**kw, scenario_id="same-scenario-prefix-123-4317")
    b = ex.synthetic_actor(**kw, scenario_id="same-scenario-prefix-123-5686")
    assert a != b and len(a) <= 255
    assert a.split("__")[-1] != b.split("__")[-1] and len(a.split("__")[-1]) == 32
