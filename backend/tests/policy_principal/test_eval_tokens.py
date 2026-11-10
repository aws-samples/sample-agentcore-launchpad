# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_mint_entrances
"""Per-user policy D5 (contract I9 v2, amendment 10 — evaluation traffic is exempt from
policy): the hub's eval / canary token file holds ONE hub-signed system token per job
(``System::"eval"`` / ``System::"canary"``, never the launching user), minted at submission,
re-signed while the job runs (jti / iat / exp only), deleted at its terminal state and by the
startup sweep; inert with the switch off. And the worker's side: the same token rides in
every session's launchpad_meta; a job without a readable token runs token-less (the Gateway
decides) and never stops a sample."""

from __future__ import annotations

import json
import logging
import time
import types
from datetime import UTC, datetime, timedelta

import jsonschema
import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import jobs
from app.models.policy import EvalTokenFile
from app.policy import contracts
from app.policy.contracts import codec
from app.services import eval_tokens, principal_mint
from app.services.policy_entities import Principal
from app.services.strands_eval import jobs_batch
from app.services.strands_eval import principal_tokens as worker_tokens
from tests.conftest import ws_ctx
from tests.evaluation.test_jobs_replay_failures import (  # noqa: F401
    S1,
    S2,
    S3,
    Sink,
    _spec,
    harness,
)
from tests.policy_principal.test_mint_entrances import (  # noqa: F401
    _agent,
    _switches,
    kms,
    on,
)
from tests.policy_principal.test_principal_token import KEY_ID

BUCKET = "launchpad-artifacts-test"


class _S3:
    """A versioned bucket: every put is a version; a plain delete adds a delete marker."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, *, Bucket, Key, Body, **_kw):
        assert Bucket == BUCKET
        self.calls.append(("put", Key))
        self.objects[Key] = bytes(Body)
        self.versions.setdefault(Key, []).append(f"v{len(self.calls)}")

    def get_object(self, *, Bucket, Key, **_kw):
        return {"Body": types.SimpleNamespace(read=lambda: self.objects[Key])}

    def list_object_versions(self, *, Bucket, Prefix, **_kw):
        return {"Versions": [{"Key": k, "VersionId": v} for k, vs in self.versions.items()
                             if k.startswith(Prefix) for v in vs]}

    def delete_object(self, *, Bucket, Key, VersionId=None, **_kw):
        self.calls.append(("delete", Key))
        if VersionId is None:
            self.objects.pop(Key, None)
            return
        self.versions[Key] = [v for v in self.versions.get(Key, []) if v != VersionId]
        if not self.versions[Key]:
            self.objects.pop(Key, None)


@pytest.fixture
def s3(monkeypatch) -> _S3:
    from app.services import aws_clients

    fake = _S3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    return fake


@pytest.fixture
def ws():
    return ws_ctx({"artifacts_bucket": BUCKET}, id=DEFAULT_WORKSPACE_ID)


def _job(job_id="run-1", sids=(S1, S2)) -> jobs.JobSpec:
    return jobs.JobSpec(job_id=job_id, job_type="batch_eval", attempt=1, workspace={},
                        sink={"bucket": BUCKET, "prefix": jobs.job_prefix(job_id, 1)},
                        spec={"sessions": [{"session_id": s, "prompts": ["hi"]} for s in sids]})


def _file(s3: _S3, job_id="run-1") -> dict:
    return json.loads(s3.objects[eval_tokens.tokens_key(job_id)])


def _claims(token: str, signer, agent_id: str, now: float | None = None) -> dict:
    return codec.verify_token(token, {KEY_ID: signer.public_key},
                              now=time.time() if now is None else now,
                              workspace_id=DEFAULT_WORKSPACE_ID, env="",
                              agent_id=agent_id).claims


SYSTEM_EVAL = {"kind": "system", "id": "eval", "label": "system:eval"}
USER = Principal.from_identity(types.SimpleNamespace(
    username="hr-intern", role="member", user_id="u_hrintern01"))


def _prepare(ws, agent, job, channel="eval", **kw):
    return eval_tokens.prepare(ws, agent, job, channel=channel,
                               owner_kind=eval_tokens.OWNER_EVAL_RUN, owner_id="run-1", **kw)


# ── hub: submission ────────────────────────────────────────────────────────


def test_switch_off_writes_nothing_and_the_spec_is_untouched(ws, s3, monkeypatch, kms):
    _switches(global_on=False, workspace_on=False, monkeypatch=monkeypatch)
    job = _job()
    before = json.dumps(job.spec, sort_keys=True)
    assert _prepare(ws, _agent(), job) is False
    assert json.dumps(job.spec, sort_keys=True) == before and s3.calls == []
    _switches(global_on=True, workspace_on=False, monkeypatch=monkeypatch)
    assert _prepare(ws, _agent(), job) is False and s3.calls == []


def test_submission_mints_one_system_token_per_job(ws, s3, on, signer):
    agent = _agent()
    job = _job(sids=(S1, S2, S3))
    assert _prepare(ws, agent, job) is True
    body = _file(s3)
    jsonschema.Draft202012Validator(contracts.load_schema("eval_tokens")).validate(body)
    assert body["schema"] == "launchpad.policy.eval-tokens/2" and body["job_id"] == "run-1"
    assert "tokens" not in body  # one token for the whole job, not one per session
    claims = _claims(body["token"], signer, agent.id)
    assert claims["sid"] == "run-1" and claims["ch"] == "eval"  # one policy session per job
    assert claims["psid"] == codec.psid_for(DEFAULT_WORKSPACE_ID, "run-1", 0, 0)
    assert claims["p"] == SYSTEM_EVAL and claims["aud"] == [agent.id]
    assert len([c for c in s3.calls if c[0] == "put"]) == 1  # one KMS Sign, one write
    assert job.spec[eval_tokens.SPEC_FLAG] == {"key": "evaluations/run-1/tokens.json"}
    db = SessionLocal()
    try:
        row = db.get(EvalTokenFile, "run-1")
        assert row.principal == SYSTEM_EVAL and row.session_ids == []
        stored = json.dumps({c.name: getattr(row, c.name) for c in row.__table__.columns},
                            default=str)
    finally:
        db.close()
    token = body["token"]  # the ledger holds the claim, never a token
    assert token not in stored and token.split(".")[2] not in stored


def test_the_launching_user_is_never_the_principal(ws, s3, on, signer):
    """Amendment 10: eval traffic carries no user identity — a user handed in by an older
    caller is ignored, the job still acts as System::"eval"."""
    agent = _agent()
    assert _prepare(ws, agent, _job(), principal=USER) is True
    assert _claims(_file(s3)["token"], signer, agent.id)["p"] == SYSTEM_EVAL
    assert eval_tokens.system_principal(USER, "eval").claim() == SYSTEM_EVAL
    assert eval_tokens.system_principal(None, "canary").claim()["id"] == "canary"
    assert eval_tokens.system_principal(Principal.system("hub"), "eval").claim()["id"] == "eval"


def test_canary_traffic_acts_as_system_canary(ws, s3, on, signer):
    agent = _agent()
    job = _job("trf-1")
    eval_tokens.prepare(ws, agent, job, principal=Principal.system("canary"),
                        channel="canary", owner_kind=eval_tokens.OWNER_CANARY,
                        owner_id="cny-1")
    claims = _claims(_file(s3, "trf-1")["token"], signer, agent.id)
    assert claims["ch"] == "canary" and claims["p"]["kind"] == "system"
    assert claims["p"]["id"] == "canary" and claims["sid"] == "trf-1"


def test_the_audience_is_the_target_and_its_a2a_peers(ws, s3, on, signer):
    peer = _agent(name="peer", arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/peer")
    agent = _agent(tools=[{"type": "a2a", "name": "peer",
                           "config": {"runtime_arn": peer.arn, "agent_id": peer.id}}])
    _prepare(ws, agent, _job())
    assert _claims(_file(s3)["token"], signer, agent.id)["aud"] == [agent.id, peer.id]


def test_a_job_without_sessions_mints_nothing(ws, s3, on):
    job = _job(sids=())
    assert _prepare(ws, _agent(), job) is False and s3.calls == []
    assert eval_tokens.SPEC_FLAG not in job.spec


def test_no_signer_writes_no_file_but_marks_the_job(ws, s3, on, monkeypatch, caplog):
    """The run then goes token-less: the worker finds no file and the Gateway decides."""
    def boom():
        raise RuntimeError("KMS down")

    monkeypatch.setattr(principal_mint, "_signer", boom)
    caplog.set_level(logging.WARNING, logger="launchpad.policy")
    job = _job()
    assert _prepare(ws, _agent(), job) is True
    assert s3.objects == {} and eval_tokens.SPEC_FLAG in job.spec
    assert "eval job run-1 minted no principal token (RuntimeError)" in caplog.text


def test_a_kms_failure_while_minting_opens_only_the_eval_circuit(ws, s3, on, caplog):
    """N17-obs493 (re-review F4): the eval mint is behind ITS OWN 60 s circuit per workspace —
    a Sign failure trips it (the next eval job in the window makes no KMS call) and the
    interactive path (a hub Gateway call here) still mints."""
    calls: list[str] = []
    real_sign = on.sign

    def sign(**kw):
        calls.append("sign")
        if len(calls) == 1:
            raise RuntimeError("KMSInternalException")  # one throttled / failed Sign
        return real_sign(**kw)

    on.sign = sign
    caplog.set_level(logging.WARNING, logger="launchpad.policy")
    job = _job(sids=(S1, S2, S3))
    assert _prepare(ws, _agent(), job) is True
    assert calls == ["sign"] and s3.objects == {} and eval_tokens.SPEC_FLAG in job.spec
    assert "eval job run-1 minted no principal token (policy.principal_unavailable)" in caplog.text
    assert principal_mint.circuit_open(principal_mint.PATH_EVAL, DEFAULT_WORKSPACE_ID)
    assert not principal_mint.circuit_open(principal_mint.PATH_TURN, DEFAULT_WORKSPACE_ID)
    # the next eval job in the window: no KMS call at all
    _prepare(ws, _agent(name="governed-2"), _job(job_id="run-2", sids=(S1,)))
    assert calls == ["sign"]
    # the interactive path of the SAME workspace is untouched: it mints
    assert principal_mint.mint_for_hub_call(DEFAULT_WORKSPACE_ID, None) is not None
    assert calls == ["sign", "sign"]
    # another workspace's eval path is untouched too
    assert not principal_mint.circuit_open(principal_mint.PATH_EVAL, "ws-other")


def test_an_interactive_sign_failure_does_not_block_the_eval_mint(ws, s3, on):
    """The other direction of N17-obs493: a turn's Sign failure opens the TURN circuit of its
    workspace; the eval job of the same workspace still mints its token."""
    calls: list[str] = []
    real_sign = on.sign

    def sign(**kw):
        calls.append("sign")
        if len(calls) == 1:
            raise RuntimeError("KMSInternalException")
        return real_sign(**kw)

    on.sign = sign
    assert principal_mint.mint_for_hub_call(DEFAULT_WORKSPACE_ID, None) is None
    assert principal_mint.circuit_open(principal_mint.PATH_TURN, DEFAULT_WORKSPACE_ID)
    assert principal_mint.mint_for_hub_call(DEFAULT_WORKSPACE_ID, None) is None
    assert calls == ["sign"]  # the turn circuit is open: no KMS call
    assert _prepare(ws, _agent(), _job(sids=(S1, S2))) is True
    assert _file(s3)["token"] and calls == ["sign", "sign"]


def test_a_key_resolution_failure_is_seen_by_every_path(ws, s3, on, monkeypatch):
    """One hub key: a DescribeKey failure stays hub-global (the eval job and a turn both
    mint nothing, without a second KMS call), unlike a Sign failure."""
    describes: list[str] = []

    def describe(**_):
        describes.append("describe_key")
        raise RuntimeError("AccessDeniedException")

    on.describe_key = describe
    principal_mint.reset_signer()
    assert _prepare(ws, _agent(), _job(sids=(S1,))) is True and s3.objects == {}
    assert principal_mint.mint_for_hub_call(DEFAULT_WORKSPACE_ID, None) is None
    assert principal_mint.circuit_open(principal_mint.PATH_EVAL, "ws-other")
    assert describes == ["describe_key"]


# ── hub: refresh / delete / sweep ─────────────────────────────────────────


def test_a_refresh_keeps_the_policy_session_and_renews_only_jti_iat_exp(ws, s3, on, signer,
                                                                        monkeypatch):
    agent = _agent()
    _prepare(ws, agent, _job())
    first = _claims(_file(s3)["token"], signer, agent.id)
    monkeypatch.setattr(time, "time", lambda: first["iat"] + 301.0)
    assert eval_tokens.refresh(ws, "run-1") is True
    second = _claims(_file(s3)["token"], signer, agent.id, now=first["iat"] + 302.0)
    same = ("sid", "psid", "tsv", "p", "ch", "ws", "agt", "aud")
    assert {k: second[k] for k in same} == {k: first[k] for k in same}
    assert second["jti"] != first["jti"] and second["exp"] > first["exp"]


def test_a_failed_refresh_keeps_the_old_token_and_retries(ws, s3, on, monkeypatch):
    from app.services import principal_token

    _prepare(ws, _agent(), _job())
    before = _file(s3)["token"]
    db = SessionLocal()
    try:
        written = db.get(EvalTokenFile, "run-1").written_at
    finally:
        db.close()

    def boom(**kw):
        raise principal_token.PrincipalTokenUnavailable("policy.principal_invalid", "x")

    monkeypatch.setattr(principal_token, "build_claims", boom)
    assert eval_tokens.refresh(ws, "run-1") is False
    assert _file(s3)["token"] == before  # the old token stays until it expires on its own
    db = SessionLocal()
    try:
        assert db.get(EvalTokenFile, "run-1").written_at == written  # retried next poll
    finally:
        db.close()


def test_tick_refreshes_only_when_due_and_deletes_a_finished_job(ws, s3, on):
    _prepare(ws, _agent(), _job())
    puts = lambda: [c for c in s3.calls if c[0] == "put"]  # noqa: E731
    now = datetime.now(UTC)
    eval_tokens.tick(ws, [("run-1", False)], now=now + timedelta(seconds=100))
    assert len(puts()) == 1  # not due yet
    eval_tokens.tick(ws, [("run-1", False)], now=now + timedelta(seconds=301))
    assert len(puts()) == 2
    eval_tokens.tick(ws, [("run-1", True)])
    assert eval_tokens.tokens_key("run-1") not in s3.objects
    db = SessionLocal()
    try:
        assert db.get(EvalTokenFile, "run-1") is None
    finally:
        db.close()
    eval_tokens.tick(ws, [("run-1", False)], now=now + timedelta(hours=1))
    assert len(puts()) == 2  # nothing left to refresh


def test_a_byoc_target_is_refreshed_at_half_its_token_life():
    assert eval_tokens.refresh_s("zip_runtime") == 300
    assert eval_tokens.refresh_s("byoc") == 150


def test_the_startup_sweep_deletes_what_no_watcher_will_refresh(ws, s3, on, monkeypatch):
    from app.services import workspace as workspace_service

    monkeypatch.setattr(workspace_service, "context_for_workspace", lambda _id: ws)
    agent = _agent()
    for job_id, owner in (("run-live", "run-live"), ("run-done", "run-done")):
        eval_tokens.prepare(ws, agent, _job(job_id), channel="eval",
                            owner_kind=eval_tokens.OWNER_EVAL_RUN, owner_id=owner)
    eval_tokens.prepare(ws, agent, _job("trf-1"), principal=Principal.system("canary"),
                        channel="canary", owner_kind=eval_tokens.OWNER_CANARY,
                        owner_id="cny-1")
    swept = eval_tokens.sweep_on_startup(
        owner_terminal=lambda kind, oid: kind == eval_tokens.OWNER_CANARY or oid == "run-done")
    assert sorted(swept) == ["run-done", "trf-1"]
    assert set(s3.objects) == {eval_tokens.tokens_key("run-live")}


def test_the_service_sweep_keeps_a_run_reconcile_will_resume(ws, s3, on, monkeypatch):
    from app.evaluation import service
    from app.evaluation.models import EvalRun

    db = SessionLocal()
    try:
        for rid, status in (("r-live", "evaluating"), ("r-done", "completed")):
            db.add(EvalRun(id=rid, workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a",
                           agent_name="a", mode="evaluators", evaluators=[], status=status,
                           batch_eval_id=rid, session_ids=[]))
        db.commit()
    finally:
        db.close()
    assert service._token_owner_terminal("eval_run", "r-live") is False
    assert service._token_owner_terminal("eval_run", "r-done") is True
    assert service._token_owner_terminal("eval_run", "r-gone") is True
    assert service._token_owner_terminal("canary", "anything") is True


def test_the_terminal_delete_removes_every_version(ws, s3, on):
    _prepare(ws, _agent(), _job())
    eval_tokens.refresh(ws, "run-1")
    eval_tokens.refresh(ws, "run-1")
    assert len(s3.versions[eval_tokens.tokens_key("run-1")]) == 3
    eval_tokens.discard(ws, "run-1")
    assert not s3.versions[eval_tokens.tokens_key("run-1")]
    assert eval_tokens.tokens_key("run-1") not in s3.objects


def test_a_canary_traffic_submit_that_fails_still_discards_its_token(ws, s3, on):
    from app.optimization import traffic

    agent = _agent()
    job = _job("trf-fail")
    eval_tokens.prepare(ws, agent, job, principal=Principal.system("canary"),
                        channel="canary", owner_kind=eval_tokens.OWNER_CANARY,
                        owner_id="cny-1")
    assert eval_tokens.tokens_key("trf-fail") in s3.objects

    class Boom:
        def submit(self, *_a):
            raise RuntimeError("worker unreachable")

    with pytest.raises(RuntimeError):
        traffic.run(ws, types.SimpleNamespace(kind="canary", id="cny-1"), job, {},
                    executor=Boom())
    assert eval_tokens.tokens_key("trf-fail") not in s3.objects


# ── worker: every session carries the job's token, or the turn goes token-less ────────


def _worker_token(exp: float, tag: str = "job") -> str:
    import base64

    def seg(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'ES256'})}.{seg({'sid': tag, 'exp': exp})}.c2ln"


class _TokenSink(Sink):
    def __init__(self, files: dict[str, bytes]):
        super().__init__()
        self.job.job_id = "j-batch_eval"  # the job the token file names (= _job's id)
        self.files = files
        self.reads: list[str] = []

    def read_object(self, key):
        self.reads.append(key)
        return self.files.get(key)


KEY = "evaluations/j-batch_eval/tokens.json"


def _tokens_file(token: str, job_id="j-batch_eval") -> bytes:
    return json.dumps({"schema": worker_tokens.SCHEMA, "job_id": job_id,
                       "written_at": "2026-10-26T20:00:00Z", "token": token}).encode()


def _v1_file(tokens: dict[str, str], job_id="j-batch_eval") -> bytes:
    return json.dumps({"schema": worker_tokens.SCHEMA_V1, "job_id": job_id,
                       "written_at": "2026-10-26T20:00:00Z", "tokens": tokens}).encode()


def _governed_spec() -> dict:
    return _spec(**{worker_tokens.SPEC_FLAG: {"key": KEY}})


def _spy(h) -> dict[str, str | None]:
    seen: dict[str, str | None] = {}
    inner = h.env.invoke

    def spy(prompt, session_id, session_spec):
        seen[session_id] = (session_spec.get("launchpad_meta") or {}).get("principal")
        return inner(prompt, session_id, session_spec)

    h.env.invoke = spy
    return seen


def test_the_worker_sends_every_session_the_jobs_one_token(harness):
    from tests.evaluation.test_jobs_replay_failures import _job

    h = harness({})
    seen = _spy(h)
    token = _worker_token(time.time() + 900)
    sink = _TokenSink({KEY: _tokens_file(token)})
    job = _job(_governed_spec())
    jobs_batch.run_job(job, sink, h.env)
    assert seen == {S1: token, S2: token, S3: token}
    assert len(sink.reads) == 1  # read once, held for the run
    assert all(s.get("launchpad_meta") is None for s in job.spec["sessions"])  # copies only
    dumped = json.dumps([sink.invocations, sink.result_rows, sink.artifacts,
                         sink.finished], default=str)
    assert token not in dumped  # never in results / artefacts


def test_a_job_without_a_readable_token_runs_token_less_and_never_stops_a_sample(harness):
    """Amendment 10: no file (the hub could not mint) ⇒ the turns go without a principal; the
    Gateway decides (a preset passes, an ordinary agent is principal_missing there and that
    shows in Decisions) — the worker records nothing as failed on its own."""
    from tests.evaluation.test_jobs_replay_failures import _job

    h = harness({})
    seen = _spy(h)
    sink = _TokenSink({})
    jobs_batch.run_job(_job(_governed_spec()), sink, h.env)
    assert seen == {S1: None, S2: None, S3: None}
    assert [c[0] for c in h.runtime.calls] == [S1, S2, S3]  # every session invoked
    assert {r["status"] for r in sink.invocations} == {"ok"}
    assert "principal_missing" not in json.dumps([sink.invocations, sink.result_rows])
    assert sink.finished[0] == "COMPLETED"


def test_a_v1_per_session_file_is_still_honoured_per_session(harness):
    """Rollover read-compat: a job prepared by a hub before amendment 10 holds one token per
    session; the worker sends each session its own, a session missing from it goes token-less."""
    from tests.evaluation.test_jobs_replay_failures import _job

    h = harness({})
    seen = _spy(h)
    exp = time.time() + 900
    toks = {S1: _worker_token(exp, S1), S3: _worker_token(exp, S3)}
    sink = _TokenSink({KEY: _v1_file(toks)})
    jobs_batch.run_job(_job(_governed_spec()), sink, h.env)
    assert seen == {S1: toks[S1], S2: None, S3: toks[S3]}
    assert [c[0] for c in h.runtime.calls] == [S1, S2, S3]


def test_a_job_without_the_flag_is_sent_exactly_as_before(harness):
    from tests.evaluation.test_jobs_replay_failures import _job

    h = harness({})
    seen = []
    inner = h.env.invoke
    h.env.invoke = lambda p, sid, spec: (seen.append(spec), inner(p, sid, spec))[1]
    sink = _TokenSink({})
    job = _job(_spec())
    jobs_batch.run_job(job, sink, h.env)
    assert sink.reads == [] and all(s is orig for s, orig in zip(
        seen, job.spec["sessions"], strict=True))


def test_the_worker_rereads_when_the_held_token_runs_low():
    now = [1_000.0]
    files = {"k": _tokens_file(_worker_token(1_000.0 + 300), "j")}
    reads: list[str] = []

    def read(key):
        reads.append(key)
        return files.get(key)

    held = worker_tokens.JobTokens(read, "k", "j", clock=lambda: now[0])
    first = held.token_for(S1)
    assert first and held.token_for(S2) == first and len(reads) == 1  # cached while > 120 s left
    now[0] += 200  # 100 s left: re-read; the hub has re-signed the file meanwhile
    files["k"] = _tokens_file(_worker_token(now[0] + 900), "j")
    renewed = held.token_for(S1)
    assert renewed != first and len(reads) == 2
    now[0] += 2_000  # expired and the file is gone: the turn goes token-less, never stops
    files.clear()
    assert held.token_for(S1) is None


def test_a_file_of_another_job_is_ignored():
    held = worker_tokens.JobTokens(
        lambda _k: _tokens_file(_worker_token(time.time() + 900), "other-job"), "k", "j")
    assert held.token_for(S1) is None


def test_the_worker_key_and_schema_match_the_contract():
    assert worker_tokens.SCHEMA == eval_tokens.SCHEMA == "launchpad.policy.eval-tokens/2"
    assert worker_tokens.SPEC_FLAG == eval_tokens.SPEC_FLAG
    assert eval_tokens.tokens_key("j1") == contracts.EVAL_TOKENS_KEY.format(job_id="j1")
    assert worker_tokens.REREAD_MARGIN_S == 120
    assert not hasattr(worker_tokens, "PrincipalMissing")  # a missing token stops nothing


def test_the_worker_library_ships_the_token_reader():
    from app.templates import eval_worker

    files = eval_worker.library_files()
    assert f"{eval_worker.LIBRARY_PACKAGE}/principal_tokens.py" in files
    assert "from app." not in files[f"{eval_worker.LIBRARY_PACKAGE}/principal_tokens.py"]


# ── hub: the eval run's watcher (service.execute_run) ─────────────────────


@pytest.fixture
def run_harness(monkeypatch):
    import app.evaluation.service as svc
    from tests.evaluation.fakes import FakeExecutor

    fake = FakeExecutor()
    seen: list[tuple[dict, bool]] = []
    real_submit = fake.submit

    def submit(ws_, job):
        seen.append((dict(job.spec), eval_tokens.tokens_key(job.job_id) in _S3_REF[0].objects))
        fake.complete(job.job_id, [])  # the worker is done the moment the poller looks
        return real_submit(ws_, job)

    fake.submit = submit
    monkeypatch.setattr(svc, "select_executor", lambda settings, job_type: fake)
    monkeypatch.setattr(svc, "POLL_INTERVAL_S", 0.0)
    monkeypatch.setattr(svc, "_sleep", lambda s: None)
    return svc, seen


_S3_REF: list[_S3] = []


def _execute(svc, ws, agent_id: str) -> str:
    from tests.evaluation.test_resume import make_run

    run_id = make_run(status="queued", mode="evaluators", agent_id=agent_id,
                      created_by=None, created_by_name="demo-admin")
    plan = svc.RunPlan(run_id=run_id, job_type="batch_eval", target={},
                       sessions=[{"session_id": S1, "prompts": ["hi"]}], replay=True, wait={},
                       agent_id=agent_id)
    svc.execute_run(run_id, workspace=ws, plan=plan)
    return run_id


def test_a_run_hands_its_job_the_system_token_before_dispatch_and_deletes_it_after(
        ws, s3, on, run_harness, signer):
    svc, seen = run_harness
    _S3_REF[:] = [s3]
    agent = _agent()
    written: list[dict] = []
    real_put = s3.put_object

    def put(**kw):
        real_put(**kw)
        if kw["Key"].endswith("tokens.json"):
            written.append(json.loads(kw["Body"]))

    s3.put_object = put
    run_id = _execute(svc, ws, agent.id)
    [(spec, file_existed)] = seen
    assert file_existed and spec[eval_tokens.SPEC_FLAG] == {
        "key": f"evaluations/{run_id}/tokens.json"}
    [body] = written
    claims = _claims(body["token"], signer, agent.id)
    assert claims["p"] == SYSTEM_EVAL and claims["sid"] == run_id  # the launching user is not
    assert eval_tokens.tokens_key(run_id) not in s3.objects  # terminal → deleted
    db = SessionLocal()
    try:
        assert db.get(EvalTokenFile, run_id) is None
    finally:
        db.close()


def test_a_run_with_the_switch_off_is_dispatched_exactly_as_before(ws, s3, monkeypatch, kms,
                                                                   run_harness):
    svc, seen = run_harness
    _S3_REF[:] = [s3]
    _switches(global_on=False, workspace_on=False, monkeypatch=monkeypatch)
    _execute(svc, ws, _agent().id)
    [(spec, file_existed)] = seen
    assert eval_tokens.SPEC_FLAG not in spec and not file_existed and s3.calls == []


@pytest.mark.parametrize("env", ["", "t1"])
def test_the_bucket_deny_admits_the_eval_worker_role_the_hub_creates(env):
    """infra/stacks/base_stack.py EvalTokensReadersOnly names the worker role by pattern;
    the role the hub actually creates (agent_iam.role_name_for over the preset's name and a
    uuid4-hex agent id) must match it, or the worker could not read its own tokens."""
    import fnmatch
    import uuid

    from app.core import naming
    from app.services.agent_iam import role_name_for
    from app.system_agents.presets import EVAL_WORKER_BASE_NAME

    preset = naming.suffixed(EVAL_WORKER_BASE_NAME, env)
    role = role_name_for(preset, uuid.uuid4().hex, env_name=env)
    pattern = ("launchpad-agent-launchpad-eval-worker-????????" if not env
               else f"launchpad-agent-launchpad-eval-worker-{env}-????????-{env}")
    assert fnmatch.fnmatchcase(role, pattern), (role, pattern)
