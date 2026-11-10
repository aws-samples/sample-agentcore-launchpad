"""memory-extractor Lambda (N6): driven in-process with fakes — no AWS, no network."""

import dataclasses
import importlib.util
import json
import logging
import sys
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from app.services import opensearch_http as core
from tests.memory_fakes import FakeBedrockRuntime, FakeEmbedder, FakeOpenSearch, FakeS3

LAMBDA_DIR = Path(__file__).resolve().parents[1] / "lambdas" / "memory_extractor"
T0 = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)
BUCKET = "bkt"
STORE = "launchpad_memory-default"
ACTOR = "agent1__river"


@pytest.fixture(scope="module")
def handler():
    # `import opensearch_http` inside handler.py resolves the same way it does in Lambda.
    sys.path.insert(0, str(LAMBDA_DIR))
    spec = importlib.util.spec_from_file_location(
        "memory_extractor_handler", LAMBDA_DIR / "handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    sys.path.remove(str(LAMBDA_DIR))


def _deps(handler, outputs):
    emb, os_ = FakeEmbedder(), FakeOpenSearch()
    for name, body in (
        (core.memory_index(""), core.memory_index_body()),
        (core.jobs_index(""), core.jobs_index_body()),
        (core.kb_docs_index(""), core.kb_docs_index_body()),
    ):
        os_.ensure_index(name, body)
    return handler.Deps(
        s3=FakeS3(),
        opensearch=os_,
        bedrock=FakeBedrockRuntime(emb, outputs),
        embedder=emb,
        now=lambda: T0,
        env={"extract_model": "global.zai.glm-5.3", "context_events": 4},
    )


def _event(deps, prompt, answer, session="sess-1", ts=T0):
    ev = core.turn_event(ACTOR, session, prompt, answer, ts)
    event_name = core.event_key(ts, ev["eventId"][:8])
    key = f"memory/{STORE}/{ACTOR}/session_{session}/events/{event_name}"
    deps.s3.put_object(Bucket=BUCKET, Key=key, Body=json.dumps(ev).encode())
    return key, ev


# The default `actor_summary_interval` is 1 (memory recall freshness, 2026-10-29): a test that
# scripts exactly three model outputs (facts, preferences, summary) pins the roll out with this.
NO_ROLL = {"actor_summary_interval": 5}


def _msg(key, long_term=True, merge=None):
    body = {
        "kind": "memory_extract",
        "store_id": STORE,
        "workspace_id": "default",
        "actor_id": ACTOR,
        "session_id": "sess-1",
        "event_key": key,
        "long_term": long_term,
        "bucket": BUCKET,
    }
    if merge is not None:
        body["merge"] = merge
    return body


def test_memory_extract_writes_facts_preferences_and_a_summary(handler):
    deps = _deps(
        handler,
        [
            '["User likes green tea", "User lives in Ningxia"]',
            '[{"preference": "concise answers", "category": "style"}]',
            "Chatted about tea.",
        ],
    )
    key, ev = _event(deps, "I like green tea and live in Ningxia. Keep answers short.", "Noted!")
    out = handler.process_message(_msg(key), deps)
    assert out["status"] == "COMPLETED"
    # r14-a: the v2 counters are additive; a first extraction adds everything and merges nothing
    assert out["counts"] == {
        "facts": 2, "preferences": 1, "summaries": 1, "duplicates": 0,
        "updates": 0, "supersedes": 0, "deletes": 0, "noops": 0,
        "consolidated": 0, "expired": 0, "actor_summary": 0,
    }
    by_ns = {}
    for d in deps.opensearch.indices[core.memory_index("")].values():
        by_ns.setdefault(d["namespace"], []).append(d)
    assert sorted(x["text"] for x in by_ns[f"/facts/{ACTOR}"]) == [
        "User likes green tea",
        "User lives in Ningxia",
    ]
    pref = by_ns[f"/preferences/{ACTOR}"][0]
    assert json.loads(pref["text"]) == {"preference": "concise answers", "category": "style"}
    assert pref["strategy"] == "user_preference"
    summ = by_ns[f"/summaries/{ACTOR}/sess-1"][0]
    assert json.loads(summ["text"]) == {"summary": "Chatted about tea."}
    assert summ["session_id"] == "sess-1"
    for d in deps.opensearch.indices[core.memory_index("")].values():
        assert d["store_id"] == STORE
        assert d["workspace_id"] == "default"
        assert d["actor_id"] == ACTOR
        assert len(d["embedding"]) == 16
        assert d["source_event_ids"] == [ev["eventId"]]
        assert d["created_at"] == T0.isoformat()
        # r14-a: every new record is written ACTIVE with the v2 fields
        assert d["updated_at"] == T0.isoformat() and d["last_op"] == "add"
        assert 0.0 < d["confidence"] <= 1.0 and core.record_is_active(d)
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "COMPLETED"
    assert job["kind"] == "memory_extract"
    assert job["strategy_ids"] == ["semantic", "user_preference", "summarization"]
    assert job["actor_id"] == ACTOR
    assert job["session_id"] == "sess-1"
    assert job["updated_at"] == T0.isoformat()
    # C3: GLM, maxTokens >= 256; the reasoningContent block was ignored.
    for call in deps.bedrock.converse_calls:
        assert call["modelId"] == "global.zai.glm-5.3"
        assert call["inferenceConfig"]["maxTokens"] >= 256


def test_unanswered_turn_is_skipped_without_a_model_call(handler):
    """N17-obs304: an event whose ASSISTANT part is empty (a failed turn written before the
    runtime fix) is SKIPPED — no model call, no fact, preference or summary record."""
    deps = _deps(handler, ["[]", "[]", "The lodging cap is 1100 yuan for ministers."])
    key, _ = _event(deps, "去上海出差住宿标准是多少？", "")
    out = handler.process_message(_msg(key), deps)
    assert out["status"] == "SKIPPED"
    assert deps.bedrock.converse_calls == []
    assert not deps.opensearch.indices[core.memory_index("")]
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "SKIPPED" and "no assistant answer" in job["skip_reason"]
    # whitespace-only is no answer either
    key2, _ = _event(deps, "你好", " \n ", ts=T0.replace(minute=1))
    assert handler.process_message(_msg(key2), deps)["status"] == "SKIPPED"
    assert deps.bedrock.converse_calls == []


def test_unanswered_context_turns_never_reach_the_prompt(handler):
    """N17-obs304: an earlier unanswered turn in the context window is dropped from the
    transcript, so the summarizer is never handed a bare question to answer."""
    deps = _deps(handler, ["[]", "[]", "Chatted about tea."])
    _event(deps, "去上海出差住宿标准是多少？", "", ts=T0)
    key, _ = _event(deps, "I like tea", "Noted.", ts=T0.replace(minute=1))
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert out["status"] == "COMPLETED"
    prompts = [c["messages"][0]["content"][0]["text"] for c in deps.bedrock.converse_calls]
    assert prompts and all("住宿标准" not in p for p in prompts)
    assert all("USER: I like tea" in p for p in prompts)


def test_extraction_prompts_forbid_answering_or_outside_knowledge(handler):
    """N17-obs304: every extraction/summary prompt records only what was said."""
    for prompt in (handler.SUMMARY_PROMPT, handler.ACTOR_SUMMARY_PROMPT,
                   handler.FACTS_PROMPT, handler.PREFERENCES_PROMPT):
        assert "Never answer the user's questions" in prompt
        assert "outside knowledge" in prompt


def test_duplicates_are_skipped_at_cosine_0_92(handler):
    deps = _deps(handler, ['["User likes green tea"]', "[]", "s"])
    key, _ = _event(deps, "tea", "ok")
    handler.process_message(_msg(key), deps)
    deps.bedrock.outputs = ['["User likes green tea"]', "[]", "s2"]
    key2, _ = _event(deps, "tea again", "ok", ts=T0.replace(minute=1))
    out = handler.process_message(_msg(key2), deps)
    assert out["counts"]["facts"] == 0
    assert out["counts"]["duplicates"] == 1
    docs = list(deps.opensearch.indices[core.memory_index("")].values())
    assert len([d for d in docs if d["strategy"] == "semantic"]) == 1
    # summary upserted, not duplicated
    assert len([d for d in docs if d["strategy"] == "summarization"]) == 1


def test_redelivery_of_a_completed_event_is_a_noop(handler):
    deps = _deps(handler, ['["A"]', "[]", "s"])
    key, _ = _event(deps, "a", "b")
    first = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    again = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert again == {
        "job_id": first["job_id"],
        "status": "COMPLETED",
        "skipped": "already completed",
    }
    assert len(deps.bedrock.converse_calls) == 3


def test_short_term_only_messages_record_a_skipped_job(handler):
    deps = _deps(handler, [])
    key, _ = _event(deps, "a", "b")
    out = handler.process_message(_msg(key, long_term=False), deps)
    assert out["status"] == "SKIPPED"
    assert deps.bedrock.converse_calls == []


def test_model_garbage_fails_soft_per_strategy(handler):
    deps = _deps(handler, ["not json at all", '[{"preference": "x"}]', "sum"])
    key, _ = _event(deps, "a", "b")
    out = handler.process_message(_msg(key), deps)
    assert out["status"] == "COMPLETED"
    assert out["counts"]["facts"] == 0
    assert out["counts"]["preferences"] == 1
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert "semantic" in job["warnings"][0]


def test_a_missing_event_file_fails_the_job_and_is_dropped_not_retried(handler):
    """N11-r3-bug4: the agent's memory snapshots are deleted with the agent (r2-B
    teardown); a turn enqueued just before has nothing left to read. Redelivering it
    could never succeed — record the failure, acknowledge, drop."""
    deps = _deps(handler, [])
    with pytest.raises(handler.DroppedMessage):
        handler.process_message(
            _msg(f"memory/{STORE}/{ACTOR}/session_sess-1/events/nope.json"), deps
        )
    (job,) = deps.opensearch.indices[core.jobs_index("")].values()
    assert job["status"] == "FAILED"
    assert "nope.json" in job["failure_reason"] and "dropped" in job["failure_reason"]


class _FlakyS3(FakeS3):
    """A transient S3 error (SlowDown) for one key — the retryable case."""

    def get_object(self, *, Bucket, Key, **kw):
        if Key.endswith("slow.json"):
            raise ClientError({"Error": {"Code": "SlowDown", "Message": "busy"}}, "GetObject")
        return super().get_object(Bucket=Bucket, Key=Key, **kw)


def test_lambda_handler_reports_partial_batch_failures(handler, monkeypatch, caplog):
    deps = _deps(handler, ['["A"]', "[]", "s"])
    flaky = _FlakyS3()
    flaky.objects = deps.s3.objects
    deps.s3 = flaky
    key, _ = _event(deps, "a", "b")
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    event = {
        "Records": [
            {"messageId": "m1", "body": json.dumps(_msg(key))},
            {"messageId": "m2", "body": json.dumps(_msg(f"memory/{STORE}/{ACTOR}/s/slow.json"))},
            {"messageId": "m3", "body": "not json"},
            {"messageId": "m4", "body": json.dumps(_msg("memory/x/y/session_z/events/gone.json"))},
        ]
    }
    # m2 is transient -> retried; m3 is poison and m4's agent is gone -> both dropped.
    assert handler.lambda_handler(event, None) == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    # N11-r3-bug4: exactly one line per message, naming id / kind / job / outcome / duration
    lines = {r.getMessage().split()[0]: r for r in caplog.records
             if r.name == "launchpad.memory_extractor"}
    assert set(lines) == {"message=m1", "message=m2", "message=m3", "message=m4"}
    outcomes = {k: dict(p.split("=", 1) for p in r.getMessage().split(" detail=")[0].split())
                for k, r in lines.items()}
    assert outcomes["message=m1"]["outcome"] == "ok"
    assert outcomes["message=m1"]["kind"] == "memory_extract"
    assert outcomes["message=m1"]["job"] == handler.message_job_id(_msg(key))
    assert outcomes["message=m2"]["outcome"] == "retry"
    assert outcomes["message=m3"]["outcome"] == "poison"
    assert outcomes["message=m4"]["outcome"] == "dropped"
    assert all("duration_ms" in o for o in outcomes.values())
    assert lines["message=m1"].levelno == logging.INFO
    assert lines["message=m4"].levelno == logging.WARNING
    assert lines["message=m3"].levelno == logging.ERROR


def _kb_msg(index="launchpad-kb-kb1", bucket=BUCKET):
    return {
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
        "job_id": "ij-gone", "bucket": bucket, "prefix": "kb/KB1/DS1/", "index": index,
    }


def test_kb_ingest_for_a_deleted_kb_is_acked_and_dropped_at_warning(handler, monkeypatch, caplog):
    """N11-r3-bug4 (live): KBEXAMPLE00004 was deleted, its index with it; the stray
    kb_ingest message failed silently three times and landed in the DLQ."""
    deps = _deps(handler, [])
    deps.s3.put_object(Bucket=BUCKET, Key="kb/KB1/DS1/policy.md", Body=b"text")
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    out = handler.lambda_handler(
        {"Records": [{"messageId": "k1", "body": json.dumps(_kb_msg(index="launchpad-kb-gone"))}]},
        None,
    )
    assert out == {"batchItemFailures": []}  # acknowledged, not retried
    job = deps.opensearch.get_doc(core.jobs_index(""), "ij-gone")
    assert job["status"] == "FAILED" and "KB1" in job["failure_reason"]
    assert "launchpad-kb-gone" in job["failure_reason"]
    [line] = [r for r in caplog.records if r.name == "launchpad.memory_extractor"]
    assert line.levelno == logging.WARNING
    assert "outcome=dropped" in line.getMessage() and "job=ij-gone" in line.getMessage()
    assert "kind=kb_ingest" in line.getMessage()


def test_kb_ingest_for_a_vanished_bucket_is_dropped_but_other_s3_errors_retry(handler):
    class GoneBucketS3(FakeS3):
        def __init__(self, code):
            super().__init__()
            self.code = code

        def list_objects_v2(self, **kw):
            raise ClientError({"Error": {"Code": self.code, "Message": "x"}}, "ListObjectsV2")

    deps = _deps(handler, [])
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    deps.s3 = GoneBucketS3("NoSuchBucket")
    with pytest.raises(handler.DroppedMessage):
        handler.process_message(_kb_msg(), deps)
    assert "dropped" in deps.opensearch.get_doc(core.jobs_index(""), "ij-gone")["failure_reason"]

    deps.s3 = GoneBucketS3("SlowDown")  # transient → still retried
    with pytest.raises(handler.RetryableError):
        handler.process_message(_kb_msg(), deps)


def test_kb_ingest_chunks_embeds_and_records_document_status(handler):
    deps = _deps(handler, [])
    prefix = "kb/KB1/DS1/"
    policy_body = ("Vacation policy. " * 200).encode()
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "policy.md", Body=policy_body)
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "deck.pptx", Body=b"binary")
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "guide.pdf", Body=b"%PDF")
    deps.s3.put_object(
        Bucket=BUCKET, Key=prefix + "_text/guide.pdf.txt", Body=b"Guide text about expenses."
    )
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    deps.opensearch.put_doc(
        core.jobs_index(""),
        "ij1",
        {
            "job_id": "ij1",
            "kind": "kb_ingest",
            "kb_id": "KB1",
            "ds_id": "DS1",
            "status": "STARTING",
            "counts": {},
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
        },
    )
    out = handler.process_message(
        {
            "kind": "kb_ingest",
            "workspace_id": "default",
            "kb_id": "KB1",
            "ds_id": "DS1",
            "job_id": "ij1",
            "bucket": BUCKET,
            "prefix": prefix,
            "index": "launchpad-kb-kb1",
        },
        deps,
    )
    assert out["status"] == "COMPLETE"
    assert out["counts"] == {
        "numberOfDocumentsScanned": 3,
        "numberOfNewDocumentsIndexed": 2,
        "numberOfDocumentsFailed": 1,
        "numberOfDocumentsUnchanged": 0,  # N17-obs246: always reported
    }
    chunks = deps.opensearch.indices["launchpad-kb-kb1"]
    assert all(
        c["kb_id"] == "KB1" and c["ds_id"] == "DS1" and len(c["embedding"]) == 16 and c["text"]
        for c in chunks.values()
    )
    assert {c["doc_uri"] for c in chunks.values()} == {
        f"s3://{BUCKET}/{prefix}policy.md",
        f"s3://{BUCKET}/{prefix}guide.pdf",
    }
    assert len([c for c in chunks.values() if c["doc_name"] == "policy.md"]) >= 3
    docs = {d["name"]: d for d in deps.opensearch.indices[core.kb_docs_index("")].values()}
    assert docs["policy.md"]["status"] == "INDEXED"
    assert docs["policy.md"]["chunk_count"] >= 3
    assert docs["policy.md"]["size_bytes"] == 3400
    assert docs["guide.pdf"]["status"] == "INDEXED"
    assert docs["deck.pptx"]["status"] == "UNSUPPORTED"
    assert "pptx" in docs["deck.pptx"]["status_reason"]
    assert "guide.pdf.txt" not in docs
    assert deps.opensearch.get_doc(core.jobs_index(""), "ij1")["status"] == "COMPLETE"


class _OrderRecorder:
    """Wraps a FakeOpenSearch to record, in call order, the KB-index bulk writes and
    refreshes and the kb-docs status writes — what N-SCN-obs12 is about."""

    def __init__(self, inner):
        self._inner = inner
        self.events: list[tuple[str, str]] = []

    def bulk(self, index, docs, refresh=True):
        self.events.append(("bulk", index))
        return self._inner.bulk(index, docs, refresh=refresh)

    def request(self, method, path, body=None, params=None):
        if path.endswith("/_refresh"):
            self.events.append(("refresh", path[1:-len("/_refresh")]))
        return self._inner.request(method, path, body, params)

    def put_doc(self, index, doc_id, doc, refresh=True):
        if index == core.kb_docs_index(""):
            self.events.append((f"doc:{doc.get('status')}", doc["name"]))
        return self._inner.put_doc(index, doc_id, doc, refresh=refresh)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_kb_ingest_over_an_empty_prefix_says_so_in_the_counts(handler, caplog):
    """N17-obs204: a listing that returns nothing (or only `_text/` sidecars) completes with
    the three zeros AND `prefix_empty: true`, so the console can name the cause; a prefix
    with one original carries no such key. One INFO line records what the job saw."""
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    deps = _deps(handler, [])
    prefix = "kb/KB1/DS1/"
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "_text/orphan.pdf.txt", Body=b"sidecar only")
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    body = {
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
        "job_id": "ij-empty", "bucket": BUCKET, "prefix": prefix, "index": "launchpad-kb-kb1",
    }
    out = handler.process_message(body, deps)
    assert out["status"] == "COMPLETE"
    assert out["counts"] == {
        "numberOfDocumentsScanned": 0,
        "numberOfNewDocumentsIndexed": 0,
        "numberOfDocumentsFailed": 0,
        "numberOfDocumentsUnchanged": 0,
        "prefix_empty": True,
    }
    stored = deps.opensearch.get_doc(core.jobs_index(""), "ij-empty")
    assert stored["counts"]["prefix_empty"] is True
    messages = [r.getMessage() for r in caplog.records]
    lines = [m for m in messages if m.startswith("kb_ingest job=ij-empty")]
    assert lines == [
        f"kb_ingest job=ij-empty kb=KB1 ds=DS1 prefix=s3://{BUCKET}/{prefix} objects=1 "
        "originals=0 scanned=0 indexed=0 failed=0 unchanged=0 deleted=0 dispatched=0 "
        "full=false prefix_empty=true"
    ]

    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "note.md", Body=b"one original")
    out = handler.process_message({**body, "job_id": "ij-one"}, deps)
    assert out["counts"]["numberOfDocumentsScanned"] == 1 and "prefix_empty" not in out["counts"]
    messages = [r.getMessage() for r in caplog.records]
    [line] = [m for m in messages if m.startswith("kb_ingest job=ij-one")]
    assert line.endswith("objects=2 originals=1 scanned=1 indexed=1 failed=0 unchanged=0 "
                         "deleted=0 dispatched=0 full=false prefix_empty=false")


def test_kb_ingest_refreshes_the_kb_index_before_each_document_turns_indexed(handler):
    """N-SCN-obs12: the chunks are bulk-written without a refresh (obs11) while the
    document record is written with one, so `documents: 2/2 INDEXED` used to be visible
    before the second document's vectors were searchable — a retrieve 1 s later ranked
    only the first document. Now every INDEXED record is preceded by a `_refresh` of the
    KB index issued after that document's bulk, and COMPLETE follows a final refresh."""
    deps = _deps(handler, [])
    prefix = "kb/KB1/DS1/"
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "handbook.md", Body=b"Vacation: 5 days carry.")
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "policy.md", Body=b"Claims within 45 days.")
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "deck.pptx", Body=b"binary")
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    rec = _OrderRecorder(deps.opensearch)
    deps = dataclasses.replace(deps, opensearch=rec)
    out = handler.process_message(
        {"kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
         "job_id": "ij1", "bucket": BUCKET, "prefix": prefix, "index": "launchpad-kb-kb1"},
        deps,
    )
    assert out["status"] == "COMPLETE"
    kb = "launchpad-kb-kb1"
    assert rec.events == [   # S3 lists by key: deck, handbook, policy
        ("doc:UNSUPPORTED", "deck.pptx"),   # nothing indexed → nothing to refresh
        # N17-obs274 pass 4: INDEXING before the old chunks go, INDEXED after the refresh
        ("doc:INDEXING", "handbook.md"),
        ("bulk", kb), ("refresh", kb), ("doc:INDEXED", "handbook.md"),
        ("doc:INDEXING", "policy.md"),
        ("bulk", kb), ("refresh", kb), ("doc:INDEXED", "policy.md"),
        ("refresh", kb),                    # the final refresh before COMPLETE
    ]
    assert deps.opensearch.get_doc(core.jobs_index(""), "ij1")["status"] == "COMPLETE"


def test_kb_reingest_replaces_a_documents_chunks(handler):
    deps = _deps(handler, [])
    prefix = "kb/KB1/DS1/"
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    seed_job = {"job_id": "ij1", "kind": "kb_ingest", "status": "STARTING"}
    deps.opensearch.put_doc(core.jobs_index(""), "ij1", seed_job)
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "a.txt", Body=b"one two three")
    msg = {
        "kind": "kb_ingest",
        "workspace_id": "default",
        "kb_id": "KB1",
        "ds_id": "DS1",
        "job_id": "ij1",
        "bucket": BUCKET,
        "prefix": prefix,
        "index": "launchpad-kb-kb1",
    }
    handler.process_message(msg, deps)
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "a.txt", Body=b"completely new")
    # a re-sync is a new hub job (N17-obs266: the same id would be a finished planner's no-op)
    handler.process_message({**msg, "job_id": "ij2"}, deps)
    texts = [c["text"] for c in deps.opensearch.indices["launchpad-kb-kb1"].values()]
    assert texts == ["completely new"]


def test_bearer_is_resolved_from_the_identity_secret_once(handler, monkeypatch):
    calls = []

    class Control:
        def get_api_key_credential_provider(self, name):
            calls.append(("provider", name))
            return {"apiKeySecretArn": "arn:x:secret"}

    class Secrets:
        def get_secret_value(self, SecretId):
            calls.append(("secret", SecretId))
            return {"SecretString": json.dumps({"api_key": "bearer-123"})}

    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    handler._BEARER.clear()
    assert handler.resolve_bearer(Control(), Secrets(), "launchpad-global-bedrock") == "bearer-123"
    assert handler.resolve_bearer(Control(), Secrets(), "launchpad-global-bedrock") == "bearer-123"
    assert calls == [("provider", "launchpad-global-bedrock"), ("secret", "arn:x:secret")]
    assert handler._secret_to_bearer('{"apiKey": "k"}') == "k"
    assert handler._secret_to_bearer("raw-key") == "raw-key"


def test_resolve_bearer_accepts_the_cn_dict_shaped_secret_arn(handler):
    # N11-r3-bug2 (live cn-northwest-1): apiKeySecretArn arrives as {"secretArn": ...}
    handler._BEARER.clear()
    arn = "arn:aws-cn:secretsmanager:cn-northwest-1:1:secret:x-AbC"

    class Control:
        def get_api_key_credential_provider(self, name):
            return {"name": name, "apiKeySecretArn": {"secretArn": arn}}

    class Secrets:
        def __init__(self):
            self.calls = []

        def get_secret_value(self, SecretId):  # noqa: N803
            self.calls.append(SecretId)
            assert isinstance(SecretId, str)
            return {"SecretString": '{"apiKey": "bearer-123"}'}

    secrets = Secrets()
    assert handler.resolve_bearer(Control(), secrets, "launchpad-global-bedrock") == "bearer-123"
    assert secrets.calls == [arn]
    assert handler.secret_arn_of({"apiKeySecretArn": "arn:plain"}) == "arn:plain"
    with pytest.raises(RuntimeError):
        handler.secret_arn_of({"apiKeySecretArn": {"other": 1}})
    handler._BEARER.clear()


# --- N-SCN-obs11: job-status writes refresh only at the end -------------------------
class _RefreshRecorder:
    """Wraps a FakeOpenSearch so the `refresh` flag of every `put_doc` on the jobs
    index is recorded alongside the status it wrote."""

    def __init__(self, inner):
        self._inner = inner
        self.job_writes: list[tuple[str, bool]] = []

    def put_doc(self, index, doc_id, doc, refresh=True):
        if index == core.jobs_index("") and doc.get("kind") != "kb_ingest_doc_lease":
            self.job_writes.append((doc.get("status"), refresh))  # N17-obs274: leases aside
        return self._inner.put_doc(index, doc_id, doc, refresh=refresh)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_memory_extract_job_status_refreshes_only_on_the_terminal_write(handler):
    """`IN_PROGRESS` is written with `refresh=False` (no forced segment flush on the
    busy node); the terminal `COMPLETED` keeps `refresh=True` so the hub's next poll
    sees it."""
    deps = _deps(handler, ['["likes tea"]', '{"preferences": []}', "Talked about tea."])
    key, _ = _event(deps, "I like tea", "Noted.")
    rec = _RefreshRecorder(deps.opensearch)
    deps = dataclasses.replace(deps, opensearch=rec)
    handler.process_message(_msg(key), deps)
    assert rec.job_writes == [("IN_PROGRESS", False), ("COMPLETED", True)]
    assert handler.NON_TERMINAL_JOB_STATUSES == frozenset({"STARTING", "IN_PROGRESS"})


def test_kb_ingest_job_status_refreshes_only_on_the_terminal_write(handler):
    deps = _deps(handler, [])
    prefix = "kb/KB1/DS1/"
    deps.s3.put_object(Bucket=BUCKET, Key=prefix + "policy.md", Body=b"Vacation policy.")
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    rec = _RefreshRecorder(deps.opensearch)
    deps = dataclasses.replace(deps, opensearch=rec)
    handler.process_message(
        {"kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
         "job_id": "ij1", "bucket": BUCKET, "prefix": prefix, "index": "launchpad-kb-kb1"},
        deps,
    )
    assert rec.job_writes == [("IN_PROGRESS", False), ("COMPLETE", True)]


def test_a_failed_job_write_refreshes_too(handler):
    """FAILED is terminal: the hub must see it on the next poll, not after the next
    background refresh."""
    deps = _deps(handler, [])
    rec = _RefreshRecorder(deps.opensearch)
    deps = dataclasses.replace(deps, opensearch=rec)
    with pytest.raises(handler.DroppedMessage):
        handler.process_message(
            {"kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB9", "ds_id": "DS1",
             "job_id": "ij9", "bucket": BUCKET, "prefix": "kb/KB9/DS1/",
             "index": "launchpad-kb-kb9"},  # index never created → dropped
            deps,
        )
    assert rec.job_writes == [("IN_PROGRESS", False), ("FAILED", True)]


# ── D21 (M1-d): the embedder follows the message's `embedding` descriptor ──
def _titan_block(model_id="amazon.titan-embed-text-v2:0", dimension=1024):
    return {"model_id": model_id, "kind": "bedrock", "base_url": None, "api_model_name": model_id,
            "credential_provider": "launchpad-global-bedrock", "dimension": dimension,
            "max_chars": 8000, "provider": "bedrock", "catalog_version": 7}


def _openai_block(dimension=16):
    return {"model_id": "siliconflow.BAAI/bge-m3", "kind": "openai_compatible",
            "base_url": "https://api.siliconflow.cn/v1", "api_model_name": "BAAI/bge-m3",
            "credential_provider": "launchpad-siliconflow", "dimension": dimension,
            "max_chars": 100, "provider": "siliconflow", "catalog_version": 7}


class _FakeControl:
    def __init__(self):
        self.names = []

    def get_api_key_credential_provider(self, *, name):
        self.names.append(name)
        arn = f"arn:aws-cn:secretsmanager:cn-northwest-1:1:secret:{name}"
        return {"apiKeySecretArn": {"secretArn": arn}}


class _FakeSecrets:
    def get_secret_value(self, *, SecretId):
        return {"SecretString": json.dumps({"api_key": f"key-for-{SecretId.rsplit(':', 1)[1]}"})}


def _fake_post(dimension=16):
    seen = []

    def post(url, headers, body):
        seen.append((url, headers, json.loads(body)))
        return json.dumps({"data": [{"embedding": [0.25] * dimension}]}).encode()

    post.seen = seen
    return post


def test_embedder_for_without_a_block_or_with_the_containers_titan_row_keeps_deps(handler):
    deps = _deps(handler, [])
    handler._EMBEDDERS.clear()
    assert handler.embedder_for({"kind": "kb_ingest"}, deps) is deps.embedder
    # FakeEmbedder has no model_id → the env default stands in for it
    assert handler.embedder_for({"embedding": _titan_block()}, deps) is deps.embedder
    assert handler.embedder_for({"embedding": {"model_id": ""}}, deps) is deps.embedder


def test_embedder_for_builds_titan_for_another_bedrock_row_on_the_same_bearer_client(handler):
    deps = _deps(handler, [])
    handler._EMBEDDERS.clear()
    emb = handler.embedder_for({"embedding": _titan_block("amazon.titan-embed-text-v1", 768)}, deps)
    assert isinstance(emb, handler.TitanEmbedder)
    assert emb.bedrock_runtime is deps.bedrock and emb.model_id == "amazon.titan-embed-text-v1"
    assert emb.dimensions == 768
    # cached per descriptor for the life of the container
    assert handler.embedder_for({"embedding": _titan_block("amazon.titan-embed-text-v1", 768)},
                                deps) is emb


def test_embedder_for_builds_an_openai_embedder_through_identity_and_secrets(handler):
    deps = _deps(handler, [])
    handler._EMBEDDERS.clear()
    handler._BEARER.clear()
    control, post = _FakeControl(), _fake_post(16)
    deps = dataclasses.replace(deps, control=control, secrets=_FakeSecrets(), http_post=post)
    emb = handler.embedder_for({"embedding": _openai_block()}, deps)
    assert isinstance(emb, handler.OpenAIEmbedder)
    vector = emb.embed("x" * 500)
    assert vector == [0.25] * 16
    assert control.names == ["launchpad-siliconflow"]  # ITS provider, not the Bedrock one
    url, headers, body = post.seen[0]
    assert url == "https://api.siliconflow.cn/v1/embeddings"
    assert headers["Authorization"] == "Bearer key-for-launchpad-siliconflow"
    assert body == {"model": "BAAI/bge-m3", "input": "x" * 100}  # max_chars honoured
    # a vector of another length than the descriptor's dimension is refused
    bad = dataclasses.replace(deps, http_post=_fake_post(8))
    handler._EMBEDDERS.clear()
    with pytest.raises(RuntimeError, match="8-dimensional.*declares 16"):
        handler.embedder_for({"embedding": _openai_block()}, bad).embed("x")
    # without the clients an OpenAI-compatible block is a loud error, not a silent Titan
    handler._EMBEDDERS.clear()
    with pytest.raises(RuntimeError, match="Deps.control"):
        handler.embedder_for({"embedding": _openai_block()}, _deps(handler, []))


def test_kb_ingest_embeds_with_the_messages_openai_row(handler):
    deps = _deps(handler, [])
    handler._EMBEDDERS.clear()
    handler._BEARER.clear()
    post = _fake_post(16)
    deps = dataclasses.replace(deps, control=_FakeControl(), secrets=_FakeSecrets(), http_post=post)
    deps.s3.put_object(Bucket=BUCKET, Key="kb/KB1/DS1/policy.md", Body=b"alpha beta gamma")
    deps.opensearch.ensure_index("launchpad-kb-kb1", core.kb_index_body())
    deps.opensearch.put_doc(core.jobs_index(""), "ij9", {"job_id": "ij9", "kind": "kb_ingest",
                            "kb_id": "KB1", "ds_id": "DS1", "status": "STARTING", "counts": {},
                            "created_at": T0.isoformat(), "updated_at": T0.isoformat()})
    out = handler.process_message({**_kb_msg(), "job_id": "ij9", "prefix": "kb/KB1/DS1/",
                                   "embedding": _openai_block()}, deps)
    assert out["status"] == "COMPLETE"
    chunks = deps.opensearch.indices["launchpad-kb-kb1"]
    assert chunks and all(c["embedding"] == [0.25] * 16 for c in chunks.values())
    assert post.seen  # the endpoint was called, not Titan


# ── N17-obs237: a size refusal is PERMANENT (shrink once, then FAILED + dropped), never a DLQ ──
def _too_long(operation="InvokeModel"):
    """The live error on default's extractor (2026-10-20 16:42:44Z, 4 receives → DLQ)."""
    return ClientError(
        {"Error": {"Code": "ValidationException",
                   "Message": "Too many input tokens. Max input tokens: 8192, request input "
                              "token count: 9067"},
         "ResponseMetadata": {"HTTPStatusCode": 400}},
        operation,
    )


class LimitedEmbedder(FakeEmbedder):
    """Titan's 8192-token cap, modelled as a character threshold."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars
        self.lengths: list[int] = []

    def embed(self, text: str) -> list[float]:
        self.lengths.append(len(text))
        if len(text) > self.max_chars:
            raise _too_long()
        return super().embed(text)


def _long_turn(deps, chars: int, *, context: int = 0, max_prompt_chars: int):
    """One turn of ``chars`` characters preceded by ``context`` earlier turns of the same size
    in the session; `deps.env` carries the prompt budget."""
    deps.env = {**deps.env, "max_prompt_chars": max_prompt_chars}
    for i in range(context):
        _event(deps, "早" * (chars // 2), "答" * (chars // 2), ts=T0.replace(minute=i))
    return _event(deps, "新" * (chars // 2), "复" * (chars // 2), ts=T0.replace(minute=30))


def test_the_live_size_refusal_is_permanent_and_the_shared_predicate_never_called_it_transient(
    handler,
):
    """Defect (1) root cause: `runtime_invoke.is_upstream_transient` already answered False for
    this 400 — it was `process_memory_extract`'s last-resort `except Exception → RetryableError`
    that redelivered it. Pinned here so the classification stays explicit."""
    import runtime_invoke  # the sibling copy on sys.path while the handler fixture is alive

    exc = _too_long()
    assert handler.is_input_too_long(exc) and handler.is_model_validation_error(exc)
    assert handler._is_transient(exc) is False
    assert runtime_invoke.is_upstream_transient(exc) is False
    assert handler.is_input_too_long(
        RuntimeError("context_length_exceeded: this model's maximum context length is 8192"))
    assert handler.is_input_too_long(_too_long("Converse"))
    throttled = ClientError({"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
                            "InvokeModel")
    assert not handler.is_input_too_long(throttled) and handler._is_transient(throttled)
    other = ClientError({"Error": {"Code": "ValidationException",
                                   "Message": "The provided model identifier is invalid"}},
                        "InvokeModel")
    assert handler.is_model_validation_error(other) and not handler.is_input_too_long(other)


def test_fit_transcript_drops_the_oldest_context_first_then_truncates_the_turn(handler):
    events = [core.turn_event(ACTOR, "s", f"q{i} " + "x" * 90, "a" * 100, T0) for i in range(3)]
    turn = core.turn_event(ACTOR, "s", "final " + "y" * 150, "z" * 150, T0)
    full = handler._transcript([*events, turn])
    same, fit = handler.fit_transcript(events, turn, len(full))
    assert same == full and fit == {"context_events": 3, "dropped": 0, "truncated": False,
                                    "chars": len(full)}
    two, fit = handler.fit_transcript(events, turn, len(full) - 1)
    assert fit["dropped"] == 1 and fit["context_events"] == 2 and not fit["truncated"]
    assert two.startswith("USER: q1 ") and two.endswith("z" * 150)  # the OLDEST went first
    only, fit = handler.fit_transcript(events, turn, 200)
    assert fit == {"context_events": 0, "dropped": 3, "truncated": True, "chars": 200}
    assert only.startswith("USER: final ") and only.endswith("[… truncated]")


def test_the_transcript_is_fitted_to_the_budget_before_any_model_call(handler):
    """Defect (2): `CONTEXT_EVENTS` + the new turn went to the embedder whole. The budget
    applies first — the embedder never sees more than `max_prompt_chars` characters."""
    deps = _deps(handler, ['["A"]', "[]", "s"])
    embedder = LimitedEmbedder(max_chars=10_000)
    deps = dataclasses.replace(deps, embedder=embedder, bedrock=FakeBedrockRuntime(
        embedder, ['["A"]', "[]", "s"]))
    key, _ = _long_turn(deps, 1200, context=3, max_prompt_chars=3000)
    out = handler.process_message(_msg(key), deps)
    assert out["status"] == "COMPLETED"
    # the two candidate embeddings (facts, preferences) saw the fitted transcript only
    assert all(length <= 3000 for length in embedder.lengths[:2]), embedder.lengths
    for call in deps.bedrock.converse_calls:
        assert len(call["messages"][0]["content"][0]["text"]) < 3000 + 400  # + EXISTING prefix
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "COMPLETED"
    assert any("prompt fitted to 3000 chars" in w and "context event(s) dropped" in w
               for w in job["warnings"]), job["warnings"]


def test_a_size_refusal_shrinks_the_prompt_once_and_the_extraction_completes(handler):
    """The budget is a character estimate; a refusal that still gets through is retried ONCE
    with every context event dropped and the turn halved — the same call, then the rest of the
    extraction on the shrunk transcript."""
    deps = _deps(handler, ['["A"]', "[]", "s"])
    embedder = LimitedEmbedder(max_chars=5000)  # the "model" refuses above this
    deps = dataclasses.replace(deps, embedder=embedder, bedrock=FakeBedrockRuntime(
        embedder, ['["A"]', "[]", "s"]))
    # ≈9000 chars: within the budget, above the "model's" cap
    key, _ = _long_turn(deps, 3000, context=2, max_prompt_chars=20_000)
    out = handler.process_message(_msg(key), deps)
    assert out["status"] == "COMPLETED", out
    assert embedder.lengths[0] > 5000 and embedder.lengths[1] <= 4500  # refused, then shrunk
    assert all(length <= 4500 for length in embedder.lengths[1:3])  # the next call stays shrunk
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "COMPLETED" and "failure_code" not in job
    assert any("refused the prompt size" in w and "retried once" in w for w in job["warnings"])


def test_a_size_refusal_after_the_shrink_is_failed_with_the_reason_and_dropped_not_retried(
    handler, monkeypatch, caplog,
):
    """Pre-fix: `outcome=retry … ValidationException … Too many input tokens` on every receive
    until the DLQ (4 receives), the extraction silently lost. Now: FAILED `failure_code:
    input_too_long` with the model's reason, the message acknowledged (`outcome=dropped`,
    no batchItemFailures), one WARNING line."""
    deps = _deps(handler, ['["A"]', "[]", "s"])
    embedder = LimitedEmbedder(max_chars=100)  # refuses even the shrunk prompt
    deps = dataclasses.replace(deps, embedder=embedder, bedrock=FakeBedrockRuntime(
        embedder, ['["A"]', "[]", "s"]))
    key, _ = _long_turn(deps, 3000, context=2, max_prompt_chars=20_000)
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    event = {"Records": [{"messageId": "m1", "body": json.dumps(_msg(key))}]}
    assert handler.lambda_handler(event, None) == {"batchItemFailures": []}  # acked, not retried
    assert len(embedder.lengths) == 2  # the call, the one shrunk retry — nothing more
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "input_too_long"
    assert job["failure_reason"].startswith("input_too_long: ClientError: ")
    assert "Too many input tokens" in job["failure_reason"]
    assert any("retried once" in w for w in job["warnings"])
    outcome_lines = [r for r in caplog.records if r.getMessage().startswith("message=m1")]
    assert len(outcome_lines) == 1 and outcome_lines[0].levelno == logging.WARNING
    assert "outcome=dropped" in outcome_lines[0].getMessage()
    assert "input_too_long" in outcome_lines[0].getMessage()
    assert deps.opensearch.indices[core.memory_index("")] == {}  # nothing half-written


def test_a_size_refusal_from_the_extraction_model_is_shrunk_too(handler):
    """The Converse call shares the prompt: its refusal shrinks once as well."""

    class RefusingBedrock(FakeBedrockRuntime):
        def __init__(self, embedder, outputs, max_chars):
            super().__init__(embedder, outputs)
            self.max_chars = max_chars
            self.lengths: list[int] = []

        def converse(self, **kwargs):
            text = kwargs["messages"][0]["content"][0]["text"]
            self.lengths.append(len(text))
            if len(text) > self.max_chars:
                raise _too_long("Converse")
            return super().converse(**kwargs)

    deps = _deps(handler, ['["A"]', "[]", "s"])
    bedrock = RefusingBedrock(deps.embedder, ['["A"]', "[]", "s"], max_chars=5000)
    deps = dataclasses.replace(deps, bedrock=bedrock)
    key, _ = _long_turn(deps, 3000, context=2, max_prompt_chars=20_000)
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert out["status"] == "COMPLETED", out
    assert bedrock.lengths[0] > 5000 and all(length <= 5000 for length in bedrock.lengths[1:])
    assert len(bedrock.converse_calls) == 3  # facts, preferences, summary — all on the shrunk text


def test_another_model_validation_error_is_failed_and_redelivered_not_dropped(
        handler, monkeypatch, caplog):
    """N17-obs535 (ten-student simulation 2026-10-30): a 400 the request caused that is not
    about size (an invalid model id, a capability the catalog row got wrong) used to be FAILED
    and then acknowledged — the message vanished with nothing but the jobs row saying so. It is
    still FAILED `failure_code: model_validation` for the hub, but the receive is reported as a
    batch item failure (SQS redelivers; the DLQ after maxReceiveCount) and ONE structured
    WARNING line says `outcome=failed_permanent reason=model_validation model=<id>` + text."""

    class InvalidModel(FakeEmbedder):
        def embed(self, text):
            raise ClientError({"Error": {"Code": "ValidationException",
                                         "Message": "The provided model identifier is invalid"}},
                              "InvokeModel")

    deps = _deps(handler, ['["A"]', "[]", "s"])
    deps = dataclasses.replace(deps, embedder=InvalidModel())
    key, _ = _event(deps, "hello", "hi")
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    event = {"Records": [{"messageId": "m1", "body": json.dumps(_msg(key))}]}
    assert handler.lambda_handler(event, None) == {
        "batchItemFailures": [{"itemIdentifier": "m1"}]}  # redelivered, never acked
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_validation"
    assert "model identifier is invalid" in job["failure_reason"]
    lines = [r for r in caplog.records if "outcome=" in r.getMessage()]
    assert len(lines) == 1 and lines[0].levelno == logging.WARNING
    text = lines[0].getMessage()
    assert "outcome=failed_permanent" in text and "reason=model_validation" in text
    assert "model=global.zai.glm-5.3" in text and "model identifier is invalid" in text
    assert not any("outcome=dropped" in r.getMessage() for r in caplog.records)
    # the class is a redelivery for the handler's batch report
    assert issubclass(handler.ModelValidationFailure, handler.RetryableError)
    with pytest.raises(handler.ModelValidationFailure) as info:
        handler.process_message(_msg(key), deps)
    assert info.value.model == "global.zai.glm-5.3"


# ── N17-obs245: one document's chunks go to the index in batches; a timed-out batch retries once ──
class _Pool:
    def __str__(self) -> str:  # what urllib3 prints — the domain host, which must stay off records
        return "HTTPSConnectionPool(host='search-lp-memory.cn-northwest-1.example', port=443)"


def _read_timeout():
    import urllib3

    return urllib3.exceptions.ReadTimeoutError(_Pool(), "/_bulk",
                                               "Read timed out. (read timeout=30.0)")


class BatchRecordingOpenSearch(FakeOpenSearch):
    """`bulk` records every batch size and raises the scripted exceptions first (one per call)."""

    def __init__(self) -> None:
        super().__init__()
        self.batches: list[int] = []
        self.script: list[BaseException] = []
        self.refreshes: list[str] = []

    def bulk(self, index, docs, refresh=True):
        self.batches.append(len(docs))
        assert refresh is False
        if self.script:
            raise self.script.pop(0)
        return super().bulk(index, docs, refresh)

    def request(self, method, path, body=None, params=None):
        if path.endswith("/_refresh"):
            self.refreshes.append(path)
        return super().request(method, path, body, params)


def _kb_deps(handler, os_: FakeOpenSearch):
    deps = _deps(handler, [])
    for name, body in ((core.memory_index(""), core.memory_index_body()),
                       (core.jobs_index(""), core.jobs_index_body()),
                       (core.kb_docs_index(""), core.kb_docs_index_body()),
                       ("launchpad-kb-kb1", core.kb_index_body())):
        os_.ensure_index(name, body)
    deps = dataclasses.replace(deps, opensearch=os_)
    deps.opensearch.put_doc(core.jobs_index(""), "ij1", {
        "job_id": "ij1", "kind": "kb_ingest", "kb_id": "KB1", "ds_id": "DS1",
        "status": "STARTING", "counts": {}, "created_at": T0.isoformat(),
        "updated_at": T0.isoformat()})
    return deps


LONG_HTML = ("<html><body>" + "\n\n".join(
    f"<p>Day {i}: " + ("drive along the coast and stop for photos " * 18) + "</p>"
    for i in range(40)) + "</body></html>").encode()
KB_PREFIX = "kb/KB1/DS1/"


def _ingest(handler, deps, job_id=None):
    """One sync of the test prefix. Like the hub, every sync is its own job: the first call on
    a world is `ij1`, later calls mint `ij2`, `ij3`, … unless a job id is given (N17-obs266: a
    redelivered planner of a COMPLETE job is a no-op, so re-syncs must not reuse the id)."""
    n = deps.env.get("_syncs", 0) + 1
    deps.env["_syncs"] = n  # the env dict is shared by every `dataclasses.replace` copy
    return handler.process_message({
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
        "job_id": job_id or f"ij{n}", "bucket": BUCKET, "prefix": KB_PREFIX,
        "index": "launchpad-kb-kb1"}, deps)


def _doc_records(deps) -> dict[str, dict]:
    return {d["name"]: d for d in deps.opensearch.indices[core.kb_docs_index("")].values()}


def test_a_documents_chunks_are_written_in_batches_of_sixteen(handler):
    """Live: the 81.3 KB HTML's chunks went to the single t3.small node in ONE `_bulk` and
    the request exceeded the 30 s read timeout. Now `BULK_BATCH_CHUNKS` per request, the
    delete-then-write order and the single `_refresh` per document kept."""
    os_ = BatchRecordingOpenSearch()
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    text, _ = handler._doc_text("trip.html", LONG_HTML)
    n = len(core.chunk_text(text))
    assert 33 <= n <= 48, n  # three batches' worth
    out = _ingest(handler, deps)
    assert out["status"] == "COMPLETE" and out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert handler.BULK_BATCH_CHUNKS == 16
    assert os_.batches == [16] * (n // 16) + ([n % 16] if n % 16 else [])
    assert len(os_.batches) == 3 and sum(os_.batches) == n
    assert len(deps.opensearch.indices["launchpad-kb-kb1"]) == n  # every chunk landed once
    record = _doc_records(deps)["trip.html"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == n
    assert os_.refreshes == ["/launchpad-kb-kb1/_refresh"] * 2  # one per document + the final one


def test_a_timed_out_batch_is_retried_once_and_the_document_indexes(handler, caplog):
    os_ = BatchRecordingOpenSearch()
    os_.script = [_read_timeout()]  # the first batch times out once
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert out["counts"]["numberOfDocumentsFailed"] == 0
    n = sum(os_.batches) - 16  # the retried batch counted twice
    assert os_.batches == [16, 16, 16, n - 32]  # same batch again, then the rest
    assert len(deps.opensearch.indices["launchpad-kb-kb1"]) == n  # idempotent ids, no duplicates
    assert _doc_records(deps)["trip.html"]["status"] == "INDEXED"
    retry_lines = [r.getMessage() for r in caplog.records if "retrying once" in r.getMessage()]
    assert retry_lines == [f"kb_ingest: bulk batch 1-16 of {n} chunks timed out "
                           "(ReadTimeoutError) — retrying once"]


def test_a_batch_timing_out_twice_fails_only_that_document_with_a_readable_reason(
    handler, caplog,
):
    """The second document of the job still indexes; the stored reason is the readable string
    (no exception class, no hostname); the log line carries the class."""
    os_ = BatchRecordingOpenSearch()
    os_.script = [_read_timeout(), _read_timeout()]
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "zz-policy.md",
                       Body=("Vacation policy. " * 60).encode())
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    out = _ingest(handler, deps)
    assert out["status"] == "COMPLETE"
    assert out["counts"] == {"numberOfDocumentsScanned": 2, "numberOfNewDocumentsIndexed": 1,
                             "numberOfDocumentsFailed": 1, "numberOfDocumentsUnchanged": 0}
    records = _doc_records(deps)
    failed, indexed = records["trip.html"], records["zz-policy.md"]
    assert failed["status"] == "FAILED" and failed["chunk_count"] == 0
    assert failed["status_reason"] == "index write timed out — run the sync again"
    assert "search-lp-memory" not in failed["status_reason"]
    assert "ReadTimeoutError" not in failed["status_reason"]
    assert indexed["status"] == "INDEXED" and indexed["chunk_count"] >= 1
    assert os_.batches[:2] == [16, 16]  # the batch, its one retry — then the document failed
    assert len(os_.batches) == 3  # + the second document's single batch
    failed_lines = [r.getMessage() for r in caplog.records if "FAILED" in r.getMessage()]
    assert len(failed_lines) == 1
    assert failed_lines[0].startswith("kb_ingest job=ij1 document KB1:DS1:")
    assert failed_lines[0].endswith("FAILED (IndexWriteTimeout ← ReadTimeoutError)")
    assert "search-lp-memory" not in " ".join(failed_lines)  # the host stays out of the log too
    # nothing of the failed document is left in the index (delete-then-write), the job is COMPLETE
    chunks = deps.opensearch.indices["launchpad-kb-kb1"].values()
    assert all(d["doc_name"] != "trip.html" for d in chunks)
    assert deps.opensearch.get_doc(core.jobs_index(""), "ij1")["status"] == "COMPLETE"


def test_a_non_timeout_bulk_error_is_not_retried(handler):
    """Only transport timeouts get the retry — a 400 from the index is the document's failure
    at once, as before."""
    os_ = BatchRecordingOpenSearch()
    os_.script = [core.OpenSearchError(400, "mapper_parsing_exception")]
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfDocumentsFailed"] == 1 and os_.batches == [16]
    assert _doc_records(deps)["trip.html"]["status_reason"].startswith("OpenSearchError: ")


# ── N17-obs246: a sync re-embeds only what changed; deleted objects leave the index ──────────
class CountingEmbedder(FakeEmbedder):
    def __init__(self) -> None:
        self.calls = 0

    def embed(self, text):
        self.calls += 1
        return super().embed(text)


def _incremental_world(handler):
    os_ = BatchRecordingOpenSearch()
    deps = _kb_deps(handler, os_)
    embedder = CountingEmbedder()
    deps = dataclasses.replace(deps, embedder=embedder)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "a-policy.md",
                       Body=("Vacation policy. " * 60).encode())
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "b-trip.html", Body=LONG_HTML)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "c-guide.pdf", Body=b"%PDF")
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "_text/c-guide.pdf.txt",
                       Body=b"Guide text about expenses.")
    return deps, os_, embedder


def _counts(out):
    return {k: v for k, v in out["counts"].items() if k != "prefix_empty"}


def test_a_second_sync_of_an_unchanged_prefix_embeds_nothing_and_counts_unchanged(handler):
    """Live: the user's 4-file KB re-embedded all 4 (≈90 s) to retry the one that failed; each
    document was unsearchable between its delete and its write. Now a document whose record is
    INDEXED with the listing's ETag + size is skipped outright."""
    deps, os_, embedder = _incremental_world(handler)
    first = _ingest(handler, deps)
    assert _counts(first) == {"numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 3,
                              "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 0}
    records = _doc_records(deps)
    assert all(r["status"] == "INDEXED" and r["etag"] for r in records.values())
    # the PDF's record carries the SIDECAR's ETag (its text), the others the object's
    sidecar_etag = deps.s3.get_object(Bucket=BUCKET, Key=KB_PREFIX + "_text/c-guide.pdf.txt")
    assert records["c-guide.pdf"]["etag"] == sidecar_etag["ETag"].strip('"')
    chunks_before = dict(deps.opensearch.indices["launchpad-kb-kb1"])
    embeds, batches = embedder.calls, len(os_.batches)
    second = _ingest(handler, deps)
    assert _counts(second) == {"numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 0,
                               "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 3}
    assert embedder.calls == embeds and len(os_.batches) == batches  # no embed, no write
    assert deps.opensearch.indices["launchpad-kb-kb1"] == chunks_before  # never deleted either
    assert deps.opensearch.get_doc(core.jobs_index(""), "ij1")["status"] == "COMPLETE"


def test_only_the_changed_document_is_re_indexed(handler):
    deps, os_, embedder = _incremental_world(handler)
    _ingest(handler, deps)
    embeds = embedder.calls
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "a-policy.md",
                       Body=("Vacation policy, revised. " * 60).encode())  # new ETag + size
    out = _ingest(handler, deps)
    assert _counts(out) == {"numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 1,
                            "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 2}
    policy_chunks = [d for d in deps.opensearch.indices["launchpad-kb-kb1"].values()
                     if d["doc_name"] == "a-policy.md"]
    assert policy_chunks and all("revised" in d["text"] for d in policy_chunks)
    assert embedder.calls == embeds + len(policy_chunks)  # exactly its chunks were embedded
    # a changed PDF SIDECAR (same original object) is a change too
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "_text/c-guide.pdf.txt",
                       Body=b"Guide text about expenses and travel.")
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert out["counts"]["numberOfDocumentsUnchanged"] == 2
    guide = [d for d in deps.opensearch.indices["launchpad-kb-kb1"].values()
             if d["doc_name"] == "c-guide.pdf"]
    assert len(guide) == 1 and "travel" in guide[0]["text"]


def test_a_record_without_an_etag_is_re_indexed_once_then_skipped(handler):
    """A record written before this change has no `etag`: treated as changed once (the ETag is
    backfilled), unchanged from then on."""
    deps, os_, embedder = _incremental_world(handler)
    _ingest(handler, deps)
    legacy_id = next(i for i, d in deps.opensearch.indices[core.kb_docs_index("")].items()
                     if d["name"] == "b-trip.html")
    deps.opensearch.indices[core.kb_docs_index("")][legacy_id].pop("etag")
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert out["counts"]["numberOfDocumentsUnchanged"] == 2
    assert deps.opensearch.get_doc(core.kb_docs_index(""), legacy_id)["etag"]
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 0
    assert out["counts"]["numberOfDocumentsUnchanged"] == 3


def test_full_true_forces_the_complete_rewrite(handler):
    deps, os_, embedder = _incremental_world(handler)
    _ingest(handler, deps)
    embeds = embedder.calls
    out = handler.process_message({
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
        "job_id": "ij-full", "bucket": BUCKET, "prefix": KB_PREFIX, "index": "launchpad-kb-kb1",
        "full": True}, deps)
    assert _counts(out) == {"numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 3,
                            "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 0}
    assert embedder.calls == 2 * embeds


def test_a_failed_or_unsupported_document_is_tried_again_on_the_next_sync(handler):
    """Only an INDEXED record counts as unchanged — a FAILED (obs245 timeout) or UNSUPPORTED
    one is retried by the next sync, which is what "run the sync again" promises."""
    os_ = BatchRecordingOpenSearch()
    os_.script = [_read_timeout(), _read_timeout()]
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "deck.pptx", Body=b"binary")
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfDocumentsFailed"] == 2  # the timeout, the unsupported type
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1  # the HTML, now that writes work
    assert out["counts"]["numberOfDocumentsFailed"] == 1
    assert out["counts"]["numberOfDocumentsUnchanged"] == 0
    assert _doc_records(deps)["trip.html"]["status"] == "INDEXED"


def test_a_deleted_object_leaves_the_index_with_the_next_sync(handler):
    deps, os_, embedder = _incremental_world(handler)
    _ingest(handler, deps)
    assert any(d["doc_name"] == "a-policy.md"
               for d in deps.opensearch.indices["launchpad-kb-kb1"].values())
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": KB_PREFIX + "a-policy.md"}]})
    out = _ingest(handler, deps)
    assert _counts(out) == {"numberOfDocumentsScanned": 2, "numberOfNewDocumentsIndexed": 0,
                            "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 2,
                            "numberOfDocumentsDeleted": 1}
    assert not any(d["doc_name"] == "a-policy.md"
                   for d in deps.opensearch.indices["launchpad-kb-kb1"].values())
    assert "a-policy.md" not in _doc_records(deps)
    assert set(_doc_records(deps)) == {"b-trip.html", "c-guide.pdf"}
    # a sync that finds nothing stale deletes nothing and reports no `Deleted` key
    assert "numberOfDocumentsDeleted" not in _ingest(handler, deps)["counts"]


# ── N17-obs247: the sync message plans; one message per changed document does the work ──────
from tests.memory_fakes import FakeSQS  # noqa: E402

QUEUE_URL = "https://sqs.example/launchpad-memory-extract"


def _fanout_world(handler, sqs=None):
    deps, os_, embedder = _incremental_world(handler)
    sqs = sqs or FakeSQS()
    deps = dataclasses.replace(deps, sqs=sqs, queue_url=QUEUE_URL)
    return deps, os_, embedder, sqs


def _job_doc(deps, job_id="ij1"):
    return deps.opensearch.get_doc(core.jobs_index(""), job_id)


def _result_rows(deps, job_id="ij1"):
    return [d for d in deps.opensearch.indices[core.jobs_index("")].values()
            if d.get("kind") == "kb_ingest_doc_result" and d.get("job_id") == job_id]


def test_a_sync_fans_out_one_message_per_changed_document_and_completes_on_the_last(handler):
    """Live: one invocation handled every document sequentially under the 120 s timeout. The
    planner now embeds nothing and enqueues one `kb_ingest_doc` per changed document; each
    writes a result row; the job turns COMPLETE with the last one."""
    deps, os_, embedder, sqs = _fanout_world(handler)
    out = _ingest(handler, deps)
    assert out["status"] == "IN_PROGRESS" and out["dispatched"] == 3
    assert embedder.calls == 0 and os_.batches == []  # the planner reads the listing only
    assert [url for url, _ in sqs.sent] == [QUEUE_URL] * 3
    messages = [m for _, m in sqs.sent]
    assert {m["kind"] for m in messages} == {"kb_ingest_doc"}
    assert sorted(m["document"]["name"] for m in messages) == ["a-policy.md", "b-trip.html",
                                                              "c-guide.pdf"]
    assert all(m["job_id"] == "ij1" and m["index"] == "launchpad-kb-kb1" and m["document"]["etag"]
               for m in messages)
    job = _job_doc(deps)
    assert job["status"] == "IN_PROGRESS" and job["fanout"]["expected"] == 3
    assert len(job["fanout"]["run"]) == 12  # N17-obs266: this planner run's generation
    assert job["counts"]["numberOfDocumentsScanned"] == 3
    assert job["counts"]["numberOfNewDocumentsIndexed"] == 0
    for i, message in enumerate(messages):
        result = handler.process_message(message, deps)
        assert result["status"] == "INDEXED", result
        assert (_job_doc(deps)["status"] == "COMPLETE") == (i == 2), i  # only the LAST completes
    job = _job_doc(deps)
    assert _counts({"counts": job["counts"]}) == {
        "numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 3,
        "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 0}
    assert job["fanout"]["expected"] == 3 and job["fanout"]["reported"] == 3
    assert len(_result_rows(deps)) == 3
    assert all(r["status"] == "INDEXED" for r in _doc_records(deps).values())
    # the hub lists jobs by kind=kb_ingest: the result rows never show up as jobs
    hits = deps.opensearch.search(core.jobs_index(""), {"query": {"bool": {"filter": [
        {"term": {"kind": "kb_ingest"}}, {"term": {"kb_id": "KB1"}}]}}})["hits"]["hits"]
    assert [h["_id"] for h in hits] == ["ij1"]


def test_a_failed_document_in_the_fan_out_still_completes_the_job_with_failed_1(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    messages = {m["document"]["name"]: m for _, m in sqs.sent}
    os_.script = [_read_timeout(), _read_timeout()]  # the HTML's first batch, twice
    assert handler.process_message(messages["b-trip.html"], deps)["status"] == "FAILED"
    assert _job_doc(deps)["status"] == "IN_PROGRESS"
    assert handler.process_message(messages["a-policy.md"], deps)["status"] == "INDEXED"
    assert handler.process_message(messages["c-guide.pdf"], deps)["status"] == "INDEXED"
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE"
    assert job["counts"]["numberOfNewDocumentsIndexed"] == 2
    assert job["counts"]["numberOfDocumentsFailed"] == 1
    records = _doc_records(deps)
    assert records["b-trip.html"]["status"] == "FAILED"
    assert records["b-trip.html"]["status_reason"] == "index write timed out — run the sync again"
    assert records["a-policy.md"]["status"] == records["c-guide.pdf"]["status"] == "INDEXED"


def test_a_duplicate_document_message_is_idempotent(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    messages = [m for _, m in sqs.sent]
    first = handler.process_message(messages[0], deps)
    embeds = embedder.calls
    again = handler.process_message(messages[0], deps)  # SQS at-least-once
    assert first["status"] == "INDEXED" and again["status"] == "DUPLICATE"
    assert embedder.calls == embeds and len(_result_rows(deps)) == 1
    for message in messages[1:]:
        handler.process_message(message, deps)
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 3
    assert len(_result_rows(deps)) == 3


def test_unchanged_documents_are_never_enqueued(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    for _, message in list(sqs.sent):
        handler.process_message(message, deps)
    assert _job_doc(deps)["status"] == "COMPLETE"
    sent = len(sqs.sent)
    out = _ingest(handler, deps, "ij2")  # nothing changed: no fan-out, COMPLETE at once
    assert out["status"] == "COMPLETE" and len(sqs.sent) == sent
    assert out["counts"]["numberOfDocumentsUnchanged"] == 3
    assert "dispatched" not in out
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "a-policy.md", Body=b"Policy, revised.")
    out = _ingest(handler, deps, "ij3")  # every sync is its own job (the hub mints the id)
    assert out["status"] == "IN_PROGRESS" and out["dispatched"] == 1
    assert len(sqs.sent) == sent + 1 and sqs.sent[-1][1]["document"]["name"] == "a-policy.md"
    assert out["counts"]["numberOfDocumentsUnchanged"] == 2
    handler.process_message(sqs.sent[-1][1], deps)
    job = _job_doc(deps, "ij3")
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert job["counts"]["numberOfDocumentsUnchanged"] == 2


def test_a_document_whose_enqueue_fails_is_indexed_inline(handler):
    class FlakySQS(FakeSQS):
        def send_message(self, **kw):
            if len(self.sent) == 1:  # the second send fails
                self.sent.append(("refused", {}))
                raise RuntimeError("queue hiccup")
            return super().send_message(**kw)

    sqs = FlakySQS()
    deps, os_, embedder, sqs = _fanout_world(handler, sqs=sqs)
    out = _ingest(handler, deps)
    assert out["status"] == "IN_PROGRESS" and out["dispatched"] == 2
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1  # the inline one
    assert embedder.calls > 0
    assert _job_doc(deps)["fanout"]["expected"] == 2
    for _, message in sqs.sent:
        if message:
            handler.process_message(message, deps)
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 3


def test_the_planner_completes_the_job_itself_when_every_result_already_reported(handler):
    """Race: a document message may finish before the planner's final counts write. The
    planner re-checks after that write, so the job never sticks at IN_PROGRESS."""
    deps, os_, embedder, sqs = _fanout_world(handler)

    class EagerSQS(FakeSQS):
        def send_message(self, **kw):
            out = super().send_message(**kw)
            handler.process_message(self.sent[-1][1], deps)  # processed "instantly"
            return out

    deps = dataclasses.replace(deps, sqs=EagerSQS())
    out = _ingest(handler, deps)
    assert out["dispatched"] == 3
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 3


def test_chunk_embeddings_run_on_a_bounded_pool_and_keep_their_order(handler):
    import threading
    import time as _time

    class TrackingEmbedder(FakeEmbedder):
        def __init__(self):
            self.lock = threading.Lock()
            self.now = self.peak = 0

        def embed(self, text):
            with self.lock:
                self.now += 1
                self.peak = max(self.peak, self.now)
            _time.sleep(0.005)
            with self.lock:
                self.now -= 1
            return super().embed(text)

    os_ = BatchRecordingOpenSearch()
    deps = _kb_deps(handler, os_)
    embedder = TrackingEmbedder()
    deps = dataclasses.replace(deps, embedder=embedder)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    out = _ingest(handler, deps)  # no queue → inline
    assert out["status"] == "COMPLETE" and out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert 2 <= embedder.peak <= handler.EMBED_WORKERS == 4, embedder.peak
    reference = FakeEmbedder()
    for chunk in deps.opensearch.indices["launchpad-kb-kb1"].values():
        assert chunk["embedding"] == reference.embed(chunk["text"])  # vector ↔ its own chunk


def test_lambda_handler_drives_the_planner_and_the_document_messages(handler, monkeypatch):
    deps, os_, embedder, sqs = _fanout_world(handler)
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    planner = {"Records": [{"messageId": "p1", "body": json.dumps({
        "kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
        "job_id": "ij1", "bucket": BUCKET, "prefix": KB_PREFIX, "index": "launchpad-kb-kb1"})}]}
    assert handler.lambda_handler(planner, None) == {"batchItemFailures": []}
    docs = {"Records": [{"messageId": f"d{i}", "body": json.dumps(m)}
                        for i, (_, m) in enumerate(sqs.sent)]}
    assert handler.lambda_handler(docs, None) == {"batchItemFailures": []}
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_a_document_message_for_a_vanished_job_or_kb_is_dropped(handler, monkeypatch, caplog):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    message = sqs.sent[0][1]
    deps.opensearch.delete_doc(core.jobs_index(""), "ij1")  # the hub swept the data source
    with pytest.raises(handler.DroppedMessage):
        handler.process_message(message, deps)
    assert embedder.calls == 0 and _result_rows(deps) == []


# ── N17-obs252: the message's `extract_model` block picks the extraction model ───────────────
def _extract_block(**over):
    block = {"model_id": "global.moonshotai.kimi-k3", "kind": "bedrock", "base_url": None,
             "api_model_name": "global.moonshotai.kimi-k3",
             "credential_provider": "launchpad-global-bedrock", "provider": "bedrock",
             "model_region": "us-west-2", "catalog_version": 7}
    block.update(over)
    return block


def test_the_extraction_model_comes_from_the_message_block_else_the_env(handler):
    deps = _deps(handler, ['["A"]', "[]", "s"])
    key, _ = _event(deps, "I like tea", "noted")
    out = handler.process_message({**_msg(key), "extract_model": _extract_block()}, deps)
    assert out["status"] == "COMPLETED"
    assert {c["modelId"] for c in deps.bedrock.converse_calls} == {"global.moonshotai.kimi-k3"}
    # no block (an older hub, a message in flight at the update): the env model
    deps = _deps(handler, ['["A"]', "[]", "s"])
    key, _ = _event(deps, "I like tea", "noted")
    handler.process_message(_msg(key), deps)
    assert {c["modelId"] for c in deps.bedrock.converse_calls} == {"global.zai.glm-5.3"}
    assert handler.chat_model_for({"extract_model": {"kind": "bedrock"}}, deps).model_id == (
        "global.zai.glm-5.3")  # a block without a model id is no block


def test_an_openai_compatible_extraction_row_goes_through_its_own_provider(handler):
    class ChatPost:
        def __init__(self):
            self.seen = []
            self.replies = ['["User likes tea"]', "[]", "Talked about tea."]

        def __call__(self, url, headers, body):
            self.seen.append((url, headers, json.loads(body)))
            return json.dumps({"choices": [{"message": {
                "role": "assistant", "content": self.replies.pop(0)}}]}).encode()

    handler._CHATS.clear()
    handler._BEARER.clear()
    deps = _deps(handler, [])
    control, post = _FakeControl(), ChatPost()
    deps = dataclasses.replace(deps, control=control, secrets=_FakeSecrets(), http_post=post)
    key, _ = _event(deps, "I like tea", "noted")
    block = _extract_block(model_id="siliconflow/deepseek-v4", kind="openai_compatible",
                           base_url="https://api.siliconflow.cn/v1",
                           api_model_name="deepseek-ai/DeepSeek-V4",
                           credential_provider="launchpad-siliconflow", provider="siliconflow",
                           temperature=True)  # F-LOAD-03: the row takes the field
    out = handler.process_message({**_msg(key, merge=NO_ROLL), "extract_model": block}, deps)
    assert out["status"] == "COMPLETED" and out["counts"]["facts"] == 1
    assert deps.bedrock.converse_calls == []  # Bedrock never consulted for the extraction
    assert control.names == ["launchpad-siliconflow"]
    urls = {u for u, _, _ in post.seen}
    assert urls == {"https://api.siliconflow.cn/v1/chat/completions"}
    _, headers, body = post.seen[0]
    assert headers["Authorization"] == "Bearer key-for-launchpad-siliconflow"
    assert body["model"] == "deepseek-ai/DeepSeek-V4" and body["temperature"] == 0
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    # cached per block for the container's life; the clients are required
    assert handler.chat_model_for({"extract_model": block}, deps) is handler.chat_model_for(
        {"extract_model": block}, deps)
    handler._CHATS.clear()
    with pytest.raises(RuntimeError, match="Deps.control"):
        handler.chat_model_for({"extract_model": block}, _deps(handler, []))


def test_a_size_refusal_from_the_message_model_is_shrunk_like_the_default(handler):
    """The obs237 handling wraps whichever model the block names."""

    class RefusingOnce(FakeBedrockRuntime):
        def __init__(self, embedder, outputs):
            super().__init__(embedder, outputs)
            self.refused = False

        def converse(self, **kwargs):
            if not self.refused:
                self.refused = True
                raise _too_long("Converse")
            return super().converse(**kwargs)

    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, bedrock=RefusingOnce(deps.embedder, ['["A"]', "[]", "s"]))
    key, _ = _event(deps, "I like tea", "noted")
    out = handler.process_message({**_msg(key), "extract_model": _extract_block()}, deps)
    assert out["status"] == "COMPLETED"
    assert {c["modelId"] for c in deps.bedrock.converse_calls} == {"global.moonshotai.kimi-k3"}
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert any("retried once" in w for w in job["warnings"])


# ── F-LOAD-03 (N17-obs534): `temperature` is sent only when the block says the row takes it ──
def _temperature_rejected(operation="Converse"):
    """The live Bedrock text for Kimi K3 (N12-bug11; ten-student simulation 2026-10-30)."""
    return ClientError(
        {"Error": {"Code": "ValidationException",
                   "Message": "This model doesn't support the temperature field. Remove "
                              "temperature and try again."},
         "ResponseMetadata": {"HTTPStatusCode": 400}},
        operation,
    )


def _openai_reply(text="[]"):
    return json.dumps({"choices": [{"finish_reason": "stop",
                                    "message": {"content": text}}]}).encode()


def _openai_400(url, text):
    from urllib.error import HTTPError as UrlHTTPError

    return UrlHTTPError(url, 400, "Bad Request", {}, BytesIO(json.dumps(
        {"error": {"message": text, "type": "invalid_request_error"}}).encode()))


def test_bedrock_chat_sends_temperature_only_when_the_block_says_so(handler):
    """N17-obs534: the Lambda hard-coded `inferenceConfig.temperature: 0`; Kimi K3 takes no
    temperature field and refused every extraction of the ten-student run. The block's
    `temperature` (the catalog row's capability) now gates the field: True → sent, False or
    absent (an older hub's block) → not sent; the env fallback never sends it."""
    for block, expected in (
        (_extract_block(temperature=True), {"temperature": 0}),
        (_extract_block(temperature=False), {}),
        (_extract_block(), {}),  # a block without the key: an older hub
    ):
        deps = _deps(handler, ['["A"]', "[]", "s"])
        key, _ = _event(deps, "I like tea", "noted")
        out = handler.process_message({**_msg(key, merge=NO_ROLL), "extract_model": block}, deps)
        assert out["status"] == "COMPLETED", block
        configs = [c["inferenceConfig"] for c in deps.bedrock.converse_calls]
        assert len(configs) == 3 and all(c["maxTokens"] == 2048 for c in configs)
        assert [{k: v for k, v in c.items() if k != "maxTokens"} for c in configs] == [expected] * 3
    deps = _deps(handler, ['["A"]', "[]", "s"])
    key, _ = _event(deps, "I like tea", "noted")
    assert handler.process_message(_msg(key, merge=NO_ROLL), deps)["status"] == "COMPLETED"
    assert deps.bedrock.converse_calls and all(
        "temperature" not in c["inferenceConfig"] for c in deps.bedrock.converse_calls)
    assert handler.chat_model_for(_msg(key), deps).temperature is False


def test_openai_chat_sends_temperature_only_when_the_block_says_so(handler):
    for temperature, expected in ((True, True), (False, False), (None, False)):
        seen = []

        def post(url, headers, body, seen=seen):
            seen.append(json.loads(body))
            return _openai_reply()

        kwargs = {} if temperature is None else {"temperature": temperature}
        chat = handler.OpenAIChat(base_url="https://example.invalid", api_key="test",
                                  api_model_name="model", post=post, **kwargs)
        assert chat.converse_text("system", "user") == "[]"
        assert ("temperature" in seen[0]) is expected, (temperature, seen)
        assert seen[0]["max_tokens"] == 2048 and seen[0]["model"] == "model"
    # through chat_model_for: the block's capability reaches the cached client
    handler._CHATS.clear()
    handler._BEARER.clear()
    deps = dataclasses.replace(_deps(handler, []), control=_FakeControl(), secrets=_FakeSecrets(),
                               http_post=lambda *a: _openai_reply())
    block = _extract_block(model_id="siliconflow/deepseek-v4", kind="openai_compatible",
                           base_url="https://api.siliconflow.cn/v1",
                           api_model_name="deepseek-ai/DeepSeek-V4",
                           credential_provider="launchpad-siliconflow", provider="siliconflow")
    assert handler.chat_model_for({"extract_model": block}, deps).temperature is False
    assert handler.chat_model_for({"extract_model": {**block, "temperature": True}},
                                  deps).temperature is True
    handler._CHATS.clear()


def test_a_bedrock_400_naming_temperature_is_retried_once_without_it(handler, caplog):
    """A block that says True for a model that refuses the field (a wrong catalog row, a model
    swapped under the id): the 400 is retried ONCE without temperature, the chat remembers the
    downgrade for the rest of the extraction, one WARNING line names it."""

    class RejectsTemperature(FakeBedrockRuntime):
        def converse(self, **kwargs):
            if "temperature" in kwargs["inferenceConfig"]:
                self.converse_calls.append(kwargs)
                raise _temperature_rejected()
            return super().converse(**kwargs)

    deps = _deps(handler, [])
    deps = dataclasses.replace(
        deps, bedrock=RejectsTemperature(deps.embedder, ['["A"]', "[]", "s"]))
    key, _ = _event(deps, "I like tea", "noted")
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    out = handler.process_message(
        {**_msg(key, merge=NO_ROLL), "extract_model": _extract_block(temperature=True)}, deps)
    assert out["status"] == "COMPLETED"
    configs = [c["inferenceConfig"] for c in deps.bedrock.converse_calls]
    assert [("temperature" in c) for c in configs] == [True, False, False, False]
    assert sum("rejected the temperature field" in r.getMessage() for r in caplog.records) == 1
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["status"] == "COMPLETED"


def test_the_temperature_retry_happens_exactly_once_before_classification(handler):
    """A model that refuses even the retry: two calls (with, then without the field), then the
    400 is classified like any other — `model_validation`, FAILED and redelivered (N17-obs535),
    with the model named."""

    class AlwaysRejects(FakeBedrockRuntime):
        def converse(self, **kwargs):
            self.converse_calls.append(kwargs)
            raise _temperature_rejected()

    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, bedrock=AlwaysRejects(deps.embedder, []))
    key, _ = _event(deps, "I like tea", "noted")
    with pytest.raises(handler.ModelValidationFailure) as info:
        handler.process_message(
            {**_msg(key, merge=NO_ROLL), "extract_model": _extract_block(temperature=True)}, deps)
    assert [("temperature" in c["inferenceConfig"]) for c in deps.bedrock.converse_calls] == [
        True, False]
    assert info.value.model == "global.moonshotai.kimi-k3"
    assert "temperature field" in str(info.value)
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_validation"


def test_an_openai_400_naming_temperature_is_retried_once_without_it(handler):
    seen = []

    def post(url, headers, body):
        payload = json.loads(body)
        seen.append(payload)
        if "temperature" in payload:
            raise _openai_400(url, "Unsupported parameter: 'temperature' is not supported "
                                   "with this model.")
        return _openai_reply()

    chat = handler.OpenAIChat(base_url="https://example.invalid", api_key="test",
                              api_model_name="model", post=post, temperature=True)
    assert chat.converse_text("system", "user") == "[]"
    assert [("temperature" in p) for p in seen] == [True, False]
    assert chat.temperature is False  # remembered for the container's life
    assert chat.converse_text("system", "user") == "[]"
    assert len(seen) == 3 and "temperature" not in seen[2]


def test_a_400_not_about_temperature_is_not_retried_and_the_size_classifier_reads_the_body(
        handler):
    """The temperature check must not eat the HTTP body the size classifier reads next
    (`_error_text` puts it back), and a size / other 400 is never retried without the field."""
    from urllib.error import HTTPError as UrlHTTPError

    size = _openai_400("u", "This model's maximum context length is 8192 tokens "
                            "(context_length_exceeded)")
    assert handler.is_temperature_rejection(size) is False
    assert handler.is_input_too_long(size) is True  # the body was put back after the first read
    temp = _openai_400("u", "Unsupported parameter: 'temperature'")
    assert handler.is_temperature_rejection(temp) is True
    assert handler.is_temperature_rejection(temp) is True  # re-readable
    assert handler.is_model_validation_error(temp) is True  # still the permanent class
    assert handler.is_temperature_rejection(
        UrlHTTPError("u", 422, "x", {}, BytesIO(b"temperature"))) is False  # 400 only
    assert handler.is_temperature_rejection(_temperature_rejected()) is True
    assert handler.is_temperature_rejection(_too_long("Converse")) is False
    assert handler.is_temperature_rejection(RuntimeError("temperature")) is False
    calls = []

    def post(url, headers, body):
        calls.append(json.loads(body))
        raise _openai_400(url, "This model's maximum context length is 8192 tokens")

    chat = handler.OpenAIChat(base_url="https://example.invalid", api_key="test",
                              api_model_name="model", post=post, temperature=True)
    with pytest.raises(UrlHTTPError):
        chat.converse_text("system", "user")
    assert len(calls) == 1 and chat.temperature is True  # not a temperature refusal: no retry


# ── N17-obs255 (rev-r22 on obs246): the sweep re-checks S3 and respects later writes ─────────
class StaleListingS3(FakeS3):
    """`list_objects_v2` answers from a listing captured at construction (a concurrent /
    duplicate sync that listed BEFORE another job indexed a new object); head / get are live."""

    def __init__(self, live: FakeS3) -> None:
        super().__init__()
        self.objects = live.objects  # shared, live
        self._captured = {k: dict(v) for k, v in live.objects.items()}

    def list_objects_v2(self, **kw):
        live, self.objects = self.objects, self._captured
        try:
            return super().list_objects_v2(**kw)
        finally:
            self.objects = live


def test_a_sync_with_a_stale_listing_does_not_sweep_a_document_another_job_just_indexed(handler):
    """rev-r22's repro: capture the listing, upload y-new.md, sync (Y INDEXED), then run the
    sync that listed earlier → before the fix `{unchanged: 3, deleted: 1}` with Y still in S3
    but its record and chunks gone. Now `HeadObject` says Y is present → kept."""
    deps, os_, embedder = _incremental_world(handler)
    _ingest(handler, deps)
    stale = StaleListingS3(deps.s3)  # listed before Y existed
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "y-new.md", Body=b"Y is new.")
    out = _ingest(handler, deps, "ij2")
    assert out["counts"]["numberOfNewDocumentsIndexed"] == 1
    assert _doc_records(deps)["y-new.md"]["status"] == "INDEXED"
    late = dataclasses.replace(deps, s3=stale)
    out = _ingest(handler, late, "ij3")
    assert out["counts"]["numberOfDocumentsUnchanged"] == 3
    assert "numberOfDocumentsDeleted" not in out["counts"]
    assert "y-new.md" in _doc_records(deps)
    chunks = deps.opensearch.indices["launchpad-kb-kb1"].values()
    assert any(d["doc_name"] == "y-new.md" for d in chunks)
    # a document that IS gone from S3 is still swept
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": KB_PREFIX + "a-policy.md"}]})
    out = _ingest(handler, deps, "ij4")
    assert out["counts"]["numberOfDocumentsDeleted"] == 1
    assert "a-policy.md" not in _doc_records(deps)


def test_a_record_indexed_after_this_jobs_listing_is_never_swept(handler):
    """The other guard: a record whose `indexed_at` is later than this job's listing time is
    the other job's work — kept even when this job cannot see its object."""
    deps, os_, embedder = _incremental_world(handler)
    later = dataclasses.replace(deps, now=lambda: T0.replace(minute=5))
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "y-new.md", Body=b"Y is new.")
    _ingest(handler, later)  # Y indexed at T0+5
    assert _doc_records(deps)["y-new.md"]["indexed_at"] == T0.replace(minute=5).isoformat()
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": KB_PREFIX + "y-new.md"}]})
    earlier = dataclasses.replace(deps, now=lambda: T0)  # this job "listed" at T0, before Y
    out = _ingest(handler, earlier, "ij2")
    assert "numberOfDocumentsDeleted" not in out["counts"]
    assert "y-new.md" in _doc_records(deps)  # written after our listing → not ours to sweep
    out = _ingest(handler, later, "ij3")  # a job listing after the delete, and after Y's write
    assert out["counts"].get("numberOfDocumentsDeleted") == 1


# ── rev-r22 nit on obs245: a failed batch leaves no half-written chunks; no host in reasons ──
def test_a_batch_failure_after_earlier_batches_leaves_no_partial_chunks_and_no_hostname(handler):
    import urllib3

    os_ = BatchRecordingOpenSearch()
    error = urllib3.exceptions.ProtocolError(
        "('Connection aborted.', RemoteDisconnected) search-lp-memory.cn-northwest-1.example")
    deps = _kb_deps(handler, os_)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "trip.html", Body=LONG_HTML)
    # arm the failure on the SECOND bulk call only
    real_bulk = os_.bulk

    def bulk(index, docs, refresh=True):
        if len(os_.batches) == 1:
            os_.batches.append(len(docs))
            raise error
        return real_bulk(index, docs, refresh)

    os_.bulk = bulk
    out = _ingest(handler, deps)
    assert out["counts"]["numberOfDocumentsFailed"] == 1
    record = _doc_records(deps)["trip.html"]
    assert record["status"] == "FAILED"
    assert record["status_reason"] == ("ProtocolError: transport error while indexing — run "
                                       "the sync again")
    assert "search-lp-memory" not in record["status_reason"]
    assert os_.batches[:2] == [16, 16]  # the first batch was written before the second failed
    assert not any(d["doc_name"] == "trip.html"
                   for d in deps.opensearch.indices["launchpad-kb-kb1"].values())  # swept
    # an OpenSearch 400 keeps its (host-free) body, as before
    assert handler._stored_reason(core.OpenSearchError(400, "mapper_parsing_exception")) == (
        "OpenSearchError: OpenSearch HTTP 400: mapper_parsing_exception")


# ── N17-obs266 (rev-r22 on obs247): a re-run planner never completes early or double-counts ──
def _doc_messages(sqs, run=None):
    return [m for _, m in sqs.sent if m and (run is None or m.get("run") == run)]


def test_a_redelivered_planner_re_plans_under_a_new_run_and_the_old_rows_do_not_count(handler):
    """rev-r22's repro: ingest (3 dispatched), process A, ingest again with the SAME job id →
    before the fix B's result completed the job (2 ≥ the new expected, counting A's old row)
    while C had no record and 3 messages were pending; the final counts counted A twice. Now
    each planner run has a `run` stamp: rows and messages of run 1 are invisible to run 2's
    plan, stale run-1 messages are acked without work."""
    deps, os_, embedder, sqs = _fanout_world(handler)
    first = _ingest(handler, deps)
    run1 = _job_doc(deps)["fanout"]["run"]
    assert first["dispatched"] == 3 and len(run1) == 12
    by_name = {m["document"]["name"]: m for m in _doc_messages(sqs, run1)}
    assert handler.process_message(by_name["a-policy.md"], deps)["status"] == "INDEXED"
    second = _ingest(handler, deps, "ij1")  # the same job id, redelivered
    run2 = _job_doc(deps)["fanout"]["run"]
    assert run2 != run1 and second["dispatched"] == 2  # A is unchanged now
    assert second["counts"]["numberOfDocumentsUnchanged"] == 1
    run2_msgs = {m["document"]["name"]: m for m in _doc_messages(sqs, run2)}
    assert set(run2_msgs) == {"b-trip.html", "c-guide.pdf"}
    assert handler.process_message(run2_msgs["b-trip.html"], deps)["status"] == "INDEXED"
    assert _job_doc(deps)["status"] == "IN_PROGRESS"  # A's run-1 row does not count
    stale = handler.process_message(by_name["c-guide.pdf"], deps)  # run-1 message for C
    assert stale["status"] == "STALE" and "c-guide.pdf" not in _doc_records(deps)
    assert _job_doc(deps)["status"] == "IN_PROGRESS"
    assert handler.process_message(run2_msgs["c-guide.pdf"], deps)["status"] == "INDEXED"
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE"
    assert _counts({"counts": job["counts"]}) == {
        "numberOfDocumentsScanned": 3, "numberOfNewDocumentsIndexed": 2,
        "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 1}
    assert handler.process_message(by_name["b-trip.html"], deps)["status"] == "STALE"
    assert _job_doc(deps)["counts"]["numberOfNewDocumentsIndexed"] == 2  # unchanged by stale
    rows = _result_rows(deps)
    assert sorted(r["run"] for r in rows) == sorted([run1, run2, run2])
    # a planner redelivered for a COMPLETE job re-plans nothing
    third = _ingest(handler, deps, "ij1")
    assert third["skipped"] == "already completed" and len(sqs.sent) == 5
    assert _job_doc(deps)["counts"]["numberOfNewDocumentsIndexed"] == 2


def test_a_planner_that_raises_after_the_fan_out_is_redelivered_and_the_job_still_ends_right(
    handler,
):
    deps, os_, embedder, sqs = _fanout_world(handler)
    real_request = os_.request
    state = {"armed": True}

    def flaky_request(method, path, body=None, params=None):
        if state["armed"] and path.endswith("/_refresh"):
            state["armed"] = False  # the planner's refresh after the fan-out dies once
            raise core.OpenSearchError(503, "busy")
        return real_request(method, path, body, params)

    os_.request = flaky_request
    with pytest.raises(handler.RetryableError):
        _ingest(handler, deps)  # 3 messages already left under run 1
    run1_msgs = _doc_messages(sqs)
    assert len(run1_msgs) == 3 and _job_doc(deps)["status"] == "FAILED"
    out = _ingest(handler, deps, "ij1")  # the redelivery
    run2 = _job_doc(deps)["fanout"]["run"]
    assert out["dispatched"] == 3 and _job_doc(deps)["status"] == "IN_PROGRESS"
    for message in run1_msgs:
        assert handler.process_message(message, deps)["status"] == "STALE"
    assert _job_doc(deps)["status"] == "IN_PROGRESS" and _result_rows(deps) == []
    for message in _doc_messages(sqs, run2):
        assert handler.process_message(message, deps)["status"] == "INDEXED"
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 3
    assert job["counts"]["numberOfDocumentsUnchanged"] == 0


def test_each_finished_document_touches_the_jobs_updated_at(handler):
    """rev-r22 (b): the hub's stalled rule reads `updated_at`; a document finishing is progress."""
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    later = dataclasses.replace(deps, now=lambda: T0.replace(minute=7))
    handler.process_message(_doc_messages(sqs)[0], later)
    assert _job_doc(deps)["updated_at"] == T0.replace(minute=7).isoformat()
    assert _job_doc(deps)["status"] == "IN_PROGRESS"


# ── rev-r22 (c)/(d): model resolution inside the bookkeeping; OpenAI 4xx is permanent ───────
class _RefusingControl:
    def get_api_key_credential_provider(self, name):
        raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                     "Message": f"{name} does not exist"}},
                          "GetApiKeyCredentialProvider")


def test_an_unresolvable_embedding_row_fails_the_memory_job_with_a_readable_reason(
    handler, monkeypatch, caplog,
):
    handler._EMBEDDERS.clear()
    handler._BEARER.clear()
    deps = _deps(handler, ['["A"]', "[]", "s"])
    deps = dataclasses.replace(deps, control=_RefusingControl(), secrets=_FakeSecrets())
    key, _ = _event(deps, "hello", "hi")
    body = {**_msg(key), "embedding": _openai_block()}
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    event = {"Records": [{"messageId": "m1", "body": json.dumps(body)}]}
    assert handler.lambda_handler(event, None) == {"batchItemFailures": []}  # dropped, not DLQ
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(body))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_resolution"
    assert job["failure_reason"].startswith("model_resolution: ClientError: ")
    assert "ResourceNotFoundException" in job["failure_reason"]
    assert any("outcome=dropped" in r.getMessage() for r in caplog.records)


def test_an_unresolvable_embedding_row_fails_the_kb_job_and_its_documents(handler):
    handler._EMBEDDERS.clear()
    handler._BEARER.clear()
    deps, os_, embedder, sqs = _fanout_world(handler)
    deps = dataclasses.replace(deps, control=_RefusingControl(), secrets=_FakeSecrets())
    body = {"kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1", "ds_id": "DS1",
            "job_id": "ij1", "bucket": BUCKET, "prefix": KB_PREFIX, "index": "launchpad-kb-kb1",
            "embedding": _openai_block()}
    with pytest.raises(handler.DroppedMessage, match="model_resolution"):
        handler.process_message(body, deps)
    job = _job_doc(deps)
    assert job["status"] == "FAILED" and job["failure_code"] == "model_resolution"
    # a document message meeting the same failure records the document FAILED and still counts
    good = dataclasses.replace(deps, control=None, secrets=None)
    _ingest(handler, good, "ij2")
    message = {**_doc_messages(sqs)[0], "embedding": _openai_block()}
    out = handler.process_message(message, deps)
    assert out["status"] == "FAILED"
    name = message["document"]["name"]
    assert _doc_records(deps)[name]["status_reason"].startswith("model_resolution: ")


def test_an_openai_compatible_4xx_is_permanent_like_the_bedrock_validation_error(handler):
    from urllib.error import HTTPError as UrlHTTPError

    def refused(url, headers, body):
        raise UrlHTTPError(url, 400, "Bad Request", {}, None)

    handler._CHATS.clear()
    handler._BEARER.clear()
    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, control=_FakeControl(), secrets=_FakeSecrets(),
                               http_post=refused)
    key, _ = _event(deps, "I like tea", "noted")
    block = _extract_block(model_id="siliconflow/deepseek-v4", kind="openai_compatible",
                           base_url="https://api.siliconflow.cn/v1",
                           api_model_name="deepseek-ai/DeepSeek-V4",
                           credential_provider="launchpad-siliconflow", provider="siliconflow")
    with pytest.raises(handler.ModelValidationFailure) as info:  # N17-obs535: redelivered
        handler.process_message({**_msg(key), "extract_model": block}, deps)
    assert info.value.model == "deepseek-ai/DeepSeek-V4"
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_validation"
    assert handler._is_transient(UrlHTTPError("u", 429, "slow", {}, None)) is True
    assert handler._is_transient(UrlHTTPError("u", 400, "bad", {}, None)) is False
    assert handler.is_model_validation_error(UrlHTTPError("u", 422, "bad", {}, None)) is True


# ── rev-r22: a 401 / 403 from an OpenAI-compatible provider re-resolves the key once ────────
class _RotatingSecrets:
    """`key-v1` on the first read, `key-v2` afterwards — the key rotated after it was cached."""

    def __init__(self):
        self.reads = 0

    def get_secret_value(self, *, SecretId):
        self.reads += 1
        return {"SecretString": json.dumps({"api_key": "key-v1" if self.reads == 1 else "key-v2"})}


def _refusing_post(accept: str, reply_factory):
    from urllib.error import HTTPError as UrlHTTPError

    attempts = []

    def post(url, headers, body):
        attempts.append(headers["Authorization"])
        if headers["Authorization"] != f"Bearer {accept}":
            raise UrlHTTPError(url, 401, "Unauthorized", {}, None)
        return reply_factory()

    post.attempts = attempts
    return post


def test_a_401_from_the_embedding_provider_re_resolves_the_key_once_and_the_sync_goes_on(handler):
    handler._EMBEDDERS.clear()
    handler._BEARER.clear()
    control, secrets = _FakeControl(), _RotatingSecrets()
    post = _refusing_post("key-v2", lambda: json.dumps(
        {"data": [{"embedding": [0.25] * 16}]}).encode())
    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, control=control, secrets=secrets, http_post=post)
    emb = handler.embedder_for({"embedding": _openai_block()}, deps)
    assert emb.embed("hello") == [0.25] * 16
    assert post.attempts == ["Bearer key-v1", "Bearer key-v2"]  # refused once, healed
    assert control.names == ["launchpad-siliconflow"] * 2 and secrets.reads == 2
    assert handler._BEARER["launchpad-siliconflow"] == "key-v2"
    assert emb.embed("again") == [0.25] * 16 and post.attempts[-1] == "Bearer key-v2"  # kept
    assert len(post.attempts) == 3


def test_a_401_that_survives_the_re_resolution_is_permanent(handler):
    handler._CHATS.clear()
    handler._BEARER.clear()
    control, secrets = _FakeControl(), _RotatingSecrets()
    post = _refusing_post("never-accepted", lambda: b"{}")
    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, control=control, secrets=secrets, http_post=post)
    key, _ = _event(deps, "I like tea", "noted")
    block = _extract_block(model_id="siliconflow/deepseek-v4", kind="openai_compatible",
                           base_url="https://api.siliconflow.cn/v1",
                           api_model_name="deepseek-ai/DeepSeek-V4",
                           credential_provider="launchpad-siliconflow", provider="siliconflow")
    with pytest.raises(handler.ModelValidationFailure):  # N17-obs535: FAILED + redelivered
        handler.process_message({**_msg(key), "extract_model": block}, deps)
    assert post.attempts == ["Bearer key-v1", "Bearer key-v2"]  # exactly one re-resolution
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_validation"


def test_a_401_from_the_extraction_provider_heals_mid_extraction(handler):
    handler._CHATS.clear()
    handler._BEARER.clear()
    control, secrets = _FakeControl(), _RotatingSecrets()
    replies = ['["User likes tea"]', "[]", "Talked about tea."]
    post = _refusing_post("key-v2", lambda: json.dumps({"choices": [{"message": {
        "role": "assistant", "content": replies.pop(0)}}]}).encode())
    deps = _deps(handler, [])
    deps = dataclasses.replace(deps, control=control, secrets=secrets, http_post=post)
    key, _ = _event(deps, "I like tea", "noted")
    block = _extract_block(model_id="siliconflow/deepseek-v4", kind="openai_compatible",
                           base_url="https://api.siliconflow.cn/v1",
                           api_model_name="deepseek-ai/DeepSeek-V4",
                           credential_provider="launchpad-siliconflow", provider="siliconflow")
    out = handler.process_message({**_msg(key, merge=NO_ROLL), "extract_model": block}, deps)
    assert out["status"] == "COMPLETED" and out["counts"]["facts"] == 1
    assert post.attempts[:2] == ["Bearer key-v1", "Bearer key-v2"]
    assert all(a == "Bearer key-v2" for a in post.attempts[1:]) and len(post.attempts) == 4


# ── N17-obs270 (user approval 2026-10-22): one huge document is processed in slices ─────────
BIG_DOC = "\n\n".join(
    f"Section {i}: " + ("policy text line " * 45) for i in range(300)).encode()  # 300 chunks


class SliceRecordingOpenSearch(BatchRecordingOpenSearch):
    """+ the INDEXED kb-docs writes, the job's COMPLETE writes and the chunk deletes."""

    def __init__(self) -> None:
        super().__init__()
        self.indexed_writes: list[str] = []
        self.complete_writes: list[str] = []
        self.chunk_deletes: list[dict] = []

    def _note(self, index, doc_id, doc):
        if index == core.kb_docs_index("") and doc.get("status") == "INDEXED":
            self.indexed_writes.append(doc_id)
        if index == core.jobs_index("") and doc.get("status") == "COMPLETE" and \
                doc.get("kind") != "kb_ingest_doc_result":
            self.complete_writes.append(doc_id)

    def put_doc(self, index, doc_id, doc, refresh=True):
        self._note(index, doc_id, doc)
        return super().put_doc(index, doc_id, doc, refresh)

    def update_doc(self, index, doc_id, fields, refresh=True):
        self._note(index, doc_id, fields)
        return super().update_doc(index, doc_id, fields, refresh)

    def delete_by_query(self, index, query):
        if index == "launchpad-kb-kb1":
            self.chunk_deletes.append(query)
        return super().delete_by_query(index, query)

    def request(self, method, path, body=None, params=None):
        out = super().request(method, path, body, params)
        script = (body or {}).get("script") if isinstance(body, dict) else None
        if script and path.startswith(f"/{core.jobs_index('')}/_update/") and \
                out.get("result") == "updated" and \
                script["params"]["fields"].get("status") == "COMPLETE":
            self.complete_writes.append(path.rsplit("/", 1)[-1])  # N17-obs279: scripted
        return out


def _sliced_world(handler, sqs=None, clock=None):
    os_ = SliceRecordingOpenSearch()
    deps = _kb_deps(handler, os_)
    sqs = sqs or FakeSQS()
    deps = dataclasses.replace(deps, sqs=sqs, queue_url=QUEUE_URL, clock=clock or (lambda: 0.0))
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "handbook.md", Body=BIG_DOC)
    text, _ = handler._doc_text("handbook.md", BIG_DOC)
    assert len(core.chunk_text(text)) == 300
    return deps, os_, sqs


def _drain(handler, deps, sqs, start=0):
    """Process every message sent from position ``start`` on (continuations included);
    returns the statuses in order."""
    statuses, i = [], start
    while i < len(sqs.sent):
        _, message = sqs.sent[i]
        statuses.append(handler.process_message(message, deps)["status"])
        i += 1
    return statuses


def test_a_300_chunk_document_is_indexed_in_five_slices_and_completes_once(handler):
    deps, os_, sqs = _sliced_world(handler)
    out = _ingest(handler, deps)
    assert out["status"] == "IN_PROGRESS" and out["dispatched"] == 1
    refreshes_after_planner = len(os_.refreshes)
    statuses, i = [], 0
    while i < len(sqs.sent):
        _, message = sqs.sent[i]
        if i < 4:  # before the last slice: nothing INDEXED, no doc refresh, no result row
            assert os_.indexed_writes == [] and _result_rows(deps) == []
            assert len(os_.refreshes) == refreshes_after_planner
            assert _job_doc(deps)["status"] == "IN_PROGRESS"
        statuses.append(handler.process_message(message, deps)["status"])
        i += 1
    assert statuses == ["CONTINUE"] * 4 + ["INDEXED"]
    continuations = [m for _, m in sqs.sent[1:]]
    assert [m["slice_start"] for m in continuations] == [64, 128, 192, 256]
    assert all(m["kind"] == "kb_ingest_doc" and m["run"] == sqs.sent[0][1]["run"]
               and m["document"]["etag"] for m in continuations)
    assert os_.batches == [16] * 16 + [16, 16, 12]  # 4 slices of 64, then 44
    chunks = deps.opensearch.indices["launchpad-kb-kb1"]
    assert sorted(d["chunk_index"] for d in chunks.values()) == list(range(300))
    # N17-obs274: slice 0 and the last slice delete OTHER syncs' chunks only — never their own
    assert len(os_.chunk_deletes) == 2
    assert all(q["bool"]["must_not"] == [{"term": {"ingest": f"ij1:{sqs.sent[0][1]['run']}"}}]
               for q in os_.chunk_deletes)
    assert {d["ingest"] for d in chunks.values()} == {f"ij1:{sqs.sent[0][1]['run']}"}
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == 300
    assert len(os_.indexed_writes) == 1 and os_.complete_writes == ["ij1"]
    assert len(_result_rows(deps)) == 1
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 1


class _SlowClock:
    """A clock the embedder advances: every embedding call costs ``per_call`` seconds of
    wall time (a degraded model), serialised so the parallel round costs the sum."""

    def __init__(self, per_call: float) -> None:
        import threading

        self.now, self.per_call, self.lock = 0.0, per_call, threading.Lock()

    def __call__(self) -> float:
        return self.now

    def tick(self) -> None:
        with self.lock:
            self.now += self.per_call


class _SlowEmbedder(FakeEmbedder):
    def __init__(self, clock: _SlowClock) -> None:
        self.clock = clock

    def embed(self, text):
        self.clock.tick()
        return super().embed(text)


def test_a_slice_is_cut_before_the_group_that_would_pass_the_budget(handler):
    """N17-obs274 (rev-r22 on obs270): the deadline is checked BEFORE each embedding group of
    `EMBED_WORKERS`, projected with the slowest group so far — not after a 16-chunk group has
    already overrun. A model at 5 s per call → 20 s per group of 4: groups end at 20, 40, 60 s;
    before the 4th, 60 + 20 > 70 → stop at 12 chunks, the embedding done by 60 s."""
    clock = _SlowClock(5.0)
    deps, os_, sqs = _sliced_world(handler, clock=clock)
    deps = dataclasses.replace(deps, embedder=_SlowEmbedder(clock))
    _ingest(handler, deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    assert handler.EMBED_WORKERS == 4 and handler.SLICE_BUDGET_S == 70.0
    assert sqs.sent[1][1]["slice_start"] == 12 and os_.batches == [12]
    assert clock.now == 60.0  # the embedding stopped inside the budget
    assert handler.SLICE_BUDGET_S + handler.SLICE_RESERVE_S == 120.0  # conservative KB slice
    # a healthy model (no clock movement) still fills the whole 64-chunk slice
    fast = _sliced_world(handler)
    _ingest(handler, fast[0])
    handler.process_message(fast[2].sent[0][1], fast[0])
    assert fast[2].sent[1][1]["slice_start"] == 64


class _LambdaKilled(BaseException):
    """The invocation dies mid-slice (timeout / OOM) — nothing in the handler catches it."""


def test_a_crash_mid_slice_is_redone_idempotently_on_redelivery(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    second = sqs.sent[1][1]
    real_bulk = os_.bulk
    calls = {"n": 0}

    def dying_bulk(index, docs, refresh=True):  # the slice's 2nd batch dies
        calls["n"] += 1
        if calls["n"] == 2:
            raise _LambdaKilled()
        return real_bulk(index, docs, refresh)

    os_.bulk = dying_bulk
    with pytest.raises(_LambdaKilled):
        handler.process_message(second, deps)
    assert len(sqs.sent) == 2 and _result_rows(deps) == []  # no continuation, no row
    os_.bulk = real_bulk
    assert handler.process_message(second, deps)["status"] == "CONTINUE"  # the redelivery
    assert sqs.sent[2][1]["slice_start"] == 128
    assert _drain(handler, deps, sqs, start=2) == ["CONTINUE"] * 2 + ["INDEXED"]
    chunks = deps.opensearch.indices["launchpad-kb-kb1"]
    assert len(chunks) == 300  # the redone slice rewrote the same sha1(uri#i) ids
    assert sorted(d["chunk_index"] for d in chunks.values()) == list(range(300))
    assert len(os_.chunk_deletes) == 2 and len(_result_rows(deps)) == 1
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_a_continuation_that_cannot_be_enqueued_redelivers_its_slice(handler):
    class RefusingOnceSQS(FakeSQS):
        refused = False

        def send_message(self, **kw):
            if len(self.sent) == 1 and not self.refused:
                self.refused = True
                raise RuntimeError("queue hiccup")
            return super().send_message(**kw)

    deps, os_, sqs = _sliced_world(handler, sqs=RefusingOnceSQS())
    _ingest(handler, deps)
    with pytest.raises(handler.RetryableError, match="continuation"):
        handler.process_message(sqs.sent[0][1], deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    assert _drain(handler, deps, sqs, start=1)[-1] == "INDEXED"
    assert len(deps.opensearch.indices["launchpad-kb-kb1"]) == 300
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_a_continuation_of_a_superseded_planner_run_is_acked_stale(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    stale_continuation = sqs.sent[1][1]
    again = _ingest(handler, deps, "ij1")  # the planner redelivered: a new run re-dispatches
    assert again["dispatched"] == 1 and sqs.sent[2][1]["run"] != stale_continuation["run"]
    batches = len(os_.batches)
    out = handler.process_message(stale_continuation, deps)
    assert out["status"] == "STALE" and len(os_.batches) == batches and len(sqs.sent) == 3
    assert _result_rows(deps) == []
    assert _drain(handler, deps, sqs, start=2)[-1] == "INDEXED"
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_a_document_replaced_mid_ingest_stops_and_the_next_sync_takes_it(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "handbook.md",
                       Body=BIG_DOC.replace(b"policy", b"revised"))  # a new ETag
    batches = len(os_.batches)
    out = handler.process_message(sqs.sent[1][1], deps)
    assert out["status"] == "SUPERSEDED" and len(sqs.sent) == 2  # no further continuation
    assert len(os_.batches) == batches  # nothing of the replaced version written
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "FAILED" and record["status_reason"] == handler.SUPERSEDED_REASON
    # N17-obs274: this sync's partial V1 does not stay searchable while the record says FAILED
    assert deps.opensearch.indices["launchpad-kb-kb1"] == {}
    assert os_.indexed_writes == []
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfDocumentsFailed"] == 1
    assert [r["status"] for r in _result_rows(deps)] == ["SUPERSEDED"]
    # the next sync sees a changed document and indexes the new version whole
    nxt = _ingest(handler, deps)
    assert nxt["dispatched"] == 1
    assert _drain(handler, deps, sqs, start=2)[-1] == "INDEXED"
    chunks = deps.opensearch.indices["launchpad-kb-kb1"].values()
    assert len(chunks) == 300 and all("revised" in d["text"] for d in chunks)
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"


def test_a_replaced_document_already_indexed_by_a_newer_sync_keeps_its_record(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    old_continuation = sqs.sent[1][1]
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "handbook.md", Body=b"Short now.")
    _ingest(handler, deps)  # ij2 indexes the new version (one slice)
    assert handler.process_message(sqs.sent[2][1], deps)["status"] == "INDEXED"
    assert handler.process_message(old_continuation, deps)["status"] == "SUPERSEDED"
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == 1  # untouched


def test_a_document_deleted_mid_ingest_stops_as_superseded(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": KB_PREFIX + "handbook.md"}]})
    assert handler.process_message(sqs.sent[1][1], deps)["status"] == "SUPERSEDED"
    assert _job_doc(deps)["status"] == "COMPLETE"
    # N17-obs277: no record for a document that is gone (its own INDEXING record removed)
    assert "handbook.md" not in _doc_records(deps) and _doc_chunks(deps) == []


# ── N17-obs273 (deploy-ops-35 live 2026-10-22): the job `updated_at` bump tolerates conflicts ─
def _conflicting_job_updates(os_, job_id="ij1"):
    """Every `updated_at` bump (`_update` with a `doc`) of the job document answers 409
    version_conflict_engine_exception — the residual case, after OpenSearch's own
    `retry_on_conflict` attempts ran out. (N17-obs279: the scripted COMPLETE write is a
    different `_update`, not conflicted here.)"""
    real_request = os_.request
    seen: list[dict] = []

    def request(method, path, body=None, params=None):
        if path == f"/{core.jobs_index('')}/_update/{job_id}" and "doc" in (body or {}):
            seen.append(dict(params or {}))
            raise core.OpenSearchError(409, "version_conflict_engine_exception")
        return real_request(method, path, body, params)

    os_.request = request
    return seen


def test_a_conflicting_job_bump_never_turns_a_finished_document_into_a_retry(
    handler, monkeypatch, caplog,
):
    """Live: 2 of 3 concurrent document invocations logged `outcome=retry` ("result bookkeeping
    unavailable after the document: OpenSearchError") on the shared job's `updated_at` bump,
    were redelivered after the 720 s visibility timeout and acked DUPLICATE. Now the bump asks
    for `retry_on_conflict` and a residual 409 is not an error: every message is acked once,
    the job completes, and the log line names the job."""
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    conflicts = _conflicting_job_updates(os_)
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    caplog.set_level(logging.INFO, logger="launchpad.memory_extractor")
    docs = {"Records": [{"messageId": f"d{i}", "body": json.dumps(m)}
                        for i, (_, m) in enumerate(sqs.sent)]}
    assert handler.lambda_handler(docs, None) == {"batchItemFailures": []}  # no redelivery
    assert len(conflicts) == 3
    assert all(p == {"refresh": "false", "retry_on_conflict": "5"} for p in conflicts)
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfNewDocumentsIndexed"] == 3
    assert len(_result_rows(deps)) == 3
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("message=d")]
    assert len(lines) == 3 and all("outcome=ok" in x and "job=ij1 " in x for x in lines)
    skipped = [r for r in caplog.records if "updated_at bump skipped" in r.getMessage()]
    assert len(skipped) == 3 and all(r.levelno == logging.INFO for r in skipped)


def test_the_job_bump_passes_retry_on_conflict_and_still_lands(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    later = dataclasses.replace(deps, now=lambda: T0.replace(minute=9))
    handler.process_message(_doc_messages(sqs)[0], later)
    [bump] = [r for r in os_.requests if r["path"] == f"/{core.jobs_index('')}/_update/ij1"]
    assert bump["method"] == "POST" and bump["body"] == {"doc": {
        "updated_at": T0.replace(minute=9).isoformat()}}
    assert bump["params"] == {"refresh": "false", "retry_on_conflict": "5"}
    assert _job_doc(deps)["updated_at"] == T0.replace(minute=9).isoformat()


def test_a_conflicting_bump_on_a_continuation_still_continues_exactly_once(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    _conflicting_job_updates(os_)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    assert len(sqs.sent) == 2  # one continuation, no RetryableError → no second chain
    assert _drain(handler, deps, sqs, start=1)[-1] == "INDEXED"
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_any_other_bump_failure_is_logged_not_retried(handler, caplog):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    real_request = os_.request

    def request(method, path, body=None, params=None):
        if "/_update/" in path:
            raise core.OpenSearchError(503, "busy")
        return real_request(method, path, body, params)

    os_.request = request
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    assert handler.process_message(_doc_messages(sqs)[0], deps)["status"] == "INDEXED"
    assert any("updated_at bump skipped (OpenSearchError 503)" in r.getMessage()
               for r in caplog.records if r.levelno == logging.WARNING)



# ── N17-obs274 (rev-r22 BLOCKING on obs270): twin chains and concurrent syncs keep every chunk ─
def _doc_chunks(deps):
    return list(deps.opensearch.indices["launchpad-kb-kb1"].values())


def test_a_late_duplicate_of_slice_0_never_deletes_its_own_chains_chunks(handler):
    """rev-r22's repro: chain A runs slices 0, 64, 128; slice 0 is redelivered (an SQS
    duplicate, or a crash after the continuation send) as chain B, which deleted EVERY chunk of
    the document and rewrote 0..63; chain A finished 192 and 256 (INDEXED, result row) and B's
    64 was a DUPLICATE → INDEXED with chunk_count 300 but only 172 chunks, and the next
    incremental sync skipped it. Now the twins share the stamp: slice 0 deletes only chunks of
    OTHER syncs, so all 300 stay."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    slice0 = sqs.sent[0][1]
    assert handler.process_message(slice0, deps)["status"] == "CONTINUE"        # A: 0..63
    assert handler.process_message(sqs.sent[1][1], deps)["status"] == "CONTINUE"  # A: 64..127
    assert handler.process_message(sqs.sent[2][1], deps)["status"] == "CONTINUE"  # A: 128..191
    assert handler.process_message(slice0, deps)["status"] == "CONTINUE"        # B: 0..63 again
    b_next = sqs.sent[4][1]
    assert b_next["slice_start"] == 64
    assert handler.process_message(sqs.sent[3][1], deps)["status"] == "CONTINUE"  # A: 192..255
    assert handler.process_message(sqs.sent[5][1], deps)["status"] == "INDEXED"   # A: 256..299
    assert handler.process_message(b_next, deps)["status"] == "DUPLICATE"
    chunks = _doc_chunks(deps)
    assert sorted(d["chunk_index"] for d in chunks) == list(range(300))
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == 300
    assert _job_doc(deps)["status"] == "COMPLETE" and len(_result_rows(deps)) == 1


def test_a_newer_sync_takes_the_document_and_the_older_chain_stops(handler):
    """Two syncs of one document at once (a double-clicked sync, an upload's sync + a manual
    one): the newer slice 0 takes the lease; the older chain's next slice stops (SUPERSEDED,
    its own chunks removed, the record left to the owner); the newer sync ends with all 300."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)                                    # ij1
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"  # ij1: 0..63
    old_next = sqs.sent[1][1]
    out = _ingest(handler, deps)                              # ij2 (nothing INDEXED yet)
    assert out["dispatched"] == 1
    assert handler.process_message(sqs.sent[2][1], deps)["status"] == "CONTINUE"  # ij2: 0..63
    assert handler.process_message(old_next, deps)["status"] == "SUPERSEDED"      # ij1 stops
    assert len(sqs.sent) == 4  # the old chain sent nothing more
    assert _job_doc(deps, "ij1")["status"] == "COMPLETE"
    assert _job_doc(deps, "ij1")["counts"]["numberOfDocumentsFailed"] == 1
    record = _doc_records(deps)["handbook.md"]  # the old chain wrote nothing after losing
    assert record["status"] == "INDEXING" and record["ingest"].startswith("ij2:")
    assert _drain(handler, deps, sqs, start=3)[-1] == "INDEXED"
    chunks = _doc_chunks(deps)
    run2 = sqs.sent[2][1]["run"]
    assert sorted(d["chunk_index"] for d in chunks) == list(range(300))
    assert {d["ingest"] for d in chunks} == {f"ij2:{run2}"}
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"
    assert _job_doc(deps, "ij2")["status"] == "COMPLETE"


def test_an_older_slice_writing_after_the_newer_slice_0_leaves_no_trace(handler):
    """The interleave the last-slice delete covers: an older chain's slice is past its lease
    check (embedding) when the newer sync's slice 0 deletes; it then writes its chunks with its
    own stamp. Those never overwrite the newer sync's chunks (stamped ids) and leave the index
    with the older chain's next slice (lease lost) or the newer sync's last slice."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)                                    # ij1
    handler.process_message(sqs.sent[0][1], deps)             # ij1: 0..63
    _ingest(handler, deps)                                    # ij2 → sqs.sent[2]
    newer_slice0 = sqs.sent[2][1]

    class InterleavingEmbedder(FakeEmbedder):
        fired = False

        def embed(self, text):
            if not InterleavingEmbedder.fired:
                InterleavingEmbedder.fired = True  # mid-embedding of ij1's slice 64..127
                assert handler.process_message(newer_slice0, plain)["status"] == "CONTINUE"
            return super().embed(text)

    plain = deps
    racing = dataclasses.replace(deps, embedder=InterleavingEmbedder())
    assert handler.process_message(sqs.sent[1][1], racing)["status"] == "CONTINUE"  # ij1 wrote
    run1, run2 = sqs.sent[0][1]["run"], newer_slice0["run"]
    stamps = {d["ingest"] for d in _doc_chunks(deps)}
    assert stamps == {f"ij1:{run1}", f"ij2:{run2}"}  # side by side, nothing overwritten
    old_next = next(m for _, m in sqs.sent if m.get("run") == run1 and m.get("slice_start") == 128)
    assert handler.process_message(old_next, deps)["status"] == "SUPERSEDED"
    assert {d["ingest"] for d in _doc_chunks(deps)} == {f"ij2:{run2}"}
    new_msgs = [i for i, (_, m) in enumerate(sqs.sent) if m.get("run") == run2 and m.get(
        "slice_start")]
    assert _drain(handler, deps, sqs, start=new_msgs[0])[-1] == "INDEXED"
    chunks = _doc_chunks(deps)
    assert sorted(d["chunk_index"] for d in chunks) == list(range(300))
    assert {d["ingest"] for d in chunks} == {f"ij2:{run2}"}


def test_the_last_slice_removes_another_syncs_chunks_written_meanwhile(handler):
    """Belt for the lease: chunks of another sync that landed after this sync's slice 0 (no
    lease row to stop them — an older Lambda's chain) leave with this sync's last slice."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    uri = f"s3://{BUCKET}/{KB_PREFIX}handbook.md"
    deps.opensearch.put_doc("launchpad-kb-kb1", "foreign", {
        "doc_uri": uri, "chunk_index": 70, "text": "stale", "ingest": "ij0:old"})
    _drain(handler, deps, sqs, start=1)
    assert "foreign" not in deps.opensearch.indices["launchpad-kb-kb1"]
    assert len(_doc_chunks(deps)) == 300


def test_slice_0_removes_legacy_unstamped_chunks(handler):
    deps, os_, sqs = _sliced_world(handler)
    uri = f"s3://{BUCKET}/{KB_PREFIX}handbook.md"
    deps.opensearch.put_doc("launchpad-kb-kb1", "legacy", {"doc_uri": uri, "chunk_index": 0,
                                                           "text": "pre-obs274 chunk"})
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    assert "legacy" not in deps.opensearch.indices["launchpad-kb-kb1"]


def test_the_kb_index_gains_the_ingest_keyword_before_the_first_stamped_chunk(handler):
    handler._INGEST_MAPPED.clear()
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    props = deps.opensearch.bodies["launchpad-kb-kb1"]["mappings"]["properties"]
    assert props["ingest"] == {"type": "keyword"}
    puts = [r for r in os_.requests if r["method"] == "PUT" and r["path"].endswith("/_mapping")]
    assert len(puts) == 1
    handler.process_message(sqs.sent[1][1], deps)  # cached for the container: no second GET/PUT
    assert len([r for r in os_.requests if r["path"].endswith("/_mapping")]) == 2  # GET + PUT


# ── N17-obs274 (rev-r22 pass 2): the failure cleanup reads the lease; INDEXED means complete ─
def test_a_failing_slice_0_never_deletes_a_newer_owners_chunks(handler):
    """rev-r22's repro: during X's (ij1) first bulk, Y's (ij2) slice 0 runs in full — takes the
    lease, writes Y 0..63; X's batch then times out twice. The slice-0 shortcut made X the
    "owner": it deleted EVERY chunk of the document (Y's included) and wrote FAILED; Y drained
    to INDEXED with 236 of 300 chunks. Now X reads the lease, is not the owner, removes only its
    own chunks and leaves the record to Y."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)                       # ij1 → sqs.sent[0] (X)
    _ingest(handler, deps)                       # ij2 → sqs.sent[1] (Y)
    x_slice0, y_slice0 = sqs.sent[0][1], sqs.sent[1][1]
    real_bulk = os_.bulk
    state = {"y_ran": False, "x_timeouts": 0}

    def racing_bulk(index, docs, refresh=True):
        if docs and docs[0][1].get("ingest", "").startswith("ij1:"):
            if not state["y_ran"]:
                state["y_ran"] = True
                assert handler.process_message(y_slice0, deps)["status"] == "CONTINUE"
            if state["x_timeouts"] < 2:
                state["x_timeouts"] += 1
                raise _read_timeout()
        return real_bulk(index, docs, refresh)

    os_.bulk = racing_bulk
    assert handler.process_message(x_slice0, deps)["status"] == "SUPERSEDED"
    os_.bulk = real_bulk
    run2 = y_slice0["run"]
    assert {d["ingest"] for d in _doc_chunks(deps)} == {f"ij2:{run2}"}
    assert len(_doc_chunks(deps)) == 64  # Y's slice 0 untouched
    record = _doc_records(deps)["handbook.md"]  # X wrote no FAILED record: Y's INDEXING
    assert record["status"] == "INDEXING" and record["ingest"] == f"ij2:{run2}"
    assert _job_doc(deps, "ij1")["status"] == "COMPLETE"
    y_rest = next(i for i, (_, m) in enumerate(sqs.sent) if m.get("run") == run2
                  and m.get("slice_start") == 64)
    assert _drain(handler, deps, sqs, start=y_rest)[-1] == "INDEXED"
    assert sorted(d["chunk_index"] for d in _doc_chunks(deps)) == list(range(300))
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == 300


def test_an_unreadable_lease_on_failure_touches_only_this_syncs_chunks(handler):
    deps, os_, sqs = _sliced_world(handler)
    uri = f"s3://{BUCKET}/{KB_PREFIX}handbook.md"
    _ingest(handler, deps)
    real_get, real_bulk = os_.get_doc, os_.bulk
    state = {"failed": False}

    def failing_bulk(index, docs, refresh=True):
        # another sync's chunk lands after this slice 0's delete, then the write fails
        real_bulk(index, [("other", {"doc_uri": uri, "chunk_index": 1, "ingest": "ij0:x"})],
                  refresh)
        real_bulk(index, docs[:1], refresh)  # one chunk of this sync made it
        state["failed"] = True
        raise core.OpenSearchError(400, "mapper_parsing_exception")

    def blind_get(index, doc_id):
        # N17-obs278: the lease turns unreadable after the write failed (the cleanup's read);
        # slice 0's post-embedding re-check still reads it
        if doc_id.startswith("lease:") and state["failed"]:
            raise core.OpenSearchError(503, "busy")
        return real_get(index, doc_id)

    os_.bulk, os_.get_doc = failing_bulk, blind_get
    # pass 3: unreadable ≠ another owner — own chunks only, but the FAILED record IS written
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "FAILED"
    os_.bulk, os_.get_doc = real_bulk, real_get
    chunks = deps.opensearch.indices["launchpad-kb-kb1"]
    assert list(chunks) == ["other"]  # only this sync's own chunk removed
    assert _doc_records(deps)["handbook.md"]["status"] == "FAILED"


def test_a_last_slice_with_a_missing_chunk_is_recorded_failed_not_indexed(handler):
    """The completeness check (rev-r22 pass 2): after the refresh, this sync's chunks of the
    document must number `len(chunks)`; a hole (any race) is FAILED "incomplete index write —
    run the sync again", so the next incremental sync re-indexes instead of skipping it."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    real_bulk = os_.bulk

    def lossy_bulk(index, docs, refresh=True):
        return real_bulk(index, [d for d in docs if d[1].get("chunk_index") != 70], refresh)

    os_.bulk = lossy_bulk
    assert _drain(handler, deps, sqs)[-1] == "FAILED"
    os_.bulk = real_bulk
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "FAILED"
    assert record["status_reason"] == handler.INCOMPLETE_WRITE_REASON
    assert _doc_chunks(deps) == []  # pass 3 nit: its partial set is not left searchable
    assert record["status_reason"] == "incomplete index write — run the sync again"
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfDocumentsFailed"] == 1
    nxt = _ingest(handler, deps)  # not "unchanged": re-dispatched
    assert nxt["dispatched"] == 1
    _drain(handler, deps, sqs, start=len(sqs.sent) - 1)
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"
    assert len(_doc_chunks(deps)) == 300


def test_an_ingest_field_of_another_type_is_warned_about_not_remapped(handler, caplog):
    handler._INGEST_MAPPED.clear()
    deps, os_, sqs = _sliced_world(handler)
    deps.opensearch.bodies["launchpad-kb-kb1"]["mappings"]["properties"]["ingest"] = {
        "type": "text"}
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    _ingest(handler, deps)
    handler.process_message(sqs.sent[0][1], deps)
    assert not [r for r in os_.requests if r["method"] == "PUT" and r["path"].endswith("/_mapping")]
    assert any("maps `ingest` as 'text', not keyword" in r.getMessage() for r in caplog.records)
    handler._INGEST_MAPPED.clear()



# ── N17-obs274 (rev-r22 pass 3): an unreadable lease never leaves an empty INDEXED document ──
def _indexed_then_full_rebuild(handler):
    """S0 = ij1 indexes all 300 chunks (INDEXED); then a full rebuild ij2 runs its slice 0,
    which deletes ij1's chunks as not its own. Returns the world and ij2's slice-64 message."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    sent = len(sqs.sent)
    handler.process_message({"kind": "kb_ingest", "workspace_id": "default", "kb_id": "KB1",
                             "ds_id": "DS1", "job_id": "ij2", "bucket": BUCKET,
                             "prefix": KB_PREFIX, "index": "launchpad-kb-kb1", "full": True},
                            deps)
    assert handler.process_message(sqs.sent[sent][1], deps)["status"] == "CONTINUE"
    assert len(_doc_chunks(deps)) == 64  # ij1's 300 gone, ij2's 0..63 in
    return deps, os_, sqs, sqs.sent[sent + 1][1]


def _lease_reads_fail(os_, times):
    real_get = os_.get_doc
    state = {"left": times}

    def get_doc(index, doc_id):
        if doc_id.startswith("lease:") and state["left"] > 0:
            state["left"] -= 1
            raise OSError("connection reset")
        return real_get(index, doc_id)

    os_.get_doc = get_doc
    return lambda: setattr(os_, "get_doc", real_get)


def test_a_lease_read_error_before_any_work_redelivers_the_slice(handler):
    """rev-r22's repro, part (a): slice 64 of the rebuild meets two consecutive lease-GET
    errors. Before: SUPERSEDED, 0 chunks, the old INDEXED record (chunk_count 300, same ETag)
    kept — the next incremental sync skipped it for good. Now the top-of-slice read error is
    a RetryableError before any work; the redelivery finishes the rebuild."""
    deps, os_, sqs, slice64 = _indexed_then_full_rebuild(handler)
    batches, sent = len(os_.batches), len(sqs.sent)
    restore = _lease_reads_fail(os_, 2)
    with pytest.raises(handler.RetryableError, match="lease unavailable before slice 64"):
        handler.process_message(slice64, deps)
    assert len(os_.batches) == batches and len(sqs.sent) == sent  # no writes, no continuation
    assert len(_doc_chunks(deps)) == 64 and _result_rows(deps, "ij2") == []
    with pytest.raises(handler.RetryableError):
        handler.process_message(slice64, deps)  # the second failing read: still a redelivery
    restore()
    assert handler.process_message(slice64, deps)["status"] == "CONTINUE"
    assert _drain(handler, deps, sqs, start=sent)[-1] == "INDEXED"
    assert len(_doc_chunks(deps)) == 300
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"


def test_an_unreadable_lease_after_a_failed_slice_writes_failed_and_the_next_sync_repairs(
    handler,
):
    """Part (b): the top read succeeds, the slice's write fails, and the cleanup's lease read
    cannot be read. Only own chunks go (the rebuild's), but the record turns FAILED — not the
    stale INDEXED over an empty document — so the next incremental sync re-indexes it."""
    deps, os_, sqs, slice64 = _indexed_then_full_rebuild(handler)
    real_bulk, real_get = os_.bulk, os_.get_doc
    state = {"failed": False}

    def failing_bulk(index, docs, refresh=True):
        state["failed"] = True
        raise core.OpenSearchError(400, "mapper_parsing_exception")

    def get_doc(index, doc_id):
        if doc_id.startswith("lease:") and state["failed"]:
            raise OSError("connection reset")  # the cleanup's read
        return real_get(index, doc_id)

    os_.bulk, os_.get_doc = failing_bulk, get_doc
    assert handler.process_message(slice64, deps)["status"] == "FAILED"
    os_.bulk, os_.get_doc = real_bulk, real_get
    assert _doc_chunks(deps) == []
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "FAILED" and record["chunk_count"] == 0  # not INDEXED over 0
    assert _job_doc(deps, "ij2")["status"] == "COMPLETE"
    nxt = _ingest(handler, deps, "ij3")  # incremental: the FAILED record is "changed"
    assert nxt["dispatched"] == 1 and nxt["counts"]["numberOfDocumentsUnchanged"] == 0
    first_new = next(i for i, (_, m) in enumerate(sqs.sent) if m.get("job_id") == "ij3")
    assert _drain(handler, deps, sqs, start=first_new)[-1] == "INDEXED"
    assert len(_doc_chunks(deps)) == 300


# ── N17-obs274 (rev-r22 pass 4): slice 0 demotes the record to INDEXING ──────────────────────
def test_a_chain_that_dies_after_slice_0_leaves_indexing_and_the_next_sync_heals_it(handler):
    """rev-r22's repro: S1 indexes 300 (INDEXED); a full rebuild ij2's slice 0 deletes S1's
    chunks and writes 0..63; slice 64 is received 3 times, each a lease-GET error
    (RetryableError) → the DLQ. Before: the record stayed INDEXED chunk_count 300 over a
    64-chunk index and the next incremental sync counted it unchanged — permanently. Now slice
    0 wrote INDEXING first; the next incremental sync dispatches the document and ends 300."""
    deps, os_, sqs, slice64 = _indexed_then_full_rebuild(handler)
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXING" and record["chunk_count"] == 0
    assert record["ingest"].startswith("ij2:") and record["indexing_started_at"]
    restore = _lease_reads_fail(os_, 3)
    for _ in range(3):  # maxReceiveCount 3 → DLQ
        with pytest.raises(handler.RetryableError):
            handler.process_message(slice64, deps)
    restore()
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXING"  # never INDEXED over 64
    assert len(_doc_chunks(deps)) == 64
    nxt = _ingest(handler, deps, "ij3")
    assert nxt["dispatched"] == 1 and nxt["counts"]["numberOfDocumentsUnchanged"] == 0
    first = next(i for i, (_, m) in enumerate(sqs.sent) if m.get("job_id") == "ij3")
    assert _drain(handler, deps, sqs, start=first)[-1] == "INDEXED"
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "INDEXED" and record["chunk_count"] == 300
    assert len(_doc_chunks(deps)) == 300 and record["ingest"].startswith("ij3:")


def test_the_record_reads_indexing_between_slice_0_and_the_last_slice(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    seen = []
    i = 0
    while i < len(sqs.sent):
        handler.process_message(sqs.sent[i][1], deps)
        seen.append(_doc_records(deps)["handbook.md"]["status"])
        i += 1
    assert seen == ["INDEXING"] * 4 + ["INDEXED"]
    assert _doc_records(deps)["handbook.md"]["ingest"] == f"ij1:{sqs.sent[0][1]['run']}"


def test_a_late_twin_of_slice_0_does_not_demote_its_own_syncs_indexed_record(handler):
    """The window rev-r22 pass 4 opens: a twin of slice 0 that passed the DUPLICATE check just
    before its own chain wrote INDEXED (simulated by removing the result row) must not turn
    that record back to INDEXING — the record names the same stamp, so it is left alone."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    for row in _result_rows(deps):
        deps.opensearch.delete_doc(core.jobs_index(""), f"ij1:{row['run']}:{row['doc_id']}")
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"
    assert len(_doc_chunks(deps)) == 300  # same stamp: nothing deleted


# ── N17-obs278 (rev-r22): a single-slice chain that lost the lease never writes INDEXED ─────
def _one_slice_world(handler):
    """One small document (a single slice) and two concurrent syncs: ij1 (older) and ij2."""
    os_ = SliceRecordingOpenSearch()
    deps = _kb_deps(handler, os_)
    sqs = FakeSQS()
    deps = dataclasses.replace(deps, sqs=sqs, queue_url=QUEUE_URL, clock=lambda: 0.0)
    deps.s3.put_object(Bucket=BUCKET, Key=KB_PREFIX + "note.md",
                       Body=("Leave policy line. " * 40).encode())
    _ingest(handler, deps)  # ij1 → sqs.sent[0]
    _ingest(handler, deps)  # ij2 → sqs.sent[1] (nothing INDEXED yet)
    return deps, os_, sqs, sqs.sent[0][1], sqs.sent[1][1]


def test_a_single_slice_chain_losing_the_lease_mid_write_stops_superseded(handler):
    """Before: the last-slice lease re-check ran only for `slice_start > 0`, so the older chain
    (lease lost while it bulk-wrote) deleted the newer sync's chunks and recorded INDEXED under
    its own stamp. Now it stops SUPERSEDED, removes only its own chunks, and the newer sync's
    INDEXED record and chunks stand."""
    deps, os_, sqs, older, newer = _one_slice_world(handler)
    real_bulk = os_.bulk
    state = {"fired": False}

    def racing_bulk(index, docs, refresh=True):
        if docs and docs[0][1].get("ingest", "").startswith("ij1:") and not state["fired"]:
            state["fired"] = True  # the newer sync runs in full mid-write of the older one
            assert handler.process_message(newer, deps)["status"] == "INDEXED"
        return real_bulk(index, docs, refresh)

    os_.bulk = racing_bulk
    assert handler.process_message(older, deps)["status"] == "SUPERSEDED"
    os_.bulk = real_bulk
    run2 = newer["run"]
    chunks = list(deps.opensearch.indices["launchpad-kb-kb1"].values())
    assert chunks and {d["ingest"] for d in chunks} == {f"ij2:{run2}"}
    record = _doc_records(deps)["note.md"]
    assert record["status"] == "INDEXED" and record["ingest"] == f"ij2:{run2}"
    assert record["chunk_count"] == len(chunks)
    assert os_.indexed_writes == [newer["document"]["doc_id"]]  # none by the older chain
    assert _job_doc(deps, "ij1")["counts"]["numberOfDocumentsFailed"] == 1
    assert _job_doc(deps, "ij2")["counts"]["numberOfNewDocumentsIndexed"] == 1


def test_a_single_slice_chain_losing_the_lease_while_embedding_writes_nothing(handler):
    """The interleave the slice-0 re-check covers: the newer sync finishes while the older one
    is still embedding. The older one stops before its INDEXING demotion and its delete, so the
    newer INDEXED record and its chunks are never touched."""
    deps, os_, sqs, older, newer = _one_slice_world(handler)

    class InterleavingEmbedder(FakeEmbedder):
        fired = False

        def embed(self, text):
            if not InterleavingEmbedder.fired:
                InterleavingEmbedder.fired = True
                assert handler.process_message(newer, deps)["status"] == "INDEXED"
            return super().embed(text)

    batches = len(os_.batches)
    racing = dataclasses.replace(deps, embedder=InterleavingEmbedder())
    assert handler.process_message(older, racing)["status"] == "SUPERSEDED"
    newer_batches = len(os_.batches) - batches
    assert newer_batches == 1  # only the newer sync wrote chunks
    run2 = newer["run"]
    record = _doc_records(deps)["note.md"]
    assert record["status"] == "INDEXED" and record["ingest"] == f"ij2:{run2}"
    chunks = list(deps.opensearch.indices["launchpad-kb-kb1"].values())
    assert chunks and {d["ingest"] for d in chunks} == {f"ij2:{run2}"}
    assert record["chunk_count"] == len(chunks)


def test_an_unreadable_lease_at_the_slice_0_recheck_redelivers_with_nothing_written(handler):
    """N17-obs278: the re-check after slice 0's embedding cannot read the lease. Nothing is
    written yet, so it is a redelivery (`RetryableError`), never a FAILED record; the
    redelivered slice indexes the document."""
    deps, os_, sqs, older, newer = _one_slice_world(handler)
    real_get = os_.get_doc
    state = {"reads": 0}

    def get_doc(index, doc_id):
        if doc_id.startswith("lease:"):
            state["reads"] += 1
            if state["reads"] == 1:  # the first lease read of slice 0 = the re-check
                raise OSError("connection reset")
        return real_get(index, doc_id)

    os_.get_doc = get_doc
    with pytest.raises(handler.RetryableError, match="lease unavailable after embedding"):
        handler.process_message(older, deps)
    os_.get_doc = real_get
    assert os_.batches == [] and "note.md" not in _doc_records(deps)
    assert _result_rows(deps, "ij1") == []
    assert handler.process_message(older, deps)["status"] == "INDEXED"
    assert _doc_records(deps)["note.md"]["status"] == "INDEXED"


# ── N17-obs277 (rev-r22): a data source deleted mid-slice leaves no orphan chunks or record ──
def _delete_data_source(deps):
    """The hub's data-source delete (`kb_cleanup` scope `data_source`) in its step order:
    `s3` (purge the prefix) → `index` (chunks by `ds_id`) → `docs` (kb-docs rows by `ds_id`,
    then the extractor's result + lease rows, N17-obs276)."""
    keys = [k for (b, k) in deps.s3.objects if b == BUCKET and k.startswith(KB_PREFIX)]
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys]})
    deps.opensearch.delete_by_query("launchpad-kb-kb1", {"term": {"ds_id": "DS1"}})
    deps.opensearch.delete_by_query(core.kb_docs_index(""), {"term": {"ds_id": "DS1"}})
    deps.opensearch.delete_by_query(core.jobs_index(""), {"bool": {"filter": [
        {"term": {"kb_id": "KB1"}}, {"term": {"ds_id": "DS1"}},
        {"terms": {"kind": ["kb_ingest_doc_result", "kb_ingest_doc_lease"]}}]}})


def _leases(deps):
    return [d for d in deps.opensearch.indices[core.jobs_index("")].values()
            if d.get("kind") == "kb_ingest_doc_lease"]


def test_a_data_source_deleted_while_the_last_slice_writes_leaves_no_orphans(handler):
    """rev-r22's path: the last slice read its text before the delete; the delete's three
    steps ran while it bulk-wrote; it then wrote chunks and the INDEXED record after the
    `index` / `docs` sweeps — orphans of a deleted source. Now the HeadObject before the
    INDEXED write sees the purged object: own chunks removed, no record, SUPERSEDED."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    statuses = _drain_until_last(handler, deps, sqs)
    last = sqs.sent[-1][1]
    real_bulk = os_.bulk
    state = {"fired": False}

    def racing_bulk(index, docs, refresh=True):
        if not state["fired"]:
            state["fired"] = True
            _delete_data_source(deps)  # the whole delete, mid-write
        return real_bulk(index, docs, refresh)

    os_.bulk = racing_bulk
    assert handler.process_message(last, deps)["status"] == "SUPERSEDED"
    os_.bulk = real_bulk
    assert statuses == ["CONTINUE"] * 4
    assert _doc_chunks(deps) == []
    assert "handbook.md" not in _doc_records(deps)
    assert os_.indexed_writes == [] and _leases(deps) == []
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["counts"]["numberOfDocumentsFailed"] == 1


def _drain_until_last(handler, deps, sqs):
    """Process every slice but the last one (whose message stays the last sent)."""
    statuses, i = [], 0
    while True:
        out = handler.process_message(sqs.sent[i][1], deps)["status"]
        statuses.append(out)
        if sqs.sent[-1][1].get("slice_start", 0) + handler.SLICE_CHUNKS >= 300:
            return statuses
        i += 1


def test_the_source_check_runs_once_right_before_the_indexed_write(handler):
    deps, os_, sqs = _sliced_world(handler)
    heads = []
    real_head = deps.s3.head_object

    def head_object(**kw):
        heads.append(kw["Key"])
        return real_head(**kw)

    deps.s3.head_object = head_object
    _ingest(handler, deps)
    planned = len(heads)
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    assert heads[planned:] == [KB_PREFIX + "handbook.md"]  # one HeadObject, the last slice
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXED"


def test_a_source_check_that_cannot_tell_still_records_indexed(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)

    def head_object(**kw):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "busy"}}, "HeadObject")

    deps.s3.head_object = head_object
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    assert len(_doc_chunks(deps)) == 300


def test_a_document_deleted_before_slice_0_writes_no_record(handler):
    """Slice 0 meeting a missing object used to raise into the failure path and write a FAILED
    record — an orphan when its data source was being deleted. Now: SUPERSEDED, no record."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    _delete_data_source(deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "SUPERSEDED"
    assert "handbook.md" not in _doc_records(deps) and _doc_chunks(deps) == []
    assert _leases(deps) == []  # its own lease (taken first) goes too
    assert _job_doc(deps)["status"] == "COMPLETE"


def test_a_data_source_deleted_between_slices_leaves_no_record(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "CONTINUE"
    keys = [k for (b, k) in deps.s3.objects if b == BUCKET]
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [{"Key": k} for k in keys]})
    # only the S3 step ran so far: slice 0's INDEXING record and chunks are still there
    assert _doc_records(deps)["handbook.md"]["status"] == "INDEXING"
    assert handler.process_message(sqs.sent[1][1], deps)["status"] == "SUPERSEDED"
    assert "handbook.md" not in _doc_records(deps)  # its own INDEXING record removed
    assert _doc_chunks(deps) == [] and _leases(deps) == []


def test_a_missing_pdf_sidecar_of_a_deleted_source_writes_no_unsupported_record(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    _ingest(handler, deps)
    pdf = next(m for _, m in sqs.sent if m["document"]["name"] == "c-guide.pdf")
    _delete_data_source(deps)
    assert handler.process_message(pdf, deps)["status"] == "SUPERSEDED"
    assert "c-guide.pdf" not in _doc_records(deps)


def test_a_pdf_without_a_sidecar_whose_original_exists_is_still_unsupported(handler):
    deps, os_, embedder, sqs = _fanout_world(handler)
    deps.s3.delete_objects(Bucket=BUCKET, Delete={"Objects": [
        {"Key": KB_PREFIX + "_text/c-guide.pdf.txt"}]})
    _ingest(handler, deps)
    pdf = next(m for _, m in sqs.sent if m["document"]["name"] == "c-guide.pdf")
    assert handler.process_message(pdf, deps)["status"] == "UNSUPPORTED"
    assert _doc_records(deps)["c-guide.pdf"]["status"] == "UNSUPPORTED"


def test_a_data_source_deleted_after_the_count_check_gets_no_indexed_record(handler):
    """The window right before the INDEXED write: the count matched, then the delete ran."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    _drain_until_last(handler, deps, sqs)
    real_request = os_.request

    def request(method, path, body=None, params=None):
        out = real_request(method, path, body, params)
        if path.endswith("/_count"):
            assert out["count"] == 300  # complete: only the source check can stop it now
            _delete_data_source(deps)
        return out

    os_.request = request
    assert handler.process_message(sqs.sent[-1][1], deps)["status"] == "SUPERSEDED"
    os_.request = real_request
    assert os_.indexed_writes == [] and "handbook.md" not in _doc_records(deps)
    assert _doc_chunks(deps) == [] and _leases(deps) == []


# ── N17-obs279 (deploy-ops-36 live on t1): the fan-out finalise writes COMPLETE once ─────────
def test_two_concurrent_finalisers_write_complete_once(handler, caplog):
    """Live: a full rebuild's "COMPLETE after fan-out" was written twice by two document
    invocations that both saw the last result row (counts right, a benign double finalise). Now
    the terminal write is a scripted `_update` that is a `noop` when the job already says
    COMPLETE with these counts: two finalisers that both read IN_PROGRESS → one write, the
    second logs DEBUG, the counts are the ones a single finaliser writes."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    real_request = os_.request
    state = {"raced": False}
    outcomes = []

    def request(method, path, body=None, params=None):
        script = (body or {}).get("script") if isinstance(body, dict) else None
        if script and not state["raced"]:
            state["raced"] = True  # B read IN_PROGRESS too and writes first
            assert handler._maybe_complete_job(deps, "ij1") is True
        out = real_request(method, path, body, params)
        if script:
            outcomes.append(out["result"])
        return out

    os_.request = request
    caplog.set_level(logging.DEBUG, logger="launchpad.memory_extractor")
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    os_.request = real_request
    assert outcomes == ["updated", "noop"]
    assert os_.complete_writes == ["ij1"]
    job = _job_doc(deps)
    assert job["status"] == "COMPLETE" and job["fanout"]["reported"] == 1
    assert _counts({"counts": job["counts"]}) == {
        "numberOfDocumentsScanned": 1, "numberOfNewDocumentsIndexed": 1,
        "numberOfDocumentsFailed": 0, "numberOfDocumentsUnchanged": 0}
    lines = [r for r in caplog.records if "COMPLETE after fan-out" in r.getMessage()]
    assert [r.levelno for r in lines] == [logging.INFO, logging.DEBUG]
    assert "already written by another finaliser" in lines[1].getMessage()


def test_the_complete_write_sends_the_guard_script_with_a_refresh(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    _drain(handler, deps, sqs)
    [write] = [r for r in os_.requests if r["path"] == f"/{core.jobs_index('')}/_update/ij1"
               and "script" in (r["body"] or {})]
    assert write["body"]["script"]["source"] == handler._COMPLETE_ONCE_SCRIPT
    assert write["params"] == {"refresh": "true", "retry_on_conflict": "5"}
    assert write["body"]["script"]["params"]["fields"]["status"] == "COMPLETE"


def test_a_complete_job_with_other_counts_is_still_repaired(handler):
    """The guard is "already COMPLETE with THESE counts": the planner's recompute still
    overwrites totals a document computed from the preliminary half."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    _drain(handler, deps, sqs)
    right = _job_doc(deps)["counts"]
    deps.opensearch.update_doc(core.jobs_index(""), "ij1", {
        "counts": {**right, "numberOfNewDocumentsIndexed": 0}})
    assert handler._maybe_complete_job(deps, "ij1") is True
    assert _job_doc(deps)["counts"] == right
    assert os_.complete_writes == ["ij1", "ij1"]


def test_a_residual_conflict_on_the_complete_write_is_retried_client_side(handler):
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    real_request = os_.request
    state = {"conflicts": 2}

    def request(method, path, body=None, params=None):
        if isinstance(body, dict) and "script" in body and state["conflicts"]:
            state["conflicts"] -= 1
            raise core.OpenSearchError(409, "version_conflict_engine_exception")
        return real_request(method, path, body, params)

    os_.request = request
    assert _drain(handler, deps, sqs)[-1] == "INDEXED"
    os_.request = real_request
    assert _job_doc(deps)["status"] == "COMPLETE" and state["conflicts"] == 0


def test_the_complete_write_never_recreates_a_gone_job(handler, caplog):
    deps, os_, sqs = _sliced_world(handler)
    caplog.set_level(logging.WARNING, logger="launchpad.memory_extractor")
    assert handler._complete_job_once(deps, "gone-job", {"x": 1}, fanout={}) is False
    assert _job_doc(deps, "gone-job") is None
    assert any("status write 'COMPLETE' failed (OpenSearchError)" in r.getMessage()
               for r in caplog.records)


def test_a_no_such_key_the_head_contradicts_is_not_a_gone_document(handler):
    """N17-obs277 guard: only a missing object confirmed by `HeadObject` drops the document.
    A GetObject NoSuchKey for an object that still exists stays the FAILED document it was."""
    deps, os_, sqs = _sliced_world(handler)
    _ingest(handler, deps)
    real_get = deps.s3.get_object

    def get_object(**kw):
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")

    deps.s3.get_object = get_object
    assert handler.process_message(sqs.sent[0][1], deps)["status"] == "FAILED"
    deps.s3.get_object = real_get
    record = _doc_records(deps)["handbook.md"]
    assert record["status"] == "FAILED" and "ClientError" in record["status_reason"]


class _StoppedModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        stop, text = response
        return {"stopReason": stop, "usage": {"outputTokens": 17}, "output": {"message": {
            "content": [{"reasoningContent": {"reasoningText": {"text": "private reasoning"}}},
                        {"text": text}]
        }}}


def test_truncated_model_read_retries_once_before_applying_any_ops(handler, caplog):
    deps = _deps(handler, [])
    # Even valid JSON returned with max_tokens is incomplete and must never be applied.
    model = _StoppedModel([
        ("max_tokens", '["do not store the truncated draft"]'),
        ("end_turn", '["User likes green tea"]'),
        ("max_tokens", ""),
        ("end_turn", '[{"preference":"email first","category":"contact"}]'),
        ("end_turn", "A conversation about tea."),
    ])
    deps = dataclasses.replace(deps, bedrock=model)
    key, _ = _event(deps, "I like tea. Email me first.", "Noted.")
    with caplog.at_level(logging.INFO, logger=handler.logger.name):
        result = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert result["counts"]["facts"] == result["counts"]["preferences"] == 1
    assert [c["inferenceConfig"]["maxTokens"] for c in model.calls] == [
        2048, 8192, 2048, 8192, 2048,
    ]
    docs = deps.opensearch.indices[core.memory_index("")]
    assert len(docs) == 3
    assert "truncated draft" not in json.dumps(docs)
    assert "private reasoning" not in caplog.text and "green tea" not in caplog.text
    assert "stop_reason=max_tokens" in caplog.text and "max_tokens=8192" in caplog.text
    before = json.dumps(docs, sort_keys=True)
    assert handler.process_message(_msg(key, merge=NO_ROLL), deps)["skipped"] == "already completed"
    assert len(model.calls) == 5 and json.dumps(docs, sort_keys=True) == before


def test_persistent_truncation_marks_retryable_failure_without_ops(handler, monkeypatch):
    deps = _deps(handler, [])
    model = _StoppedModel([
        ("end_turn", '["User likes tea"]'),
        ("max_tokens", '[{"preference":"partial","category":"contact"}]'),
        ("max_tokens", '[{"preference":"still partial","category":"contact"}]'),
    ])
    deps = dataclasses.replace(deps, bedrock=model)
    key, _ = _event(deps, "Tea and email", "Noted")
    monkeypatch.setattr(handler, "default_deps", lambda: deps)
    result = handler.lambda_handler({"Records": [
        {"messageId": "one", "body": json.dumps(_msg(key))}
    ]}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": "one"}]}
    assert not deps.opensearch.indices[core.memory_index("")]
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "model_output_truncated"
    assert "8192" in job["failure_reason"] and "partial" not in job["failure_reason"]
    assert len(model.calls) == 3


@pytest.mark.parametrize("stop,text", [("end_turn", "not JSON"), ("guardrail_intervened", "")])
def test_nontruncated_parse_failure_keeps_local_warning_without_retry(handler, stop, text):
    deps = _deps(handler, [])
    model = _StoppedModel([
        ("end_turn", '["User likes tea"]'), (stop, text), ("end_turn", "Tea conversation"),
    ])
    deps = dataclasses.replace(deps, bedrock=model)
    key, _ = _event(deps, "Tea", "Noted")
    out = handler.process_message(_msg(key, merge=NO_ROLL), deps)
    assert out["status"] == "COMPLETED" and out["counts"]["facts"] == 1
    assert out["counts"]["preferences"] == 0 and len(model.calls) == 3
    job = deps.opensearch.get_doc(core.jobs_index(""), out["job_id"])
    assert job["warnings"] == ["user_preference: unparseable model output"]


def test_deadline_refuses_retry_and_records_job_request_correlation(handler, monkeypatch, caplog):
    deps = _deps(handler, [])
    model = _StoppedModel([("max_tokens", "")])
    deps = dataclasses.replace(deps, bedrock=model)
    key, _ = _event(deps, "Tea", "Noted")
    monkeypatch.setattr(handler, "default_deps", lambda: deps)

    class Context:
        aws_request_id = "request-123"

        def get_remaining_time_in_millis(self):
            return 200_000 if not model.calls else 95_000

    with caplog.at_level(logging.INFO, logger=handler.logger.name):
        result = handler.lambda_handler({"Records": [
            {"messageId": "one", "body": json.dumps(_msg(key))}
        ]}, Context())
    assert result == {"batchItemFailures": [{"itemIdentifier": "one"}]}
    assert len(model.calls) == 1 and not deps.opensearch.indices[core.memory_index("")]
    job = deps.opensearch.get_doc(core.jobs_index(""), handler.message_job_id(_msg(key)))
    assert job["status"] == "FAILED" and job["failure_code"] == "extraction_deadline"
    assert "request_id=request-123" in caplog.text and job["job_id"] in caplog.text
    assert handler._remaining_seconds.get() is None  # no warm-container budget leakage


def test_optional_model_budget_failure_keeps_completed_core(handler, monkeypatch):
    deps = _deps(handler, ['["User likes tea"]', "[]", "Tea conversation"])
    key, _ = _event(deps, "Tea", "Noted")

    def exhausted(*args):
        raise handler.ExtractionDeadlineError("insufficient optional budget")

    monkeypatch.setattr(handler, "_consolidate", exhausted)
    monkeypatch.setattr(handler, "_roll_actor_summary", exhausted)
    result = handler.process_message(_msg(key), deps)
    assert result["status"] == "COMPLETED" and result["counts"]["facts"] == 1
    job = deps.opensearch.get_doc(core.jobs_index(""), result["job_id"])
    assert len(job["warnings"]) == 3 and all("skipped" in w for w in job["warnings"])


def test_read_timeout_is_not_retried_by_output_budget_loop(handler):
    model = _StoppedModel([_read_timeout()])
    with pytest.raises(type(_read_timeout())):
        handler._converse_text(model, "model", "system", "user")
    assert len(model.calls) == 1


def test_openai_output_length_uses_same_bounded_retry(handler):
    calls = []

    def post(url, headers, body):
        calls.append(json.loads(body)["max_tokens"])
        return json.dumps({"choices": [{
            "finish_reason": "length" if len(calls) == 1 else "stop",
            "message": {"content": "" if len(calls) == 1 else "[]"},
        }]}).encode()

    chat = handler.OpenAIChat(base_url="https://example.invalid", api_key="test",
                              api_model_name="model", post=post)
    assert chat.converse_text("system", "user") == "[]" and calls == [2048, 8192]


def test_default_model_transport_has_no_hidden_retries(handler, monkeypatch):
    calls = []

    class Session:
        def client(self, name, **kwargs):
            calls.append((name, kwargs))
            return object()

        def get_credentials(self):
            return object()

    monkeypatch.setattr(handler.boto3, "Session", Session)
    monkeypatch.setattr(handler, "resolve_bearer", lambda *args: "test-only")
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("LAUNCHPAD_MEMORY_OS_ENDPOINT", "example.invalid")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "restored-by-monkeypatch")
    deps = handler.default_deps()
    deps.model_client_factory(75)
    configs = [kw["config"] for name, kw in calls if name == "bedrock-runtime"]
    assert [c.read_timeout for c in configs] == [60, 75]  # embedding remains unchanged
    assert all(c.connect_timeout == 5 for c in configs)
    assert all(c.retries == {"total_max_attempts": 1} for c in configs)


def test_configured_model_read_window_accepts_a_75_second_provider_response(handler, monkeypatch):
    """The real model factory must use its configurable budget, not the SDK's old 60s default."""
    calls = []

    class Provider:
        def __init__(self, config):
            self.config = config

        def converse(self, **kwargs):
            calls.append(self.config)
            if self.config.read_timeout < 75:
                raise _read_timeout()
            return {"stopReason": "end_turn", "output": {"message": {"content": [
                {"text": "[]"},
            ]}}}

    class Session:
        def client(self, name, **kwargs):
            return Provider(kwargs["config"]) if name == "bedrock-runtime" else object()

        def get_credentials(self):
            return object()

    monkeypatch.setattr(handler.boto3, "Session", Session)
    monkeypatch.setattr(handler, "resolve_bearer", lambda *args: "test-only")
    monkeypatch.setenv("AWS_REGION", "cn-northwest-1")
    monkeypatch.setenv("LAUNCHPAD_MEMORY_OS_ENDPOINT", "example.invalid")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "restored-by-monkeypatch")
    monkeypatch.setenv("LAUNCHPAD_EXTRACT_MODEL_READ_TIMEOUT_SECONDS", "120")
    deps = handler.default_deps()
    assert handler.chat_model_for({}, deps).converse_text("system", "user") == "[]"
    assert len(calls) == 1 and calls[0].read_timeout == 120
    assert calls[0].retries == {"total_max_attempts": 1}


def test_transport_timeout_still_logs_safe_request_budget(handler, monkeypatch, caplog):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, "120")

    class SlowModel:
        def converse(self, **kwargs):
            raise _read_timeout()

    with caplog.at_level(logging.INFO, logger=handler.logger.name):
        with pytest.raises(Exception) as error:
            handler._converse_text(SlowModel(), "model", "private-system", "private-user")
    assert type(error.value).__name__ == "ReadTimeoutError"
    lines = [record.getMessage() for record in caplog.records]
    assert any("model_request model=model attempt=1 max_tokens=2048 "
               "read_timeout_seconds=120.0" in line for line in lines)
    assert all("private-" not in line for line in lines)
    assert not any("model_response" in line for line in lines)


def test_bedrock_retry_clips_actual_client_timeout_to_remaining_budget(handler, monkeypatch):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, "120")
    state = {"remaining": 200.0}
    windows = []
    model = _StoppedModel([("max_tokens", ""), ("end_turn", "[]")])

    def client_factory(timeout):
        windows.append(timeout)
        state["remaining"] = 140.0
        return model

    token = handler._remaining_seconds.set(lambda: state["remaining"])
    try:
        assert handler._converse_text(
            model, "model", "system", "user", client_factory=client_factory,
        ) == "[]"
    finally:
        handler._remaining_seconds.reset(token)
    assert windows == [105.0, 45.0]
    assert [c["inferenceConfig"]["maxTokens"] for c in model.calls] == [2048, 8192]


@pytest.mark.parametrize("remaining", [0, 90, 95, 95.99])
def test_no_model_request_when_even_one_second_read_cannot_fit(handler, monkeypatch, remaining):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, "120")
    model = _StoppedModel([])
    token = handler._remaining_seconds.set(lambda: remaining)
    try:
        with pytest.raises(handler.ExtractionDeadlineError):
            handler._converse_text(model, "model", "system", "user")
    finally:
        handler._remaining_seconds.reset(token)
    assert model.calls == []


@pytest.mark.parametrize("configured", ["1", "90", "120"])
def test_no_context_still_has_a_bounded_configured_read(handler, monkeypatch, configured):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, configured)
    assert handler._remaining_seconds.get() is None
    assert handler._model_read_timeout() == float(configured)


@pytest.mark.parametrize("configured", ["0", "-1", "121", "nan", "inf", "bad"])
def test_invalid_model_read_configuration_is_rejected(handler, monkeypatch, configured):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, configured)
    with pytest.raises(ValueError):
        handler._model_read_timeout()


def test_compatible_model_transport_clips_read_and_rechecks_after_reauth(handler, monkeypatch):
    monkeypatch.setenv(handler.MODEL_READ_TIMEOUT_ENV, "120")
    state = {"remaining": 150.0}
    windows = []
    closed = []

    class Pool:
        def request(self, method, url, **kwargs):
            timeout = kwargs["timeout"]
            assert kwargs["retries"] is False and timeout.connect_timeout == 5
            windows.append(timeout.read_timeout)
            status = 401 if len(windows) == 1 else 200
            state["remaining"] = 110.0
            body = json.dumps({"choices": [{
                "finish_reason": "stop", "message": {"content": "[]"},
            }]}).encode()
            return type("Response", (), {"status": status, "headers": {}, "data": body})()

        def clear(self):
            closed.append(True)

    monkeypatch.setattr(handler.urllib3, "PoolManager", Pool)
    token = handler._remaining_seconds.set(lambda: state["remaining"])
    try:
        model = handler.OpenAIChat(
            base_url="https://example.invalid", api_key="test", api_model_name="model",
            refresh_key=lambda: "renewed-test-key",
        )
        assert model.converse_text("system", "user") == "[]"
    finally:
        handler._remaining_seconds.reset(token)
    assert windows == [55.0, 15.0] and len(closed) == 2
