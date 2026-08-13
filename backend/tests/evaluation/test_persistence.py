"""Run history persistence — adapted from agentcore_eva_opt tests/test_persistence.py.

A "restart" is simulated by reading through a brand-new session/app instance:
completed runs must still be there (SQLite is the source of truth).
"""

from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.main import create_app


def test_runs_survive_restart():
    db = SessionLocal()
    run = EvalRun(

            workspace_id=DEFAULT_WORKSPACE_ID,
            agent_id="a1", agent_name="persist-agent", dataset_name="ds",
        evaluators=["Builtin.Correctness"], status="completed",
        scores=[{"evaluatorId": "Builtin.Correctness", "score": 0.9}],
    )
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()

    # "restart": a fresh app instance over the same database
    fresh_client = TestClient(create_app())
    runs = fresh_client.get("/api/eval/runs").json()["runs"]
    match = next((r for r in runs if r["id"] == run_id), None)
    assert match is not None
    assert match["status"] == "completed"
    assert match["scores"][0]["score"] == 0.9


def _wait_until(predicate, timeout=2.5):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_queue_excess_run_queues_not_fails(client, monkeypatch):
    """At capacity, the next run reports QUEUED (position ≥ 1) instead of failing."""
    import threading

    from app.evaluation.queue import EvalRunQueue

    run_queue = EvalRunQueue(max_concurrency=1)
    release = threading.Event()
    started = threading.Event()

    def slow_job():
        started.set()
        release.wait(timeout=5)

    run_queue.submit("run-A", slow_job)
    assert started.wait(timeout=2)
    position_b = run_queue.submit("run-B", lambda: None)
    state = run_queue.state()
    assert state["running"] == ["run-A"]
    assert "run-B" in state["queued"] and position_b >= 1
    assert run_queue.position("run-B") == 1  # visible queue position
    assert state["locked"] is True
    release.set()
    assert _wait_until(
        lambda: not run_queue.state()["running"] and not run_queue.state()["queued"]
    )
    assert run_queue.state()["locked"] is False


def test_queue_runs_up_to_cap_concurrently(client, monkeypatch):
    """Three runs execute at once; the fourth waits for a slot, then executes."""
    import threading

    from app.evaluation.queue import EvalRunQueue

    run_queue = EvalRunQueue(max_concurrency=3)
    release = threading.Event()
    started: list[threading.Event] = [threading.Event() for _ in range(3)]

    def slow_job(idx: int):
        started[idx].set()
        release.wait(timeout=5)

    for idx in range(3):
        run_queue.submit(f"run-{idx}", lambda i=idx: slow_job(i))
    assert all(event.wait(timeout=2) for event in started)  # truly concurrent

    fourth_ran = threading.Event()
    position = run_queue.submit("run-3", fourth_ran.set)
    assert position >= 1
    state = run_queue.state()
    assert state["locked"] is True and state["max_concurrency"] == 3
    assert sorted(state["running"]) == ["run-0", "run-1", "run-2"]
    assert "run-3" in state["queued"]
    assert run_queue.position("run-3") == 1
    assert not fourth_ran.is_set()  # no free slot yet

    release.set()
    assert fourth_ran.wait(timeout=2)
    assert _wait_until(
        lambda: not run_queue.state()["running"] and not run_queue.state()["queued"]
    )
    assert run_queue.state()["locked"] is False


def test_queue_below_cap_submit_reports_position_zero(client):
    """With free slots, a new run starts immediately (position 0, not queued)."""
    import threading

    from app.evaluation.queue import EvalRunQueue

    run_queue = EvalRunQueue(max_concurrency=3)
    release = threading.Event()
    started = threading.Event()

    def slow_job():
        started.set()
        release.wait(timeout=5)

    assert run_queue.submit("run-A", slow_job) == 0
    assert started.wait(timeout=2)
    assert run_queue.state()["locked"] is False
    assert run_queue.submit("run-B", lambda: None) == 0  # 2 of 3 slots taken
    release.set()
    assert _wait_until(lambda: not run_queue.state()["running"])


def test_runs_list_pagination_and_mode_filter(client):
    """`/runs` pages newest-first with a total; no params keeps the old shape."""
    db = SessionLocal()
    made = []
    for index in range(7):
        run = EvalRun(

                workspace_id=DEFAULT_WORKSPACE_ID,
                agent_id="a1", agent_name=f"pager-agent-{index}", dataset_name="ds",
            evaluators=["Builtin.Correctness"], status="completed",
            mode="insights" if index % 2 else "evaluators",
        )
        db.add(run)
        db.commit()  # sequential created_at so ordering is deterministic
        made.append(run.id)
    db.close()
    newest_first = list(reversed(made))

    first = client.get("/api/eval/runs?limit=3&offset=0").json()
    assert [r["id"] for r in first["runs"]] == newest_first[:3]
    assert first["total"] >= 7 and first["limit"] == 3 and first["offset"] == 0

    second = client.get("/api/eval/runs?limit=3&offset=3").json()
    assert [r["id"] for r in second["runs"]] == newest_first[3:6]
    assert second["total"] == first["total"]  # total is the unpaginated count

    # default = pre-pagination behaviour (latest 50, no filter)
    default = client.get("/api/eval/runs").json()
    assert default["limit"] == 50 and default["offset"] == 0
    assert len(default["runs"]) <= 50

    # the console's insights duplicate guard reads this filter
    insights = client.get("/api/eval/runs?mode=insights&limit=200").json()
    assert all(r["mode"] == "insights" for r in insights["runs"])
    mine = [r["id"] for r in insights["runs"] if r["id"] in made]
    assert mine == [made[5], made[3], made[1]]  # the three insights rows, newest first
    assert insights["total"] == len(insights["runs"])


def test_runs_list_filters_by_agent_for_the_recommend_source_picker(client):
    """The experiment RECOMMEND card offers ONE agent's completed runs as a trace
    source; without this filter another agent's newer runs would hide them."""
    db = SessionLocal()
    mine, theirs = [], []
    for index in range(3):
        for agent_id, bucket in (("rec-a", mine), ("rec-b", theirs)):
            run = EvalRun(

                    workspace_id=DEFAULT_WORKSPACE_ID,
                    agent_id=agent_id, agent_name=agent_id, mode="insights",
                status="completed" if index else "evaluating",
                batch_eval_id=f"be-{agent_id}-{index}",
            )
            db.add(run)
            db.commit()
            bucket.append(run.id)
    db.close()

    listed = client.get("/api/eval/runs?agent_id=rec-a&limit=200").json()
    assert all(r["agent_id"] == "rec-a" for r in listed["runs"])
    assert {r["id"] for r in listed["runs"]} == set(mine)
    assert not ({r["id"] for r in listed["runs"]} & set(theirs))
    # the picker needs both fields to label and validate a choice client-side
    assert all("batch_eval_id" in r and "status" in r for r in listed["runs"])

    # composes with the mode filter
    combined = client.get(
        "/api/eval/runs?agent_id=rec-a&mode=insights&limit=200"
    ).json()
    assert {r["id"] for r in combined["runs"]} == set(mine)


def test_runs_list_rejects_out_of_range_paging(client):
    assert client.get("/api/eval/runs?limit=0").status_code == 422
    assert client.get("/api/eval/runs?limit=500").status_code == 422
    assert client.get("/api/eval/runs?offset=-1").status_code == 422
    assert client.get("/api/eval/runs?mode=bogus").status_code == 422
