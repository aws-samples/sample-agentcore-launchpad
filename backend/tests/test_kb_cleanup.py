"""WB-008: a KB delete never 502s after its ledger row is gone — every storage step is
best-effort, the response names what is still pending, and a `kb_cleanup` job retries it
(bounded), survives a restart, and is observable on `GET /api/jobs/{job_id}`."""

import json

import pytest
import urllib3
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.core.errors import NotFoundError
from app.deployer import pipeline
from app.models.ledger import Job
from app.services import kb_cleanup, knowledge_backend
from app.services import opensearch_http as core
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from tests.conftest import set_default_resources, ws_ctx
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import BUCKET, SUBSTRATE, T0


@pytest.fixture
def fakes():
    return {"s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
            "embedder": FakeEmbedder()}


@pytest.fixture
def backend(client, fakes):
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    return SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)


@pytest.fixture
def ws():
    return ws_ctx(SUBSTRATE, region="cn-northwest-1")


@pytest.fixture
def deferred(monkeypatch) -> list:
    """The module's one thread starter records its target instead of starting a thread,
    so a test runs the background retries by hand, after re-arranging the fakes."""
    targets: list = []
    monkeypatch.setattr(kb_cleanup, "_SPAWN", lambda target, name: targets.append((name, target)))
    return targets


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    out: list[float] = []
    monkeypatch.setattr(kb_cleanup, "_SLEEP", out.append)
    return out


def _timeout() -> Exception:
    """What `RetryingHttp` re-raises after its last attempt against a busy domain."""
    return urllib3.exceptions.ReadTimeoutError(None, "/launchpad-kb-x", "Read timed out")


def _s3_error() -> Exception:
    return ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce"}},
                       "ListObjectsV2")


def _break(fakes, step: str, exc: Exception | None = None, times: int | None = None):
    """Make one cleanup step fail (`times` failures then recover; forever when None)."""
    count = {"n": 0}

    def boom(*args, **kwargs):
        count["n"] += 1
        if times is not None and count["n"] > times:
            del fakes[target].__dict__[attr]  # back to the class method
            return getattr(fakes[target], attr)(*args, **kwargs)
        raise exc or _timeout()

    target, attr = {
        "s3": ("s3", "list_objects_v2"),
        "index": ("opensearch", "delete_index"),
        "docs": ("opensearch", "delete_by_query"),
    }[step]
    setattr(fakes[target], attr, boom)
    return lambda: fakes[target].__dict__.pop(attr, None)


def _seed(backend, ws, fakes):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds = kb["data_sources"][0]
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}a.md", Body=b"x")
    fakes["opensearch"].put_doc(core.kb_docs_index(""), "doc",
                                {"kb_id": kb["kb_id"], "ds_id": ds["ds_id"], "name": "a.md"})
    fakes["opensearch"].put_doc(core.jobs_index(""), "job",
                                {"kb_id": kb["kb_id"], "ds_id": ds["ds_id"], "status": "DONE"})
    return kb


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        db.expunge(job)
        return job
    finally:
        db.close()


def _events(job: Job) -> list[dict]:
    return [json.loads(line) for line in job.log.splitlines() if line.strip()]


def _leftovers(fakes, kb_id: str) -> dict[str, bool]:
    return {
        "s3": any(k.startswith(f"kb/{kb_id}/") for (_, k) in fakes["s3"].objects),
        "index": core.kb_index_name(kb_id, "") in fakes["opensearch"].indices,
        "docs": bool(fakes["opensearch"].indices[core.kb_docs_index("")]
                     or fakes["opensearch"].indices[core.jobs_index("")]),
    }


# ── the response ─────────────────────────────────────────────────────────────────
IAM_403_BODY = (
    '{"error":{"root_cause":[{"type":"security_exception","reason":"User: arn:aws-cn:sts::'
    '123456789012:assumed-role/launchpad-hub-role/i-0abc is not authorized to perform: '
    'es:ESHttpPost"}],"type":"security_exception"},"status":403}'
)


@pytest.mark.parametrize("step, exc, expected", [
    ("index", core.OpenSearchError(403, IAM_403_BODY), "index: OpenSearchError: HTTP 403"),
    ("docs", core.OpenSearchError(400, '{"error":{"root_cause":[{"type":"parsing_exception",'
                                       '"reason":"[term] malformed query"}]},"status":400}'),
     "docs: OpenSearchError: HTTP 400"),
    ("s3", ClientError({"Error": {"Code": "AccessDenied", "Message":
                                  "User arn:aws-cn:iam::123456789012:role/x is not authorized"}},
                       "ListObjectsV2"), "s3: ClientError: AccessDenied (ListObjectsV2)"),
])
def test_cleanup_warnings_carry_type_and_code_never_the_seam_body(
    backend, ws, fakes, deferred, slept, step, exc, expected,
):
    """N17-obs99: the operator-facing warning names the step, the exception type and the
    status / error code + operation — the raw OpenSearch body (the IAM sentence with the role
    ARN, the parsing root cause) and botocore's framing never reach the response or the job
    payload."""
    kb = _seed(backend, ws, fakes)
    _break(fakes, step, exc)
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["cleanup_warnings"] == [expected]
    text = json.dumps(out)
    for marker in ("is not authorized", "root_cause", "assumed-role", "An error occurred",
                   "malformed"):
        assert marker not in text, marker
    job = _job(out["cleanup_job_id"])
    assert job.payload["warnings"] == [expected]


@pytest.mark.parametrize("step, exc, needle", [
    ("s3", _s3_error(), "s3: ClientError: SlowDown (ListObjectsV2)"),
    ("index", _timeout(), "index: ReadTimeoutError"),
    ("docs", _timeout(), "docs: ReadTimeoutError"),
])
def test_a_transport_error_in_any_step_answers_deleted_with_the_step_pending_and_a_job(
    backend, ws, fakes, deferred, slept, step, exc, needle,
):
    kb = _seed(backend, ws, fakes)
    unbreak = _break(fakes, step, exc)
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["kb_id"] == kb["kb_id"] and out["deleted"] is True
    assert out["cleanup"] == {s: ("pending" if s == step else "ok") for s in kb_cleanup.STEPS}
    assert len(out["cleanup_warnings"]) == 1 and out["cleanup_warnings"][0].startswith(needle)
    # the row is gone regardless …
    with pytest.raises(NotFoundError):
        backend.get_kb_detail(ws, kb["kb_id"])
    # … the other two steps ran, the failed one left its leftovers
    assert _leftovers(fakes, kb["kb_id"]) == {s: s == step for s in kb_cleanup.STEPS}
    # the job is filed `queued` with exactly the pending step, and its thread was started
    job = _job(out["cleanup_job_id"])
    assert job.type == kb_cleanup.JOB_TYPE and job.status == "queued"
    assert job.workspace_id == ws.id
    assert job.payload["kb_id"] == kb["kb_id"] and job.payload["bucket"] == BUCKET
    assert job.payload["steps"] == [step] and job.payload["warnings"] == out["cleanup_warnings"]
    assert job.payload["schedule_s"] == [5.0, 30.0, 120.0]
    assert [name for name, _ in deferred] == [f"kb-cleanup-{kb['kb_id']}"]

    # the domain/bucket recovers; the first retry finishes the job
    unbreak()
    deferred[0][1]()
    assert slept == [5.0]
    job = _job(out["cleanup_job_id"])
    assert job.status == "succeeded" and job.error is None and job.payload["steps"] == []
    assert _leftovers(fakes, kb["kb_id"]) == {s: False for s in kb_cleanup.STEPS}
    msgs = [e["msg"] for e in _events(job)]
    assert msgs[0].startswith(f"retrying cleanup of {step} for kb {kb['kb_id']} at +5s, +30s")
    assert f"retry 1/3: ok [{step}] pending []" in msgs[1]
    assert msgs[-1] == f"cleanup of kb {kb['kb_id']} complete"
    assert all(e["stage"] == "kb_cleanup" for e in _events(job))


def test_every_step_failing_is_still_deleted_true_and_one_job_for_all_three(
    backend, ws, fakes, deferred,
):
    kb = _seed(backend, ws, fakes)
    for step in kb_cleanup.STEPS:
        _break(fakes, step)
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["deleted"] is True
    assert out["cleanup"] == {"s3": "pending", "index": "pending", "docs": "pending"}
    assert [w.split(":")[0] for w in out["cleanup_warnings"]] == ["s3", "index", "docs"]
    assert _job(out["cleanup_job_id"]).payload["steps"] == ["s3", "index", "docs"]
    assert len(deferred) == 1


def test_a_second_delete_404s_the_way_it_always_did(backend, ws, fakes, deferred):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")
    assert backend.delete_kb(ws, kb["kb_id"], force=True)["deleted"] is True
    with pytest.raises(NotFoundError) as exc:
        backend.delete_kb(ws, kb["kb_id"], force=True)
    assert exc.value.code == "kb.not_found"


# ── the retries ──────────────────────────────────────────────────────────────────
def test_the_job_retries_on_the_bounded_schedule_then_fails_with_the_reason(
    backend, ws, fakes, deferred, slept,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")  # never recovers
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    deferred[0][1]()
    assert slept == [5.0, 30.0, 120.0]
    job = _job(out["cleanup_job_id"])
    assert job.status == "failed"
    assert job.error.startswith(f"cleanup of kb {kb['kb_id']} still pending after 3 retries: "
                                "index: ReadTimeoutError")
    assert job.payload["steps"] == ["index"]
    assert job.payload["warnings"][0].startswith("index: ReadTimeoutError")
    events = _events(job)
    retries = [e for e in events if e["msg"].startswith("retry ")]
    assert [e["msg"].split(":")[0] for e in retries] == ["retry 1/3", "retry 2/3", "retry 3/3"]
    assert all(e["level"] == "warning" and "pending [index]" in e["msg"] for e in retries)
    assert events[-1]["level"] == "error"
    assert _leftovers(fakes, kb["kb_id"])["index"] is True  # honest: still there


def test_a_step_recovering_mid_schedule_ends_the_job_early(
    backend, ws, fakes, deferred, slept,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "docs", times=2)  # request thread + retry 1 fail, retry 2 succeeds
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    deferred[0][1]()
    assert slept == [5.0, 30.0]
    job = _job(out["cleanup_job_id"])
    assert job.status == "succeeded" and job.payload["steps"] == []
    assert _leftovers(fakes, kb["kb_id"])["docs"] is False


def test_only_the_pending_steps_are_retried(backend, ws, fakes, deferred, slept):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "s3", _s3_error())
    calls: list[str] = []
    real = backend.run_cleanup_step

    def spy(ws_, kb_id, bucket, step):
        calls.append(step)
        return real(ws_, kb_id, bucket, step)

    backend.run_cleanup_step = spy  # type: ignore[method-assign]
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert calls == ["s3", "index", "docs"]
    _break(fakes, "s3", _s3_error(), times=0)  # i.e. unbreak via recovery path
    deferred[0][1]()
    assert calls == ["s3", "index", "docs", "s3"]
    assert _job(out["cleanup_job_id"]).status == "succeeded"


# ── nothing raises past the delete ───────────────────────────────────────────────
def test_a_thread_that_will_not_start_fails_the_job_not_the_delete(
    backend, ws, fakes, monkeypatch,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")

    def refuse(target, name):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(kb_cleanup, "_SPAWN", refuse)
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["deleted"] is True and out["cleanup"]["index"] == "pending"
    job = _job(out["cleanup_job_id"])
    assert job.status == "failed"
    assert job.error == "cleanup thread did not start: RuntimeError: can't start new thread"


def test_a_ledger_that_refuses_the_job_row_still_answers_deleted(
    backend, ws, fakes, deferred, monkeypatch, caplog,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")

    def no_ledger():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(kb_cleanup, "SessionLocal", no_ledger)
    with caplog.at_level("WARNING", logger="launchpad.knowledge"):
        out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["deleted"] is True and out["cleanup_job_id"] is None
    assert "could not file the cleanup job" in caplog.text
    assert len(deferred) == 1  # the retries still run, logging only
    deferred[0][1]()  # must not raise either


def test_the_route_answers_200_deleted_true_and_the_job_is_readable(client, fakes, deferred):
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb = _seed(backend, ws, fakes)
    for step in kb_cleanup.STEPS:
        _break(fakes, step)
    res = client.delete(f"/api/knowledge-bases/{kb['kb_id']}?force=true")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["kb_id"] == kb["kb_id"] and body["deleted"] is True
    assert body["cleanup"] == {"s3": "pending", "index": "pending", "docs": "pending"}
    assert len(body["cleanup_warnings"]) == 3
    # the warnings carry type + first line, never a response body
    assert all(":" in w and "\n" not in w for w in body["cleanup_warnings"])
    job = client.get(f"/api/jobs/{body['cleanup_job_id']}")
    assert job.status_code == 200
    assert job.json()["type"] == "kb_cleanup" and job.json()["status"] == "queued"
    assert job.json()["payload"]["steps"] == ["s3", "index", "docs"]
    # the list no longer carries the KB, and a retried DELETE is a plain 404
    listed = client.get("/api/knowledge-bases").json()["items"]
    assert kb["kb_id"] not in [k["kb_id"] for k in listed]
    again = client.delete(f"/api/knowledge-bases/{kb['kb_id']}?force=true")
    assert again.status_code == 404 and again.json()["code"] == "kb.not_found"


# ── resume after a restart ───────────────────────────────────────────────────────
def _inline(monkeypatch) -> list[str]:
    spawned: list[str] = []

    def spawn(target, name):
        spawned.append(name)
        target()

    monkeypatch.setattr(kb_cleanup, "_SPAWN", spawn)
    return spawned


def test_resume_pending_jobs_re_runs_an_interrupted_cleanup(
    backend, ws, fakes, deferred, slept, monkeypatch,
):
    kb = _seed(backend, ws, fakes)
    unbreak = _break(fakes, "index")
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    job_id = out["cleanup_job_id"]
    # the process died before the thread ran: the row is still `queued`; mark it `running`
    # too, the shape a crash mid-retry leaves — both are picked up
    db = SessionLocal()
    db.get(Job, job_id).status = "running"
    db.commit()
    db.close()
    unbreak()
    knowledge_backend.set_backend(backend)  # startup registers the backend first
    set_default_resources(SUBSTRATE)
    spawned = _inline(monkeypatch)
    assert pipeline.resume_pending_jobs() == [job_id]
    assert spawned == [f"kb-cleanup-resume-{job_id[:8]}"]
    assert slept == [5.0]
    job = _job(job_id)
    assert job.status == "succeeded" and job.payload["steps"] == []
    assert _leftovers(fakes, kb["kb_id"])["index"] is False
    msgs = [e["msg"] for e in _events(job)]
    assert any(m.startswith("interrupted by backend restart — re-running the cleanup of [index]")
               for m in msgs)


def test_resume_leaves_terminal_and_foreign_jobs_alone(backend, ws, fakes, deferred):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")
    job_id = backend.delete_kb(ws, kb["kb_id"], force=True)["cleanup_job_id"]
    db = SessionLocal()
    db.get(Job, job_id).status = "succeeded"
    db.add(Job(id="othertype00000000000000000000001", workspace_id=ws.id,
               type="log_group_settle", status="queued", payload={}))
    db.commit()
    db.close()
    assert kb_cleanup._resume(job_id) is None
    assert kb_cleanup._resume("othertype00000000000000000000001") is None
    assert kb_cleanup._resume("nope") is None
    assert _job(job_id).status == "succeeded"


def test_resume_without_a_registered_backend_fails_the_job_without_raising(
    backend, ws, fakes, deferred, monkeypatch,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")
    job_id = backend.delete_kb(ws, kb["kb_id"], force=True)["cleanup_job_id"]
    knowledge_backend.set_backend(None)
    _inline(monkeypatch)
    kb_cleanup.start_cleanup_resume(job_id)
    job = _job(job_id)
    assert job.status == "failed"
    assert job.error == ("interrupted by backend restart; not re-run: no knowledge backend "
                         "with cleanup steps is registered")


def test_resume_of_a_job_whose_workspace_is_gone_fails_without_raising(
    backend, ws, fakes, deferred, monkeypatch,
):
    kb = _seed(backend, ws, fakes)
    _break(fakes, "index")
    job_id = backend.delete_kb(ws, kb["kb_id"], force=True)["cleanup_job_id"]
    db = SessionLocal()
    db.get(Job, job_id).workspace_id = "ghost"
    db.commit()
    db.close()
    knowledge_backend.set_backend(backend)
    _inline(monkeypatch)
    kb_cleanup.start_cleanup_resume(job_id)
    job = _job(job_id)
    assert job.status == "failed" and "workspace 'ghost' could not be rebuilt" in job.error


def test_resume_with_a_damaged_payload_closes_the_job_honestly(backend, ws, monkeypatch):
    knowledge_backend.set_backend(backend)
    _inline(monkeypatch)
    db = SessionLocal()
    empty = Job(workspace_id=ws.id, type=kb_cleanup.JOB_TYPE, status="queued",
                payload={"kb_id": "KBX", "steps": []})
    no_bucket = Job(workspace_id=ws.id, type=kb_cleanup.JOB_TYPE, status="queued",
                    payload={"kb_id": "KBX", "steps": ["s3"], "bucket": ""})
    db.add_all([empty, no_bucket])
    db.commit()
    ids = (empty.id, no_bucket.id)
    db.close()
    kb_cleanup.start_cleanup_resume(ids[0])
    kb_cleanup.start_cleanup_resume(ids[1])
    assert _job(ids[0]).status == "succeeded"
    assert _job(ids[1]).status == "failed"
    assert "payload carries no bucket for the s3 step" in _job(ids[1]).error


def test_run_steps_never_raises_and_rejects_unknown_steps(backend, ws):
    outcome, warnings = kb_cleanup.run_steps(backend, ws, "KBX", BUCKET, ["index", "bogus"])
    assert outcome == {"index": "ok", "bogus": "pending"}
    assert warnings == ["bogus: ValueError: unknown cleanup step 'bogus'"]


# ── WB-010: the same contract on a data-source delete ────────────────────────────
def _break_dbq(fakes, index: str, times: int | None = None):
    """Fail `delete_by_query` for ONE index only (the KB index vs the fixed kb-docs index
    are two different data-source steps); recover after `times` failures when given."""
    real = fakes["opensearch"].delete_by_query
    count = {"n": 0}

    def boom(name, query):
        if name == index:
            count["n"] += 1
            if times is None or count["n"] <= times:
                raise _timeout()
        return real(name, query)

    fakes["opensearch"].delete_by_query = boom
    return lambda: fakes["opensearch"].__dict__.pop("delete_by_query", None)


def _seed_ds(backend, ws, fakes):
    kb = _seed(backend, ws, fakes)
    ds = kb["data_sources"][0]
    fakes["opensearch"].put_doc(core.kb_index_name(kb["kb_id"], ""), "chunk",
                                {"kb_id": kb["kb_id"], "ds_id": ds["ds_id"], "text": "x"})
    return kb, ds


def _ds_leftovers(fakes, kb, ds) -> dict[str, bool]:
    return {
        "s3": any(k.startswith(ds["prefix"]) for (_, k) in fakes["s3"].objects),
        "index": bool(fakes["opensearch"].indices[core.kb_index_name(kb["kb_id"], "")]),
        "docs": any(d.get("ds_id") == ds["ds_id"]
                    for d in fakes["opensearch"].indices[core.kb_docs_index("")].values()),
    }


@pytest.mark.parametrize("step", ["s3", "index", "docs"])
def test_ds_delete_transport_error_in_any_step_answers_deleted_with_a_scoped_job(
    backend, ws, fakes, deferred, slept, step,
):
    kb, ds = _seed_ds(backend, ws, fakes)
    if step == "s3":
        unbreak = _break(fakes, "s3", _s3_error())
    else:
        unbreak = _break_dbq(fakes, core.kb_index_name(kb["kb_id"], "") if step == "index"
                             else core.kb_docs_index(""))
    out = backend.delete_data_source(ws, kb["kb_id"], ds["ds_id"])
    assert out["ds_id"] == ds["ds_id"] and out["deleted"] is True
    assert out["cleanup"] == {s: ("pending" if s == step else "ok") for s in kb_cleanup.STEPS}
    assert len(out["cleanup_warnings"]) == 1 and out["cleanup_warnings"][0].startswith(step + ":")
    assert backend.get_kb_detail(ws, kb["kb_id"])["data_sources"] == []  # row updated anyway
    assert _ds_leftovers(fakes, kb, ds) == {s: s == step for s in kb_cleanup.STEPS}
    job = _job(out["cleanup_job_id"])
    assert job.type == kb_cleanup.JOB_TYPE and job.status == "queued"
    assert job.payload["scope"] == "data_source" and job.payload["ds_id"] == ds["ds_id"]
    assert job.payload["prefix"] == ds["prefix"] and job.payload["kb_id"] == kb["kb_id"]
    assert job.payload["steps"] == [step] and job.payload["bucket"] == BUCKET
    assert [name for name, _ in deferred] == [f"kb-cleanup-{kb['kb_id']}-{ds['ds_id']}"]
    unbreak()
    deferred[0][1]()
    assert slept == [5.0]
    assert _job(out["cleanup_job_id"]).status == "succeeded"
    assert _ds_leftovers(fakes, kb, ds) == {s: False for s in kb_cleanup.STEPS}
    # the KB's own rows in the fixed index were never touched by a data-source cleanup
    assert core.kb_index_name(kb["kb_id"], "") in fakes["opensearch"].indices


def test_ds_delete_of_an_existing_mode_source_skips_s3_and_never_purges(
    backend, ws, fakes, deferred,
):
    kb = backend.create_kb(ws, "docs", "", {"mode": "upload"})
    ds2 = backend.add_data_source(
        ws, kb["kb_id"], {"mode": "existing", "bucket": BUCKET, "prefix": f"kb/{kb['kb_id']}/x"},
    )
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds2['prefix']}keep.md", Body=b"x")
    _break(fakes, "s3", _s3_error())  # would fail if the s3 step ran
    out = backend.delete_data_source(ws, kb["kb_id"], ds2["ds_id"])
    assert out["cleanup"] == {"s3": "skipped", "index": "ok", "docs": "ok"}
    assert out["cleanup_warnings"] == [] and out["cleanup_job_id"] is None
    assert (BUCKET, f"{ds2['prefix']}keep.md") in fakes["s3"].objects and deferred == []


def test_ds_index_step_treats_a_missing_kb_index_as_done(backend, ws, fakes, deferred):
    kb, ds = _seed_ds(backend, ws, fakes)
    fakes["opensearch"].delete_index(core.kb_index_name(kb["kb_id"], ""))  # a FAILED KB
    real = fakes["opensearch"].delete_by_query

    def strict(name, query):
        if name not in fakes["opensearch"].indices:
            raise core.OpenSearchError(404, "index_not_found_exception")
        return real(name, query)

    fakes["opensearch"].delete_by_query = strict
    out = backend.delete_data_source(ws, kb["kb_id"], ds["ds_id"])
    assert out["cleanup"] == {"s3": "ok", "index": "ok", "docs": "ok"} and deferred == []


def test_ds_route_answers_200_when_every_step_fails_and_the_scoped_job_resumes(
    client, fakes, deferred, slept, monkeypatch,
):
    backend = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    fakes["opensearch"].ensure_index(core.jobs_index(""), core.jobs_index_body())
    fakes["opensearch"].ensure_index(core.kb_docs_index(""), core.kb_docs_index_body())
    knowledge_backend.set_backend(backend)
    set_default_resources(SUBSTRATE)
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1")
    kb, ds = _seed_ds(backend, ws, fakes)
    unbreak_s3 = _break(fakes, "s3", _s3_error())
    unbreak_dbq = _break(fakes, "docs")  # every delete_by_query → both index + docs steps
    res = client.delete(f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds['ds_id']}")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] is True
    assert body["cleanup"] == {"s3": "pending", "index": "pending", "docs": "pending"}
    assert [w.split(":")[0] for w in body["cleanup_warnings"]] == ["s3", "index", "docs"]
    job = client.get(f"/api/jobs/{body['cleanup_job_id']}").json()
    assert job["type"] == "kb_cleanup" and job["payload"]["scope"] == "data_source"
    # a retried DELETE is a plain 404, as before
    again = client.delete(f"/api/knowledge-bases/{kb['kb_id']}/data-sources/{ds['ds_id']}")
    assert again.status_code == 404 and again.json()["code"] == "kb.data_source_not_found"
    # restart: the single resume starter re-runs the data-source-scoped job
    unbreak_s3()
    unbreak_dbq()
    spawned = _inline(monkeypatch)
    assert pipeline.resume_pending_jobs() == [body["cleanup_job_id"]]
    assert spawned == [f"kb-cleanup-resume-{body['cleanup_job_id'][:8]}"]
    assert slept == [5.0]
    done = _job(body["cleanup_job_id"])
    assert done.status == "succeeded" and done.payload["steps"] == []
    assert _ds_leftovers(fakes, kb, ds) == {s: False for s in kb_cleanup.STEPS}
    assert any("data source " + ds["ds_id"] in e["msg"] for e in _events(done))


def test_ds_scope_resume_without_a_ds_id_closes_the_job_honestly(backend, ws, monkeypatch):
    knowledge_backend.set_backend(backend)
    _inline(monkeypatch)
    db = SessionLocal()
    job = Job(workspace_id=ws.id, type=kb_cleanup.JOB_TYPE, status="queued",
              payload={"kb_id": "KBX", "bucket": BUCKET, "steps": ["docs"],
                       "scope": "data_source"})
    db.add(job)
    db.commit()
    job_id = job.id
    db.close()
    kb_cleanup.start_cleanup_resume(job_id)
    assert _job(job_id).status == "failed"
    assert "data_source scope without a ds_id" in _job(job_id).error


def test_ds_s3_step_refuses_a_prefix_outside_the_kb_namespace(backend, ws):
    with pytest.raises(ValueError, match="refusing to purge"):
        backend.run_cleanup_step(ws, "KBX", BUCKET, "s3", scope="data_source",
                                 ds_id="DS1", prefix="memory/")


# ── N17-obs119 (cn/r17-t): a refused key inside a 200 is a pending step, not a purge ──
def test_a_key_refused_inside_a_200_delete_objects_leaves_the_s3_step_pending(
    backend, ws, fakes, deferred, slept,
):
    """S3 answers `DeleteObjects` with HTTP 200 and `Errors[]` for the keys it refused; the
    `s3` step used to read nothing and call the prefix purged. Now the shared deleter
    (`s3_sweep`) raises `S3SweepResidue`, the step is `pending` with one redacted warning
    (counts + S3 code, never a key or the S3 message), the job retries it and succeeds once
    S3 stops refusing."""
    kb = _seed(backend, ws, fakes)
    (ds,) = kb["data_sources"]
    fakes["s3"].put_object(Bucket=BUCKET, Key=f"{ds['prefix']}b.md", Body=b"y")
    real = fakes["s3"].delete_objects
    state = {"refuse": True}

    def partial(*, Bucket, Delete, **kw):
        keys = [o["Key"] for o in Delete["Objects"]]
        if not state["refuse"]:
            return real(Bucket=Bucket, Delete=Delete)
        real(Bucket=Bucket, Delete={"Objects": [{"Key": keys[0]}]})
        return {"Deleted": [{"Key": keys[0]}],
                "Errors": [{"Key": k, "Code": "AccessDenied", "Message": "Access Denied"}
                           for k in keys[1:]]}

    fakes["s3"].delete_objects = partial
    out = backend.delete_kb(ws, kb["kb_id"], force=True)
    assert out["deleted"] is True
    assert out["cleanup"] == {"s3": "pending", "index": "ok", "docs": "ok"}
    assert out["cleanup_warnings"] == ["s3: S3SweepResidue: 1 of 2 objects refused (AccessDenied)"]
    text = json.dumps(out)
    assert "Access Denied" not in text and "b.md" not in text and "a.md" not in text
    assert _leftovers(fakes, kb["kb_id"])["s3"] is True  # honest: the refused key is there
    state["refuse"] = False
    deferred[0][1]()
    assert slept == [5.0]
    assert _job(out["cleanup_job_id"]).status == "succeeded"
    assert _leftovers(fakes, kb["kb_id"])["s3"] is False
