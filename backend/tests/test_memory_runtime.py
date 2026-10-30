"""The runtime memory library rendered into generated agents (N6 R6): S3SessionManager
snapshots, AfterInvocation → S3 event + SQS, k-NN recall — all faked, no AWS.

Also pins the module contract frozen in docs/cn/interfaces/memory-runtime.md (every
exported name and its parameter names), since N7's harness shim grafts the same source
and calls ``make_session_manager(actor_id=…, session_id=…)`` by keyword."""

import importlib.util
import inspect
import json
import os
import types

import pytest
import strands
from strands.hooks import AfterInvocationEvent, HookProvider
from strands.session import S3SessionManager

from app.schemas.agent import AgentSpec, KnowledgeBaseRef, MemoryConfig
from app.services import opensearch_http as core
from app.templates.strands_agent import render_main_py
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_strands_template import _FakeTool, _wire_identity  # reuse the N2 identity wiring

MEM_ENV = {
    "LAUNCHPAD_MEMORY_ID": "launchpad_memory-default",
    "LAUNCHPAD_MEMORY_BUCKET": "bkt",
    "LAUNCHPAD_MEMORY_PREFIX": "memory",
    "LAUNCHPAD_MEMORY_OS_ENDPOINT": "search-mem.example",
    "LAUNCHPAD_MEMORY_QUEUE_URL": "https://sqs.example/q",
    "LAUNCHPAD_MEMORY_LONG_TERM": "1",
    "AWS_REGION": "cn-northwest-1",
}
SPEC = AgentSpec(
    name="mem-agent",
    method="zip_runtime",
    system_prompt="Be brief.",
    memory=MemoryConfig(short_term=True, long_term=True),
)
STORE_PREFIX = "memory/launchpad_memory-default/agent1__river"


def _load(spec, tmp_path, monkeypatch, stem):
    monkeypatch.setattr(strands, "Agent", lambda **kw: types.SimpleNamespace(**kw))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    target = tmp_path / f"{stem}.py"
    target.write_text(render_main_py(spec), encoding="utf-8")
    ms = importlib.util.spec_from_file_location(stem, target)
    m = importlib.util.module_from_spec(ms)
    ms.loader.exec_module(m)
    _wire_identity(m, monkeypatch)
    fakes = {"s3": FakeS3(), "sqs": FakeSQS(), "os": FakeOpenSearch(), "emb": FakeEmbedder()}
    fakes["os"].ensure_index(core.memory_index(""), core.memory_index_body())
    # the real factories, for the tests that exercise the client wiring itself
    fakes["real"] = {
        name: getattr(m, name)
        for name in ("_memory_s3", "_memory_sqs", "_memory_opensearch", "_memory_embedder",
                     "_kb_opensearch", "_kb_embedder")
        if hasattr(m, name)
    }
    monkeypatch.setattr(m, "_memory_s3", lambda: fakes["s3"])
    monkeypatch.setattr(m, "_memory_sqs", lambda: fakes["sqs"])
    monkeypatch.setattr(m, "_memory_opensearch", lambda: fakes["os"])
    monkeypatch.setattr(m, "_memory_embedder", lambda: fakes["emb"])
    m._fakes = fakes
    return m


@pytest.fixture
def mod(tmp_path, monkeypatch):
    for k, v in MEM_ENV.items():
        monkeypatch.setenv(k, v)
    return _load(SPEC, tmp_path, monkeypatch, "mem_main")


def test_rendered_source_carries_the_library_and_no_agentcore_memory():
    code = render_main_py(SPEC)
    assert "LAUNCHPAD_MEMORY_RUNTIME = 'v1'" in code
    assert "class SnapshotSessionManager(S3SessionManager)" in code
    assert "from strands.session import S3SessionManager" in code
    assert "AfterInvocationEvent" in code
    assert "AgentCoreMemorySessionManager" not in code
    assert "bedrock_agentcore.memory" not in code
    assert "__LAUNCHPAD_" not in code
    assert "# <launchpad-opensearch-core>" in code and "# </launchpad-opensearch-core>" in code
    # the library sits after the model block it depends on (ruling R1 / memory-runtime.md §6)
    assert code.index("def build_model(") < code.index("LAUNCHPAD_MEMORY_RUNTIME = 'v1'")
    assert code.index("LAUNCHPAD_MEMORY_RUNTIME = 'v1'") < code.index("def build_agent(")


def test_module_contract_matches_the_frozen_interface(mod):
    """docs/cn/interfaces/memory-runtime.md §2 — names and parameter names N7 relies on."""

    def params(fn):
        return list(inspect.signature(fn).parameters)

    assert issubclass(mod.SnapshotSessionManager, S3SessionManager)
    assert issubclass(mod.LaunchpadMemoryHook, HookProvider)
    assert params(mod.SnapshotSessionManager.__init__) == ["self", "actor_id", "session_id"]
    assert params(mod.make_session_manager) == ["actor_id", "session_id"]
    assert mod.memory_session_manager is mod.make_session_manager
    assert params(mod.OpenSearchMemoryStore.__init__) == ["self", "actor_id"]
    assert params(mod.OpenSearchMemoryStore.search) == ["self", "query", "namespaces", "top_k"]
    assert params(mod.make_memory_store) == ["actor_id"]
    assert params(mod.recall_context) == ["actor_id", "query", "top_k"]
    assert params(mod.LaunchpadMemoryHook.__init__) == ["self", "actor_id", "session_id"]
    assert params(mod.make_memory_hooks) == ["actor_id", "session_id"]
    for name in ("_memory_env", "_memory_s3", "_memory_sqs", "_memory_opensearch",
                 "_memory_embedder"):
        assert callable(getattr(mod, name)), name


def test_session_manager_uses_the_launchpad_prefix(mod, monkeypatch):
    built = {}

    class FakeS3SM:
        def __init__(self, session_id, bucket, prefix="", **kw):
            built.update(session_id=session_id, bucket=bucket, prefix=prefix, **kw)

    monkeypatch.setattr(mod, "S3SessionManager", FakeS3SM)
    monkeypatch.setattr(
        mod, "SnapshotSessionManager",
        type("Snap", (FakeS3SM,), {"__init__": mod.SnapshotSessionManager.__init__}),
    )
    sm = mod.make_session_manager("agent1__river", "sess-1")
    assert sm is not None
    assert built["bucket"] == "bkt" and built["prefix"] == STORE_PREFIX
    assert built["session_id"] == "sess-1" and built["region_name"] == "cn-northwest-1"
    assert mod.memory_session_manager is mod.make_session_manager


def test_session_manager_construction_failure_is_fail_soft(mod, monkeypatch, capsys):
    def boom(self, *a, **kw):
        raise RuntimeError("bucket unreachable")

    monkeypatch.setattr(mod.SnapshotSessionManager, "__init__", boom)
    assert mod.make_session_manager("a", "s") is None
    assert "memory session manager unavailable" in capsys.readouterr().err


class _LoggingS3(FakeS3):
    """FakeS3 that records every API call so a test can assert which S3 operations ran."""

    def __init__(self):
        super().__init__()
        self.calls: list[str] = []

    def __getattribute__(self, name):
        attr = object.__getattribute__(self, name)
        if name in ("get_object", "head_object", "list_objects_v2", "put_object"):
            def logged(**kw):
                object.__getattribute__(self, "calls").append(name)
                return attr(**kw)
            return logged
        return attr


def _snapshot_manager_over(mod, monkeypatch, s3, actor="agent1__river", session="sess-1"):
    """Build a real SnapshotSessionManager whose boto3 S3 client is the given fake."""
    import strands.session.s3_session_manager as s3sm

    fake_boto3 = types.SimpleNamespace(
        Session=lambda **kw: types.SimpleNamespace(client=lambda **kw2: s3),
    )
    monkeypatch.setattr(s3sm, "boto3", fake_boto3)
    return mod.SnapshotSessionManager(actor, session)


def test_new_session_first_read_raises_no_404_error_span(mod, monkeypatch):
    """N17-obs528: opening a session that does not exist yet must not GetObject /
    HeadObject a missing key (botocore instrumentation turns the ClientError into an
    error span and the trace reads 「失败 / 2 个错误」). Probe by ListObjectsV2, then write."""
    s3 = _LoggingS3()
    sm = _snapshot_manager_over(mod, monkeypatch, s3)
    assert sm._is_new_session is True
    assert "get_object" not in s3.calls and "head_object" not in s3.calls
    assert s3.calls[0] == "list_objects_v2" and "put_object" in s3.calls
    key = f"{STORE_PREFIX}/session_sess-1/session.json"
    assert ("bkt", key) in s3.objects
    assert json.loads(s3.objects[("bkt", key)]["Body"])["session_id"] == "sess-1"


def test_existing_session_is_still_read(mod, monkeypatch):
    s3 = _LoggingS3()
    _snapshot_manager_over(mod, monkeypatch, s3)  # first turn creates the session
    s3.calls.clear()
    sm = _snapshot_manager_over(mod, monkeypatch, s3)  # second turn, same session
    assert sm._is_new_session is False
    assert sm.session.session_id == "sess-1"
    assert s3.calls == ["list_objects_v2", "get_object"]  # read, no create, no HeadObject


def test_session_probe_surfaces_real_s3_errors(mod, monkeypatch):
    from botocore.exceptions import ClientError
    from strands.types.exceptions import SessionException

    class Forbidden(FakeS3):
        def list_objects_v2(self, **kw):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "ListObjectsV2")

    with pytest.raises(SessionException, match="AccessDenied"):
        _snapshot_manager_over(mod, monkeypatch, Forbidden())


def test_without_a_store_everything_is_a_noop(mod, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "")
    assert mod.make_session_manager("a", "s") is None
    assert mod.make_memory_hooks("a", "s") == []
    assert mod.recall_context("a", "q") == ""
    assert mod.make_memory_store("a") is None
    agent = mod.build_agent("a", "s")
    assert not hasattr(agent, "session_manager") and not hasattr(agent, "hooks")


def test_short_term_only_persists_but_never_recalls(mod, monkeypatch):
    """LONG_TERM=0: the snapshot + hook path stays on, k-NN recall is off."""
    monkeypatch.setenv("LAUNCHPAD_MEMORY_LONG_TERM", "0")
    monkeypatch.setattr(mod, "SnapshotSessionManager", lambda a, s: f"snap:{a}:{s}")
    assert mod.make_session_manager("a", "s") == "snap:a:s"
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    assert isinstance(hook, mod.LaunchpadMemoryHook)
    assert mod.make_memory_store("agent1__river") is None
    assert mod.recall_context("agent1__river", "tea?") == ""
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[{"role": "user", "content": [{"text": "hi"}]}])
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result="hello"))
    ((_, msg),) = mod._fakes["sqs"].sent
    assert msg["long_term"] is False  # the extractor skips long-term strategies for it


def test_after_invocation_writes_one_event_and_one_sqs_message(mod):
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "I like tea"}]},
        {"role": "assistant", "content": [{"text": "Noted."}]},
    ])
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result="Noted."))
    keys = [k for (_, k) in mod._fakes["s3"].objects]
    assert len(keys) == 1
    assert keys[0].startswith(f"{STORE_PREFIX}/session_sess-1/events/")
    assert keys[0].endswith(".json")
    assert core.parse_event_key(keys[0].rsplit("/", 1)[1]) is not None  # sortable name
    body = json.loads(mod._fakes["s3"].get_object(Bucket="bkt", Key=keys[0])["Body"].read())
    assert [p["conversational"]["role"] for p in body["payload"]] == ["USER", "ASSISTANT"]
    assert body["payload"][0]["conversational"]["content"]["text"] == "I like tea"
    assert body["payload"][1]["conversational"]["content"]["text"] == "Noted."
    assert body["actorId"] == "agent1__river" and body["sessionId"] == "sess-1"
    ((url, msg),) = mod._fakes["sqs"].sent
    assert url == "https://sqs.example/q"
    assert msg == {
        "kind": "memory_extract", "store_id": "launchpad_memory-default",
        "actor_id": "agent1__river", "session_id": "sess-1", "event_key": keys[0],
        "long_term": True,
    }


EXTRACT_MODEL_BLOCK = {
    "model_id": "global.moonshotai.kimi-k3", "kind": "bedrock", "base_url": None,
    "api_model_name": "global.moonshotai.kimi-k3",
    "credential_provider": "launchpad-global-bedrock", "provider": "bedrock",
    "model_region": "us-west-2", "catalog_version": 3, "temperature": False,
}


def _fire_turn(mod):
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "I like tea"}]},
        {"role": "assistant", "content": [{"text": "Noted."}]},
    ])
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result="Noted."))
    return mod._fakes["sqs"].sent[-1][1]


def test_after_invocation_sends_the_deployed_extract_model_block(mod, monkeypatch):
    """F-LOAD-02 (N17-obs533, ten-student simulation 2026-10-30): the Runtime's own
    `memory_extract` message carried no `extract_model`, so the extractor Lambda ran its env
    fallback model for every agent-side turn whatever the `memory_extract` role said. The
    deployer now writes the role's row as compact JSON into LAUNCHPAD_MEMORY_EXTRACT_MODEL and
    the hook forwards it parsed — the same block the hub's own messages carry (incl. the
    F-LOAD-03 `temperature` capability). The value is baked in at deploy, so a role change
    reaches an agent on its next redeploy."""
    monkeypatch.setenv("LAUNCHPAD_MEMORY_EXTRACT_MODEL",
                       json.dumps(EXTRACT_MODEL_BLOCK, separators=(",", ":")))
    assert mod._memory_env()["extract_model"] == EXTRACT_MODEL_BLOCK
    msg = _fire_turn(mod)
    assert msg["extract_model"] == EXTRACT_MODEL_BLOCK
    assert msg["extract_model"]["temperature"] is False
    assert {k for k in msg if k != "extract_model"} == {
        "kind", "store_id", "actor_id", "session_id", "event_key", "long_term"}


@pytest.mark.parametrize("value", ["", "not json", "[]", '"glm"', '{"kind": "bedrock"}'])
def test_after_invocation_omits_extract_model_without_a_usable_env_value(mod, monkeypatch, value):
    """Unset, unparseable or not an object with a `model_id` → the message omits the block
    exactly as before the fix, and the Lambda keeps its LAUNCHPAD_EXTRACT_MODEL_ID fallback."""
    if value:
        monkeypatch.setenv("LAUNCHPAD_MEMORY_EXTRACT_MODEL", value)
    else:
        monkeypatch.delenv("LAUNCHPAD_MEMORY_EXTRACT_MODEL", raising=False)
    assert mod._memory_env()["extract_model"] is None
    assert "extract_model" not in _fire_turn(mod)


def test_after_invocation_reads_the_last_turn_only(mod):
    """Multi-turn message history (and tool-only blocks) → just the latest USER/ASSISTANT
    texts; the answer falls back to the event result when the history has none."""
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "old question"}]},
        {"role": "assistant", "content": [{"text": "old answer"}]},
        {"role": "user", "content": [{"text": "what time is it"}]},
        {"role": "assistant", "content": [{"toolUse": {"name": "current_utc_time"}}]},
        {"role": "user", "content": [{"toolResult": {"content": [{"text": "12:00"}]}}]},
    ])
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result="It is noon."))
    ((_, key),) = mod._fakes["s3"].objects
    body = json.loads(mod._fakes["s3"].get_object(Bucket="bkt", Key=key)["Body"].read())
    texts = [p["conversational"]["content"]["text"] for p in body["payload"]]
    assert texts == ["what time is it", "It is noon."]
    # nothing to say → nothing written
    mod._fakes["s3"].objects.clear()
    mod._fakes["sqs"].sent.clear()
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=types.SimpleNamespace(messages=[]),
                                                    result=""))
    assert not mod._fakes["s3"].objects and not mod._fakes["sqs"].sent


def test_failed_turn_writes_no_event(mod):
    """N17-obs304: Strands fires AfterInvocationEvent in a `finally` — a turn whose model call
    raised (result None, history ending in the user's message) has a question and no answer.
    Such a turn writes no event and enqueues no extraction (the extractor would otherwise
    summarize an unanswered question and invent the answer)."""
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[
        {"role": "user", "content": [{"text": "old question"}]},
        {"role": "assistant", "content": [{"text": "old answer"}]},
        {"role": "user", "content": [{"text": "how much is the Shanghai lodging cap?"}]},
    ])
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result=None))
    # a whitespace-only result is no answer either
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result="  \n"))
    # a tool call that never got its final answer
    agent.messages.append({"role": "assistant", "content": [{"toolUse": {"name": "kb_search"}}]})
    cbs[AfterInvocationEvent](types.SimpleNamespace(agent=agent, result=None))
    assert not mod._fakes["s3"].objects and not mod._fakes["sqs"].sent


def test_hook_failures_never_abort_the_turn(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("s3 down")

    monkeypatch.setattr(mod, "_memory_s3", boom)
    (hook,) = mod.make_memory_hooks("a__b", "s")
    cbs = {}
    hook.register_hooks(types.SimpleNamespace(add_callback=lambda t, cb: cbs.__setitem__(t, cb)))
    agent = types.SimpleNamespace(messages=[])
    next(iter(cbs.values()))(types.SimpleNamespace(agent=agent, result="x"))  # no raise
    assert "memory hook" in capsys.readouterr().err


def test_recall_reads_preferences_facts_and_session_summaries_only_when_long_term_is_on(
    mod, monkeypatch
):
    emb = mod._fakes["emb"]
    rows = (
        ("f", "/facts/agent1__river", "User likes tea", "semantic"),
        ("p", "/preferences/agent1__river", "Short answers", "user_preference"),
        ("o", "/facts/other__x", "User likes tea too", "semantic"),
        ("s", "/summaries/agent1__river/s1", "Tea talk", "summarization"),
        ("x", "/facts/agent1__river", "Lives elsewhere", "semantic"),
    )
    for rid, ns, text, strategy in rows:
        store = "launchpad_memory-default" if rid != "x" else "another-store"
        mod._fakes["os"].put_doc(core.memory_index(""), rid, {
            "store_id": store, "namespace": ns, "strategy": strategy, "text": text,
            "embedding": emb.embed(text), "created_at": "2026-01-01T00:00:00+00:00",
        })
    out = mod.recall_context("agent1__river", "tea?")
    assert "User likes tea" in out and "Short answers" in out
    assert "Summary of an earlier conversation: Tea talk" in out
    assert "too" not in out and "elsewhere" not in out
    rows = mod.make_memory_store("agent1__river").search("tea?", top_k=3)
    assert {r["namespace"] for r in rows} == {"/preferences/agent1__river", "/facts/agent1__river"}
    assert all({"text", "namespace", "score", "created_at"} <= set(r) for r in rows)
    monkeypatch.setenv("LAUNCHPAD_MEMORY_LONG_TERM", "0")
    assert mod.recall_context("agent1__river", "tea?") == ""
    assert mod.make_memory_store("agent1__river") is None


def _summary_doc(mod, rid, namespace, text, *, actor="agent1__river", store=None, **extra):
    """A summary record as the extractor writes it (`text` = JSON with a `summary` key,
    strategy `summarization`, embedded) under the default store unless `store` says
    otherwise."""
    emb = mod._fakes["emb"]
    raw = json.dumps({"summary": text}) if not extra.pop("plain", False) else text
    mod._fakes["os"].put_doc(core.memory_index(""), rid, {
        "store_id": store or "launchpad_memory-default", "actor_id": actor,
        "namespace": namespace, "strategy": "summarization", "text": raw,
        "embedding": emb.embed(text), "created_at": "2026-01-01T00:00:00+00:00", **extra,
    })


def test_recall_surfaces_a_fresh_session_summary_before_the_actor_summary_rolls(mod):
    """The freshness gap (2026-10-29): conversation A's summary is written at once, the
    rolling `_actor` document only every `actor_summary_interval` extractions. Conversation B
    must still see A — through the per-session summaries, with no `_actor` document at all."""
    actor = "agent1__river"
    _summary_doc(mod, "s-a", f"/summaries/{actor}/conv-a", "User planned a trip to Chengdu.")
    _summary_doc(mod, "s-plain", f"/summaries/{actor}/conv-0", "Asked about visa rules.",
                 plain=True)  # an older plain-text summary record decodes too
    _summary_doc(mod, "s-gone", f"/summaries/{actor}/conv-x", "User cancelled everything.",
                 superseded_by="s-a")  # inactive: never recalled
    _summary_doc(mod, "s-other", "/summaries/other__x/conv-a", "Other actor's trip.",
                 actor="other__x")
    _summary_doc(mod, "s-store", f"/summaries/{actor}/conv-s", "Another store's trip.",
                 store="another-store")
    emb = mod._fakes["emb"]
    mod._fakes["os"].put_doc(core.memory_index(""), "fact-shaped", {  # wrong strategy
        "store_id": "launchpad_memory-default", "namespace": f"/summaries/{actor}/conv-f",
        "strategy": "semantic", "text": "Trip fact.", "embedding": emb.embed("Trip fact."),
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    assert mod._fakes["os"].get_doc(
        core.memory_index(""),
        core.summary_record_id("launchpad_memory-default", core.actor_summary_namespace(actor)),
    ) is None

    out = mod.recall_context(actor, "where is the user travelling?")
    lines = out.splitlines()
    assert "Summary of an earlier conversation: User planned a trip to Chengdu." in lines
    assert "Summary of an earlier conversation: Asked about visa rules." in lines
    assert all(line.startswith("Summary of an earlier conversation: ") for line in lines)
    assert "Summary of earlier conversations:" not in out  # no `_actor` document yet
    for absent in ("cancelled", "Other actor", "Another store", "Trip fact"):
        assert absent not in out

    rows = mod.make_memory_store(actor).session_summaries("where is the user travelling?")
    assert {r["namespace"] for r in rows} == {f"/summaries/{actor}/conv-a",
                                               f"/summaries/{actor}/conv-0"}
    assert all({"text", "namespace", "score", "similarity", "created_at"} <= set(r) for r in rows)

    # once the extractor rolls the actor summary it is the last line and is NOT repeated as a
    # session summary (its namespace is excluded by the prefix query)
    _summary_doc(mod, core.summary_record_id("launchpad_memory-default",
                                             core.actor_summary_namespace(actor)),
                 core.actor_summary_namespace(actor), "Frequent traveller.")
    out = mod.recall_context(actor, "where is the user travelling?")
    assert out.splitlines()[-1] == "Summary of earlier conversations: Frequent traveller."
    assert out.count("Frequent traveller.") == 1
    rows = mod.make_memory_store(actor).session_summaries("where is the user travelling?")
    assert core.actor_summary_namespace(actor) not in {r["namespace"] for r in rows}


def test_recall_bounds_the_session_summary_text(mod):
    actor = "agent1__river"
    budget = mod.RECALL_SESSION_SUMMARY_MAX_CHARS
    long_text = "trip " * (budget // 5 + 50)  # one summary longer than the whole budget
    _summary_doc(mod, "s-long", f"/summaries/{actor}/conv-long", long_text.strip())
    _summary_doc(mod, "s-next", f"/summaries/{actor}/conv-next", "trip to Chengdu")
    out = mod.recall_context(actor, "trip")
    lines = out.splitlines()
    assert len(lines) == 1 and lines[0].endswith("…")
    assert len(lines[0]) - len("Summary of an earlier conversation: ") == budget
    # within budget every summary is appended whole and the top_k cap applies
    mod._fakes["os"].delete_doc(core.memory_index(""), "s-long")
    for i in range(4):
        _summary_doc(mod, f"s-{i}", f"/summaries/{actor}/conv-{i}", f"trip number {i}")
    lines = mod.recall_context(actor, "trip").splitlines()
    assert len(lines) == 3 and not any(line.endswith("…") for line in lines)


def test_recall_failure_returns_empty_text(mod, monkeypatch, capsys):
    def boom():
        raise RuntimeError("opensearch down")

    monkeypatch.setattr(mod, "_memory_opensearch", boom)
    assert mod.recall_context("agent1__river", "tea?") == ""
    assert "memory recall skipped" in capsys.readouterr().err


def test_build_agent_wires_session_manager_and_hooks(mod, monkeypatch):
    monkeypatch.setattr(mod, "make_session_manager", lambda a, s: "SM")
    agent = mod.build_agent("agent1__river", "sess-1")
    assert agent.session_manager == "SM"
    assert len(agent.hooks) == 1 and isinstance(agent.hooks[0], mod.LaunchpadMemoryHook)
    assert agent.hooks[0].actor_id == "agent1__river" and agent.hooks[0].session_id == "sess-1"


def test_titan_embedder_uses_the_model_bearer_key_path(mod, monkeypatch):
    """Ruling R1 / model-credential.md: the ONLY credential the embedder uses is the
    Global Bedrock API key already resolved for the agent's model — exported as the
    bearer right before the bedrock-runtime client is created, in MODEL_REGION."""
    identity, _ = _wire_identity(mod, monkeypatch)
    created = []

    def fake_client(service, **kw):
        created.append((service, kw))
        return types.SimpleNamespace(service=service)

    monkeypatch.setattr(mod.boto3, "client", fake_client)
    mod._MEMORY_CLIENTS.pop("emb", None)
    emb = mod._fakes["real"]["_memory_embedder"]()
    assert isinstance(emb, mod.TitanEmbedder) and emb.model_id == core.EMBED_MODEL_ID
    ((service, kwargs),) = created
    assert service == "bedrock-runtime" and kwargs["region_name"] == mod.MODEL_REGION
    assert kwargs["config"] is mod._MEMORY_BOTO_CONFIG  # bounded timeouts (fix round 1)
    assert os.environ["AWS_BEARER_TOKEN_BEDROCK"] == "bedrock-api-key"
    assert identity.calls == [{
        "workloadIdentityToken": "wl-token",
        "resourceCredentialProviderName": "launchpad-global-bedrock",
    }]
    assert mod._fakes["real"]["_memory_embedder"]() is emb  # cached per process
    assert len(identity.calls) == 1  # and no second exchange


def test_boto_clients_carry_bounded_timeouts_and_retries(mod, monkeypatch):
    """Fix round 1: a hook/recall that cannot reach AWS must give the turn back in
    seconds — every boto3 client the library builds carries the bounded Config."""
    _wire_identity(mod, monkeypatch)
    created = {}

    def fake_client(service, **kw):
        created[service] = kw
        return types.SimpleNamespace(service=service)

    monkeypatch.setattr(mod.boto3, "client", fake_client)
    for key in ("s3", "sqs", "emb"):
        mod._MEMORY_CLIENTS.pop(key, None)
    mod._fakes["real"]["_memory_s3"]()
    mod._fakes["real"]["_memory_sqs"]()
    mod._fakes["real"]["_memory_embedder"]()
    assert set(created) == {"s3", "sqs", "bedrock-runtime"}
    for service, kw in created.items():
        cfg = kw["config"]
        assert (cfg.connect_timeout, cfg.read_timeout) == (3, 10), service
        assert cfg.retries == {"max_attempts": 2, "mode": "standard"}, service
    assert created["s3"]["region_name"] == "cn-northwest-1"
    assert created["bedrock-runtime"]["region_name"] == mod.MODEL_REGION


@pytest.mark.parametrize("actor_id,session_id", [
    ("agent1/river", "sess-1"), ("agent1__river", "s/1"), ("agent1__river", ".."),
    ("..", "sess-1"), ("", "sess-1"), ("agent1__river", ""),
])
def test_unsafe_ids_skip_memory_for_the_turn(mod, monkeypatch, capsys, actor_id, session_id):
    """Fix round 1: a path separator or '..' in either id would make Strands raise and the
    hook write a key with the separator — skip memory for the turn instead, fail-soft."""
    def never(*a, **kw):
        raise AssertionError("no session manager may be constructed for an unsafe id")

    monkeypatch.setattr(mod, "SnapshotSessionManager", never)
    assert mod.make_session_manager(actor_id, session_id) is None
    assert mod.make_memory_hooks(actor_id, session_id) == []
    assert "memory skipped: unsafe actor/session id" in capsys.readouterr().err


def test_safe_ids_keep_memory_on(mod, monkeypatch):
    monkeypatch.setattr(mod, "SnapshotSessionManager", lambda a, s: f"snap:{a}:{s}")
    assert mod.make_session_manager("agent1__river", "sess-1") == "snap:agent1__river:sess-1"
    assert mod.make_session_manager("a.b-c_d", "0123abcd") == "snap:a.b-c_d:0123abcd"
    (hook,) = mod.make_memory_hooks("agent1__river", "sess-1")
    assert isinstance(hook, mod.LaunchpadMemoryHook)


def test_opensearch_clients_take_endpoint_and_region_from_the_environment(mod, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_KB_OS_ENDPOINT", "https://search-kb.example/")
    mod._MEMORY_CLIENTS.pop("os", None)
    mod._MEMORY_CLIENTS.pop("kb_os", None)
    mem = mod._fakes["real"]["_memory_opensearch"]()
    assert isinstance(mem, mod.OpenSearchHttp)
    assert mem.endpoint == "search-mem.example" and mem.region == "cn-northwest-1"
    assert mem.service == "es"
    kb = mod._fakes["real"]["_kb_opensearch"]()
    assert isinstance(kb, mod.OpenSearchHttp)
    assert kb.endpoint == "search-kb.example" and kb.region == "cn-northwest-1"
    # the KB tools embed with the memory library's Titan client (one credential path)
    assert mod._fakes["real"]["_kb_embedder"]() is mod._fakes["emb"]


def test_kb_tools_search_opensearch_with_titan(tmp_path, monkeypatch):
    spec = SPEC.model_copy(update={
        "knowledge_bases": [KnowledgeBaseRef(kb_id="KB111", name="Handbook", description="HR")],
    })
    for k, v in MEM_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("LAUNCHPAD_KB_OS_ENDPOINT", "search-mem.example")
    code = render_main_py(spec)
    assert "bedrock-agent-runtime" not in code and "agentic_retrieve_stream" not in code
    m = _load(spec, tmp_path, monkeypatch, "kb_main")
    monkeypatch.setattr(m, "_kb_opensearch", lambda: m._fakes["os"])
    monkeypatch.setattr(m, "_kb_embedder", lambda: m._fakes["emb"])
    idx = core.kb_index_name("KB111", "")
    m._fakes["os"].ensure_index(idx, core.kb_index_body())
    texts = ["Vacation policy: 20 days", "Expenses due Friday", "Vacation carry-over rules"]
    for i, text in enumerate(texts):
        m._fakes["os"].put_doc(idx, f"c{i}", {
            "kb_id": "KB111", "doc_uri": f"s3://bkt/kb/KB111/DS/{i}.md", "doc_name": f"{i}.md",
            "chunk_index": 0, "text": text, "embedding": m._fakes["emb"].embed(text),
            "metadata": {},
        })
    out = m.kb_search("vacation days")
    assert out.startswith("[Handbook]") and "passage(s):" in out
    assert "score=" in out and "source=s3://bkt/kb/KB111/DS/0.md" in out
    assert m.kb_search("   ") == "kb_search needs a non-empty query."
    assert m.kb_deep_search("   ") == "kb_search needs a non-empty query."  # one message (doc)
    assert "is not mounted" in m.kb_search("x", "KB999")
    deep = m.kb_deep_search("vacation days; carry-over rules")
    assert deep.count("source=") >= 2
    assert deep.count("source=s3://bkt/kb/KB111/DS/0.md") == 1  # de-duplicated
    monkeypatch.setattr(m, "make_session_manager", lambda a, s: None)  # no real S3 here
    assert [t.tool_name for t in m.build_agent("a", "s").tools][-2:] == ["kb_search",
                                                                          "kb_deep_search"]
