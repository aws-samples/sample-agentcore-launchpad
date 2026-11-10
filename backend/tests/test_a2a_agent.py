"""A2A-protocol agents — spec validation, template, deploy params, invoke,
card enrichment, experiment gating."""

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest
import strands
import strands.multiagent as strands_multiagent
from fastapi import FastAPI
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AppError
from app.deployer.zip_runtime import _generate_code, _method_requirements
from app.models.ledger import Agent
from app.schemas.agent import A2ASkill, AgentSpec
from app.services import registry_backend as reg
from app.services.agentcore import runtime as rt
from app.templates.strands_a2a_agent import render_a2a_main_py
from tests.conftest import ws_ctx
from tests.test_strands_template import _wire_identity

SKILL = {"id": "faq", "name": "Product FAQ",
         "description": "Answers product questions", "tags": ["support"]}


def _spec(**kw) -> AgentSpec:
    base = {"name": "a2a-check", "method": "zip_runtime", "system_prompt": "You help."}
    return AgentSpec(**{**base, **kw})


class _FakeA2AServer:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.strands_agent = kwargs["agent_factory"]("__agent_card__")

    def to_fastapi_app(self):
        return FastAPI()


class _FakeAgentSkill:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _import_a2a_rendered(
    spec: AgentSpec,
    tmp_path: Path,
    monkeypatch,
    stem: str,
    *,
    disable_memory: bool = True,
):
    code, _ = _generate_code(spec)
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", lambda fn: fn)
    a2a_package = types.ModuleType("a2a")
    a2a_package.__path__ = []
    a2a_types = types.ModuleType("a2a.types")
    a2a_types.AgentSkill = _FakeAgentSkill
    a2a_package.types = a2a_types
    strands_a2a = types.ModuleType("strands.multiagent.a2a")
    strands_a2a.A2AServer = _FakeA2AServer
    monkeypatch.setitem(sys.modules, "a2a", a2a_package)
    monkeypatch.setitem(sys.modules, "a2a.types", a2a_types)
    monkeypatch.setitem(sys.modules, "strands.multiagent.a2a", strands_a2a)
    monkeypatch.setattr(strands_multiagent, "a2a", strands_a2a, raising=False)
    if disable_memory:
        monkeypatch.delenv("LAUNCHPAD_MEMORY_ID", raising=False)
    target = tmp_path / f"{stem}.py"
    target.write_text(code, encoding="utf-8")
    module_spec = importlib.util.spec_from_file_location(stem, target)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    # Default working Identity exchange + stubbed BedrockModel (see
    # ``test_strands_template._wire_identity``) so tests that don't care about the
    # credential exchange can still call ``agent_factory``/``build_model`` freely.
    _wire_identity(module, monkeypatch)
    return module


# ─── spec validation ─────────────────────────────────────────────────────────
def test_protocol_defaults_to_http_and_old_specs_revalidate():
    assert _spec().protocol == "http"
    assert _spec().a2a_skills == []


def test_a2a_requires_zip_runtime_method():
    for method in ("harness", "container"):
        with pytest.raises(ValidationError, match="zip_runtime"):
            _spec(method=method, protocol="a2a")
    assert _spec(protocol="a2a").protocol == "a2a"


def test_a2a_rejects_custom_code_and_bundle():
    with pytest.raises(ValidationError, match="A2A template"):
        _spec(protocol="a2a", code="print('x')")
    with pytest.raises(ValidationError, match="A2A template"):
        _spec(protocol="a2a", code_bundle={"main.py": "print('x')"})


def test_a2a_skills_need_a2a_protocol_and_unique_ids():
    with pytest.raises(ValidationError, match="require protocol=a2a"):
        _spec(a2a_skills=[A2ASkill(**SKILL)])
    with pytest.raises(ValidationError, match="unique"):
        _spec(protocol="a2a", a2a_skills=[A2ASkill(**SKILL), A2ASkill(**SKILL)])


# ─── template + generate dispatch ────────────────────────────────────────────
def test_a2a_template_renders_compiles_and_carries_skills():
    spec = _spec(
        protocol="a2a",
        skills=["s3://launchpad-artifacts/skills/pirate-speak/"],
        a2a_skills=[A2ASkill(**SKILL)],
    )
    code, source = _generate_code(spec)
    assert source == "strands A2A template"
    compile(code, "main.py", "exec")  # brace-safe contract
    assert "A2AServer(" in code and "serve_at_root=True" in code
    assert "port=9000" in code
    assert "'id': 'faq'" in code
    assert "AGENTCORE_RUNTIME_URL" in code
    # N6: Launchpad's S3 snapshot session manager + AfterInvocation hook, no AgentCore Memory
    assert "S3SessionManager" in code and "AgentCoreMemorySessionManager" not in code
    assert "LAUNCHPAD_MEMORY_RUNTIME = 'v1'" in code and "bedrock_agentcore.memory" not in code
    # N16-obs79: the hub-sent actor wins, the per-context actor is the peer fallback
    assert "actor = _context_actor(context_id)" in code
    assert 'or f"{AGENT_ID or AGENT_NAME}__a2a__{context_id}"' in code  # id-keyed fallback (obs90a)
    assert 'A2A_ACTOR_METADATA_KEY = "launchpad_actor_id"' in code
    # N16-obs90: the actor binds only hub-signed and only for this agent's own actors
    assert 'A2A_ACTOR_SIG_METADATA_KEY = "launchpad_actor_sig"' in code
    assert 'os.environ.get("LAUNCHPAD_A2A_ACTOR_KEY", "")' in code
    assert "hmac.compare_digest(" in code
    # N16-obs90a: the prefix is the agent ID (what the hub signs), never the agent name
    assert 'AGENT_ID = os.environ.get("LAUNCHPAD_AGENT_ID", "")' in code
    assert 'actor.startswith(f"{AGENT_ID}__") or "__a2a__" in actor' in code
    assert 'f"{AGENT_NAME}__") or' not in code
    assert "SKILLS_ENABLED = True" in code
    assert "AgentSkills(skills=str(SKILLS_ROOT))" in code
    assert 'kwargs["plugins"] = plugins' in code


def test_a2a_template_serves_the_platform_card_version():
    """The live card (Strands A2AServer) and the registry record must agree on
    ``version``: both read ``A2A_CARD_VERSION``. Without an explicit ``version``
    Strands serves its own default ``0.0.1`` and the live-card diff flags every
    A2A agent by construction (SE-017 live check on aurora-faq-a2a)."""
    code, _ = _generate_code(_spec(protocol="a2a"))

    compile(code, "main.py", "exec")
    assert f"A2A_CARD_VERSION = {reg.A2A_CARD_VERSION!r}" in code
    server_block = code[code.index("server = A2AServer("):]
    server_block = server_block[: server_block.index("\n)")]
    assert "version=A2A_CARD_VERSION" in server_block
    assert reg.A2A_CARD_VERSION != "0.0.1"  # never collapse onto the Strands default


def test_build_a2a_card_defaults_version_to_the_platform_constant():
    card = reg.build_a2a_card(name="n", description="d", arn="arn:x", method="zip_runtime")
    assert card["version"] == reg.A2A_CARD_VERSION
    # an explicit value still wins (record edits / imports)
    assert reg.build_a2a_card(name="n", description="d", arn="arn:x", method="zip_runtime",
                              version="7")["version"] == "7"


@pytest.mark.parametrize("runtime_version", [None, "1", "3", "12"])
def test_register_agent_record_card_version_ignores_runtime_version(
    monkeypatch, runtime_version
):
    """``Agent.version`` is the AgentCore *runtime* version (assigned after the
    template is rendered) — it is not an A2A card field. The record carries the
    platform constant regardless."""
    from app.deployer.registration import register_agent_record

    calls: dict[str, dict] = {}

    class _StubRegistry:
        def upsert_record(self, workspace, **kwargs):
            calls["create"] = kwargs
            return {"recordId": "rec-1", "status": "CREATING"}, True

    reg.set_backend(_StubRegistry())
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name="aurora-faq-a2a", method="zip_runtime",
        status="active", version=runtime_version,
        arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/aurora-1",
        spec={"protocol": "a2a", "a2a_skills": [SKILL]},
    )

    result = register_agent_record(agent, ws_ctx(), auto_submit=False)

    assert result == {"record_id": "rec-1", "created": True, "changed": True}
    assert calls["create"]["descriptor_type"] == "A2A"
    record_card = reg.stored_a2a_card({"descriptors": calls["create"]["descriptors"]})
    assert record_card["version"] == reg.A2A_CARD_VERSION
    assert record_card["version"] != runtime_version
    # a runtime ARN gets the partition-aware data-plane URL of its workspace
    assert record_card["url"] == reg.data_plane_invocations_url(agent.arn, ws_ctx())

    # what the rendered runtime serves (Strands echoes ``version`` into the
    # public card) vs. what the record stores → identical on ``version``
    live_card = {"name": agent.name, "url": record_card["url"],
                 "version": reg.A2A_CARD_VERSION,
                 "protocolVersion": reg.A2A_SCHEMA_VERSION, "skills": [SKILL]}
    diff = reg.agent_card_diff(record_card, live_card)
    assert diff["identical"] is True and diff["fields"] == []


def test_register_agent_record_without_a_backend_is_unavailable():
    from app.deployer.registration import register_agent_record

    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="a", method="zip_runtime",
                  status="active", arn="arn:x", spec={})
    with pytest.raises(reg.RegistryUnavailableError):
        register_agent_record(agent, ws_ctx())


def test_rendered_template_and_record_agree_on_card_version():
    """End-to-end on the version field: pull the literal the rendered main.py
    hands to ``A2AServer(version=...)`` and diff it against a record card."""
    code, _ = _generate_code(_spec(protocol="a2a", a2a_skills=[A2ASkill(**SKILL)]))
    tree = ast.parse(code)
    rendered = next(
        ast.literal_eval(node.value) for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "A2A_CARD_VERSION" for t in node.targets)
    )
    record_card = reg.build_a2a_card(name="a2a-check", description="d", arn="arn:x",
                                     method="zip_runtime", url="https://u/", skills=[SKILL])
    live_card = {**record_card, "version": rendered}
    assert reg.agent_card_diff(record_card, live_card)["identical"] is True
    # and a Strands default would still be caught — the diff is not muted
    assert reg.agent_card_diff(record_card, {**record_card, "version": "0.0.1"})["fields"] == [
        {"field": "version", "record": reg.A2A_CARD_VERSION, "live": "0.0.1"}
    ]


def test_a2a_template_without_mounted_skills_disables_plugin():
    code, _ = _generate_code(_spec(protocol="a2a", a2a_skills=[A2ASkill(**SKILL)]))

    compile(code, "main.py", "exec")
    assert "SKILLS_ENABLED = False" in code
    assert "'id': 'faq'" in code  # AgentCard metadata remains independent


def test_a2a_skill_plugin_lifecycle_and_card_independence(tmp_path: Path, monkeypatch):
    class FakeAgentSkills:
        def __init__(self, *, skills):
            self.skills = skills

    monkeypatch.delattr(strands, "AgentSkills", raising=False)
    module = _import_a2a_rendered(
        _spec(
            protocol="a2a",
            skills=["s3://launchpad-artifacts/skills/pirate-speak/"],
            a2a_skills=[A2ASkill(**SKILL)],
        ),
        tmp_path,
        monkeypatch,
        "a2a_skill_main",
    )

    assert module.SKILLS_ENABLED is True
    assert not hasattr(module.agent_factory("missing-content"), "plugins")
    assert module.server.kwargs["skills"][0].id == "faq"

    skill_dir = tmp_path / "skills" / "pirate-speak"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# Pirate speech", encoding="utf-8")
    monkeypatch.setattr(strands, "AgentSkills", FakeAgentSkills, raising=False)

    mounted = module.agent_factory("mounted-content")
    assert mounted.plugins[0].skills == str(tmp_path / "skills")

    disabled = _import_a2a_rendered(
        _spec(protocol="a2a"),
        tmp_path,
        monkeypatch,
        "a2a_no_skill_main",
    )
    assert disabled.SKILLS_ENABLED is False
    assert not hasattr(disabled.agent_factory("disabled"), "plugins")


def test_a2a_card_context_skips_memory_but_real_context_keeps_it(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "memory-id")
    monkeypatch.setenv("LAUNCHPAD_MEMORY_BUCKET", "bkt")

    module = _import_a2a_rendered(
        _spec(protocol="a2a"),
        tmp_path,
        monkeypatch,
        "a2a_memory_card_main",
        disable_memory=False,
    )

    class FakeSessionManager:
        def __init__(self, actor_id, session_id):
            self.actor_id = actor_id
            self.session_id = session_id

    # the real one would read the session from S3 at construction (hermetic: no AWS)
    monkeypatch.setattr(module, "SnapshotSessionManager", FakeSessionManager)

    assert not hasattr(module.server.strands_agent, "session_manager")
    assert not hasattr(module.server.strands_agent, "hooks")
    for invalid_context in ("", "_internal", "bad/context"):
        card_only = module.agent_factory(invalid_context)
        assert not hasattr(card_only, "session_manager") and not hasattr(card_only, "hooks")
    actual = module.agent_factory("context-123")
    assert actual.session_manager.session_id == "context-123"
    assert actual.session_manager.actor_id == "a2a-check__a2a__context-123"
    (hook,) = actual.hooks
    assert isinstance(hook, module.LaunchpadMemoryHook)
    assert (hook.actor_id, hook.session_id) == ("a2a-check__a2a__context-123", "context-123")


FAKE_ACTOR_KEY = "fake-test-a2a-actor-key"  # C4: an obviously fake key, never a real one
# the ledger id the runtime holds as LAUNCHPAD_AGENT_ID — the hub signs actors built on IT
# (`memory_backend.scoped_actor(agent.id, human)`), never on the agent name (N16-obs90a)
AGENT_ID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def _hub_actor(human: str) -> str:
    """The actor exactly as the console builds it — the real helper, never a literal."""
    from app.services.memory_backend import scoped_actor

    return scoped_actor(AGENT_ID, human)


def _per_context(context: str) -> str:
    return f"{AGENT_ID}__a2a__{context}"


def _signed(module, context: str, actor: str, key: str = FAKE_ACTOR_KEY, **extra) -> dict:
    """A hub-shaped ``message/send`` envelope: actor + signature (``a2a_actor.sign_actor``)."""
    from app.services import a2a_actor

    metadata = {module.A2A_ACTOR_METADATA_KEY: actor,
                module.A2A_ACTOR_SIG_METADATA_KEY: a2a_actor.sign_actor(key, context, actor)}
    metadata.update(extra)
    return {
        "jsonrpc": "2.0", "id": "1", "method": "message/send",
        "params": {"message": {
            "role": "user", "messageId": "m1", "contextId": context, "metadata": metadata,
            "parts": [{"kind": "text", "text": "hi"}],
        }},
    }


def _actor_module(tmp_path: Path, monkeypatch, stem: str, *, key: str | None = FAKE_ACTOR_KEY):
    """The rendered A2A agent with memory on, a fake snapshot manager and (unless
    ``key=None``) the per-agent actor key in its environment."""
    from fastapi import Request

    monkeypatch.setenv("LAUNCHPAD_MEMORY_ID", "memory-id")
    monkeypatch.setenv("LAUNCHPAD_MEMORY_BUCKET", "bkt")
    if key is None:
        monkeypatch.delenv("LAUNCHPAD_A2A_ACTOR_KEY", raising=False)
    else:
        monkeypatch.setenv("LAUNCHPAD_A2A_ACTOR_KEY", key)
    monkeypatch.setenv("LAUNCHPAD_AGENT_ID", AGENT_ID)  # every deploy injects it
    seen_downstream: list[dict] = []

    def _echo_app(self):
        inner = FastAPI()

        @inner.post("/")
        async def rpc(request: Request):
            seen_downstream.append(await request.json())
            return {"jsonrpc": "2.0", "id": "1", "result": {"kind": "message", "parts": []}}

        return inner

    monkeypatch.setattr(_FakeA2AServer, "to_fastapi_app", _echo_app)
    module = _import_a2a_rendered(_spec(protocol="a2a"), tmp_path, monkeypatch, stem,
                                  disable_memory=False)

    class FakeSessionManager:
        def __init__(self, actor_id, session_id):
            self.actor_id = actor_id
            self.session_id = session_id

    monkeypatch.setattr(module, "SnapshotSessionManager", FakeSessionManager)
    return module, seen_downstream


def test_a2a_context_bound_by_the_hub_uses_the_console_actor(tmp_path: Path, monkeypatch):
    """N16-obs79: an A2A agent with memory wrote 0 events per console turn while its HTTP
    twin wrote 1 — the template keyed memory on ``<agent>__a2a__<context>`` while the
    console reads ``<agent>__<human>``. The hub now sends that actor as A2A message
    metadata, SIGNED with the per-agent key (N16-obs90); the middleware binds it to the
    context (and still hands the body on to the A2A app) and the factory keys the
    snapshot manager + turn-event hook on it."""
    from fastapi.testclient import TestClient

    module, seen_downstream = _actor_module(tmp_path, monkeypatch, "a2a_actor_bind_main")
    context = "c" * 40
    envelope = _signed(module, context, _hub_actor("river"))
    res = TestClient(module.app).post("/", json=envelope)
    assert res.status_code == 200, res.text
    # the body the A2A app received is the one the hub sent (middleware read did not eat it)
    assert seen_downstream == [envelope]

    bound = module.agent_factory(context)
    assert bound.session_manager.actor_id == _hub_actor("river") == f"{AGENT_ID}__river"
    (hook,) = bound.hooks
    assert (hook.actor_id, hook.session_id) == (_hub_actor("river"), context)
    # a context no caller bound (peer-to-peer) keeps the per-context actor — keyed on the
    # agent ID like every other partition of this agent (N16-obs90a)
    peer = module.agent_factory("context-peer")
    assert peer.session_manager.actor_id == _per_context("context-peer")
    # malformed metadata is ignored, never bound
    for bad in ({"metadata": "x"}, {}):
        env2 = {**envelope, "params": {"message": {
            "role": "user", "messageId": "m2", "contextId": "d" * 40, "parts": [], **bad}}}
        assert TestClient(module.app).post("/", json=env2).status_code == 200
    assert module.agent_factory("d" * 40).session_manager.actor_id == _per_context("d" * 40)
    module._bind_context_actor(b"not json")  # tolerated


def test_a2a_unsigned_or_forged_actor_metadata_is_refused(tmp_path: Path, monkeypatch, caplog):
    """N16-obs90 (rev-r16 FIX-FIRST, security): any InvokeAgentRuntime holder — a
    member-authored BYOC peer with an a2a ToolRef — can send metadata; naming
    ``<agent>__<other human>`` must NOT give it that human's memory. Without a valid
    signature the per-context actor stays, nothing is written under the forged actor, and
    one WARNING per context names the reason — never the actor value."""
    import logging

    from fastapi.testclient import TestClient

    module, _ = _actor_module(tmp_path, monkeypatch, "a2a_actor_forged_main")
    client = TestClient(module.app)
    victim = _hub_actor("alice")

    def bound(context: str) -> str:
        return module.agent_factory(context).session_manager.actor_id

    with caplog.at_level(logging.WARNING, logger="a2a-check"):
        # (1) a peer-shaped call: the actor alone, no signature
        c1 = "p" * 40
        forged = _signed(module, c1, victim)
        del forged["params"]["message"]["metadata"][module.A2A_ACTOR_SIG_METADATA_KEY]
        assert client.post("/", json=forged).status_code == 200
        assert bound(c1) == _per_context(c1)
        # (2) a wrong key
        c2 = "q" * 40
        other = _signed(module, c2, victim, key="some-other-key")
        assert client.post("/", json=other).status_code == 200
        assert bound(c2) == _per_context(c2)
        # (3) a signature captured for one context, replayed on another (tampered contextId)
        c3, c4 = "r" * 40, "s" * 40
        replay = _signed(module, c3, victim)
        replay["params"]["message"]["contextId"] = c4
        assert client.post("/", json=replay).status_code == 200
        assert bound(c4) == _per_context(c4)
        # a second refusal on the same context logs nothing more
        assert client.post("/", json=replay).status_code == 200
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "not bound" in r.getMessage()]
    assert len(warnings) == 3, [r.getMessage() for r in warnings]
    assert all("alice" not in r.getMessage() for r in warnings)  # no actor value in the log
    reasons = {r.getMessage().split("not bound: ")[1].split(" —")[0] for r in warnings}
    assert reasons == {"missing or invalid launchpad_actor_sig"}
    assert module._CONTEXT_ACTORS == {}


def test_a2a_binds_only_this_agents_own_actors(tmp_path: Path, monkeypatch, caplog):
    """N16-obs90 (c): even a correctly signed actor is bound only with the agent's own
    ``<name>__`` prefix and never in the per-context ``__a2a__`` shape."""
    import logging

    from fastapi.testclient import TestClient

    module, _ = _actor_module(tmp_path, monkeypatch, "a2a_actor_prefix_main")
    client = TestClient(module.app)
    with caplog.at_level(logging.WARNING, logger="a2a-check"):
        for context, actor in (
            ("u" * 40, "other-agent-id__river"),  # another agent's actor
            ("v" * 40, _per_context("v" * 40)),  # the per-context shape, signed
            # N16-obs90a (live t1, deploy-ops-10 T54): an actor on the agent NAME is not what
            # the hub signs (`scoped_actor(agent.id, human)`) — the first obs90 build compared
            # the name and so refused every legitimate console turn
            ("y" * 40, "a2a-check__river"),
        ):
            assert client.post("/", json=_signed(module, context, actor)).status_code == 200
            assert module.agent_factory(context).session_manager.actor_id == _per_context(context)
    assert sum("actor is not one of this agent's own" in r.getMessage()
               for r in caplog.records) == 3
    # the agent's own actor, correctly signed, still binds
    assert client.post("/", json=_signed(module, "w" * 40, _hub_actor("bob"))).status_code == 200
    assert module.agent_factory("w" * 40).session_manager.actor_id == _hub_actor("bob")


def test_a2a_runtime_without_an_agent_id_never_binds(tmp_path: Path, monkeypatch, caplog):
    """Ownership is checked against LAUNCHPAD_AGENT_ID; a runtime without it cannot tell its
    own actors apart and keeps the (name-based, legacy) per-context actor."""
    import logging

    from fastapi.testclient import TestClient

    module, _ = _actor_module(tmp_path, monkeypatch, "a2a_actor_noid_main")
    monkeypatch.setattr(module, "AGENT_ID", "")
    with caplog.at_level(logging.WARNING, logger="a2a-check"):
        res = TestClient(module.app).post("/", json=_signed(module, "z" * 40, _hub_actor("river")))
    assert res.status_code == 200
    assert module.agent_factory("z" * 40).session_manager.actor_id == f"a2a-check__a2a__{'z' * 40}"
    assert any("no LAUNCHPAD_AGENT_ID" in r.getMessage() for r in caplog.records)


def test_a2a_runtime_without_the_actor_key_never_binds(tmp_path: Path, monkeypatch, caplog):
    """A runtime deployed without ``LAUNCHPAD_A2A_ACTOR_KEY`` (pre-obs90 build) cannot
    verify anything: the per-context actor stays and the reason is logged once."""
    import logging

    from fastapi.testclient import TestClient

    module, _ = _actor_module(tmp_path, monkeypatch, "a2a_actor_nokey_main", key=None)
    with caplog.at_level(logging.WARNING, logger="a2a-check"):
        res = TestClient(module.app).post("/", json=_signed(module, "x" * 40, _hub_actor("river")))
    assert res.status_code == 200
    assert module.agent_factory("x" * 40).session_manager.actor_id == _per_context("x" * 40)
    assert any("no LAUNCHPAD_A2A_ACTOR_KEY" in r.getMessage() for r in caplog.records)


def test_a2a_without_a_store_builds_a_plain_agent(tmp_path: Path, monkeypatch):
    module = _import_a2a_rendered(_spec(protocol="a2a"), tmp_path, monkeypatch, "a2a_nomem_main")
    agent = module.agent_factory("context-123")
    assert not hasattr(agent, "session_manager") and not hasattr(agent, "hooks")


def test_a2a_render_preserves_skills_placeholder_literal_in_prompt():
    # round 16 rev: the schema now refuses the renderers' marker in member text for every
    # method (AgentSpec._no_reserved_placeholder, 422 naming the field), so a prompt carrying
    # it only reaches the template from a STORED spec that predates the rule — the template
    # must still keep the literal (replace order) rather than substitute inside the prompt
    with pytest.raises(ValueError, match="system_prompt"):
        _spec(protocol="a2a", system_prompt="Keep __LAUNCHPAD_SKILLS_ENABLED__ literal.")
    stored = _spec(protocol="a2a")
    stored.system_prompt = "Keep __LAUNCHPAD_SKILLS_ENABLED__ literal."
    code, _ = _generate_code(stored)

    assert "SYSTEM_PROMPT = 'Keep __LAUNCHPAD_SKILLS_ENABLED__ literal.'" in code
    assert "SKILLS_ENABLED = False" in code


def test_http_template_untouched_by_protocol_field():
    code, source = _generate_code(_spec())
    assert source == "strands template"
    assert "A2AServer" not in code


def test_a2a_template_builds_its_model_with_the_global_credential():
    code = render_a2a_main_py(_spec(protocol="a2a"))
    assert "api_key=_resolve_bedrock_api_key()" in code
    assert "region_name=MODEL_REGION" in code and "max_tokens=MODEL_MAX_TOKENS" in code
    assert 'request.headers.get("WorkloadAccessToken")' in code  # same header the SDK reads
    assert 'os.environ.get("LAUNCHPAD_MODEL_REGION", "us-west-2")' in code
    assert "__LAUNCHPAD_" not in code
    compile(code, "main.py", "exec")


def test_a2a_requirements_carry_the_a2a_extra():
    reqs = _method_requirements(_spec(protocol="a2a", requirements=["httpx==0.28.1"]))
    assert any(r.startswith("strands-agents[a2a") for r in reqs)
    assert any(r.startswith("boto3>=1.43.109") for r in reqs)
    assert "httpx==0.28.1" in reqs
    http_reqs = _method_requirements(_spec())
    assert not any("[a2a" in r for r in http_reqs)



# ─── deploy params ───────────────────────────────────────────────────────────
class _Stub:
    def __init__(self):
        self.created_with = None
        self.updated_with = None

    def create_agent_runtime(self, **kw):
        self.created_with = kw
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1"}

    def update_agent_runtime(self, **kw):
        self.updated_with = kw
        return {"agentRuntimeId": kw["agentRuntimeId"], "agentRuntimeVersion": "2"}


def test_create_code_runtime_sets_protocol_only_for_a2a():
    stub = _Stub()
    rt.create_code_runtime(stub, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="a2a")
    assert stub.created_with["protocolConfiguration"] == {"serverProtocol": "A2A"}
    stub2 = _Stub()
    rt.create_code_runtime(stub2, runtime_name="n", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="http")
    assert "protocolConfiguration" not in stub2.created_with


def test_update_code_runtime_echoes_protocol():
    """UpdateAgentRuntime resets an omitted protocolConfiguration (probed live)
    — the update path must always re-send it for A2A agents."""
    stub = _Stub()
    rt.update_code_runtime(stub, runtime_id="rt-1", s3_bucket="b", s3_key="k",
                           role_arn="r", protocol="a2a")
    assert stub.updated_with["protocolConfiguration"] == {"serverProtocol": "A2A"}


# ─── invoke ──────────────────────────────────────────────────────────────────
class _DataStub:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()
        self.invoked_with = None

    def invoke_agent_runtime(self, **kw):
        self.invoked_with = kw

        class R:
            def __init__(self, d):
                self._d = d

            def read(self):
                return self._d

        return {"response": R(self.body)}


def test_invoke_a2a_text_sends_message_send_and_parses_task():
    stub = _DataStub({"jsonrpc": "2.0", "id": "1", "result": {
        "artifacts": [{"parts": [{"kind": "text", "text": "A2A OK"}]}],
        # history carries the user turn + agent streaming fragments (probed
        # live) — must be ignored entirely. The user turn is load-bearing:
        # fragments alone join to exactly "A2A OK", so a parser that wrongly
        # read history instead of artifacts would still pass without it.
        "history": [{"role": "user", "parts": [{"kind": "text", "text": "ping"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": "A2"}]},
                    {"role": "agent", "parts": [{"kind": "text", "text": "A OK"}]}],
    }})
    session_id = "s" * 40
    out = rt.invoke_a2a_text(stub, "arn:rt-1", "ping", session_id=session_id)
    assert out["text"] == "A2A OK"
    payload = json.loads(stub.invoked_with["payload"])
    assert payload["method"] == "message/send"
    assert payload["params"]["message"]["parts"] == [{"kind": "text", "text": "ping"}]
    assert payload["params"]["message"]["contextId"] == session_id
    assert stub.invoked_with["runtimeSessionId"] == session_id
    assert len(stub.invoked_with["runtimeSessionId"]) >= 16


def test_invoke_a2a_text_carries_the_signed_memory_actor_as_message_metadata():
    """N16-obs79 / N16-obs90: ``actor_id`` rides as ``message.metadata.launchpad_actor_id``
    together with ``launchpad_actor_sig`` over (contextId, actor) under ``actor_key``; a call
    without a key sends no actor metadata at all (the template would refuse it anyway)."""
    from app.services import a2a_actor

    stub = _DataStub({"result": {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}})
    rt.invoke_a2a_text(stub, "arn:rt-1", "x", session_id="s" * 40, actor_id="agent-1__river",
                       actor_key="fake-test-a2a-actor-key")
    message = json.loads(stub.invoked_with["payload"])["params"]["message"]
    assert message["metadata"][rt.A2A_ACTOR_METADATA_KEY] == "agent-1__river"
    assert a2a_actor.verify_actor("fake-test-a2a-actor-key", "s" * 40, "agent-1__river",
                                  message["metadata"][rt.A2A_ACTOR_SIG_METADATA_KEY])
    assert set(message["metadata"]) == {rt.A2A_ACTOR_METADATA_KEY, rt.A2A_ACTOR_SIG_METADATA_KEY}
    rt.invoke_a2a_text(stub, "arn:rt-1", "x", actor_id="agent-1__river")  # no key
    assert "metadata" not in json.loads(stub.invoked_with["payload"])["params"]["message"]
    rt.invoke_a2a_text(stub, "arn:rt-1", "x")
    assert "metadata" not in json.loads(stub.invoked_with["payload"])["params"]["message"]


def test_invoke_a2a_text_parses_message_shape():
    stub = _DataStub({"result": {"kind": "message",
                                 "parts": [{"kind": "text", "text": "hi"}]}})
    assert rt.invoke_a2a_text(stub, "arn:rt-1", "x")["text"] == "hi"


def test_invoke_a2a_text_raises_jsonrpc_error():
    stub = _DataStub({"error": {"code": -32502, "message": "Validation error"}})
    with pytest.raises(RuntimeError, match="-32502"):
        rt.invoke_a2a_text(stub, "arn:rt-1", "x")


def test_invoke_a2a_text_relays_a_failed_task_status():
    """N10v3-bug5: the A2A server answers a *failed* Task (no artifacts) when the agent
    factory raised; the console must surface that reason, never an empty reply."""
    stub = _DataStub({"result": {
        "kind": "task", "artifacts": [],
        "status": {"state": "failed", "message": {"role": "agent", "parts": [
            {"kind": "text", "text": "RuntimeError: no workload access token"}]}},
    }})
    with pytest.raises(AppError, match="failed.*no workload access token") as info:
        rt.invoke_a2a_text(stub, "arn:rt-1", "x")
    # a 502 with the reason on /v1 (review F5), an `error` event in chat
    assert info.value.code == "agent.invoke_failed" and info.value.status_code == 502


def test_invoke_a2a_text_never_returns_silence():
    stub = _DataStub({"result": {"kind": "task", "artifacts": [],
                                 "status": {"state": "completed"}}})
    with pytest.raises(AppError, match="returned no text"):
        rt.invoke_a2a_text(stub, "arn:rt-1", "x")


@pytest.mark.parametrize("state", ["input-required", "auth-required"])
def test_invoke_a2a_text_treats_an_input_request_as_the_reply(state):
    """Review F3: a Task asking the user for input / authorization carries that question
    in status.message — it is the reply, not a failure and not "no text"."""
    stub = _DataStub({"result": {"kind": "task", "artifacts": [], "status": {
        "state": state, "message": {"role": "agent", "parts": [
            {"kind": "text", "text": "Which account should I look at?"}]}}}})
    assert rt.invoke_a2a_text(stub, "arn:rt-1", "x")["text"] == "Which account should I look at?"
    # artifacts still win when present
    assert rt.a2a_result_text({"kind": "task", "status": {"state": state, "message": {
        "parts": [{"kind": "text", "text": "q"}]}},
        "artifacts": [{"parts": [{"kind": "text", "text": "final"}]}]}) == "final"


def test_invoke_agent_text_dispatches_on_protocol(monkeypatch):
    from app.services import invoke as inv

    calls = []
    monkeypatch.setattr(inv, "data_client", lambda _ws=None: object())
    monkeypatch.setattr(inv.rt, "invoke_a2a_text",
                        lambda *a, **k: calls.append("a2a") or {"text": "", "session_id": "s"})
    monkeypatch.setattr(inv.rt, "invoke_runtime_text",
                        lambda *a, **k: calls.append("http") or {"text": "", "session_id": "s"})
    a2a_agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="x", method="zip_runtime", status="active",
                      arn="arn:rt", spec={"protocol": "a2a"})
    http_agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="y", method="zip_runtime", status="active",
                       arn="arn:rt", spec={})
    inv.invoke_agent_text(a2a_agent, "hi")
    inv.invoke_agent_text(http_agent, "hi")
    assert calls == ["a2a", "http"]


def test_invoke_agent_text_hands_the_a2a_branch_the_memory_actor(monkeypatch):
    """N16-obs79: the console's ``<agent>__<human>`` actor reaches the A2A wrapper (as
    metadata) exactly as the HTTP branch receives it in the envelope."""
    from app.services import invoke as inv

    seen: list[dict] = []
    monkeypatch.setattr(inv, "data_client", lambda _ws=None: object())
    monkeypatch.setattr(inv.rt, "invoke_a2a_text",
                        lambda *a, **k: seen.append(k) or {"text": "ok", "session_id": "s"})
    # N16-obs90: the per-agent key (derived from the console secret) rides along so the
    # wrapper signs the actor; the key itself never leaves the process
    monkeypatch.setattr(inv.a2a_actor, "actor_key",
                        lambda agent_id: f"fake-key-for-{agent_id}")
    agent = Agent(id="ag-a2a", workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                  status="active", arn="arn:rt", spec={"protocol": "a2a"})
    inv.invoke_agent_text(agent, "hi", actor_id="x__river", session_id="s" * 40)
    assert seen[-1]["actor_id"] == "x__river"
    assert seen[-1]["runtime_user_id"] == "x__river"
    assert seen[-1]["actor_key"] == "fake-key-for-ag-a2a"


# ─── card enrichment ─────────────────────────────────────────────────────────
def test_data_plane_invocations_url_is_partition_aware():
    from app.services.workspace import WorkspaceContext

    arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/x-1"
    url = reg.data_plane_invocations_url(
        arn, WorkspaceContext(account_id="1", region="cn-northwest-1"))
    assert url.startswith("https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/runtimes/")
    # the ARN is percent-encoded (InvokeAgentRuntime path segment), never raw
    assert "arn%3Aaws-cn%3Abedrock-agentcore%3Acn-northwest-1%3A1%3Aruntime%2Fx-1" in url
    assert arn not in url and url.endswith("/invocations?qualifier=DEFAULT")
    us = reg.data_plane_invocations_url(arn, WorkspaceContext(account_id="1", region="us-west-2"))
    assert us.startswith("https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/")


def test_build_a2a_card_defaults_match_legacy_shape():
    card = reg.build_a2a_card(name="n", description="d", arn="arn:x",
                              version="1", method="zip_runtime")
    assert card["url"] == "arn:x" and card["skills"] == []
    assert card["metadata"]["launchpad.transport"] == "agentcore-http"


def test_build_a2a_card_a2a_variant_carries_url_skills_transport():
    card = reg.build_a2a_card(
        name="n", description="d", arn="arn:x", version="1", method="zip_runtime",
        url="https://endpoint/invocations/", skills=[SKILL],
        transport="a2a-jsonrpc")
    assert card["url"] == "https://endpoint/invocations/"
    assert card["skills"] == [SKILL]
    assert card["metadata"]["launchpad.transport"] == "a2a-jsonrpc"
    assert "JSON-RPC" in card["metadata"]["launchpad.invoke"]


def test_derive_card_skills_explicit_wins():
    assert reg.derive_card_skills({"a2a_skills": [SKILL]}) == [SKILL]


def test_derive_card_skills_from_spec_surfaces():
    spec = {
        "method": "harness",
        "tools": [{"type": "builtin", "name": "code-interpreter"},
                  {"type": "gateway", "name": "aws-knowledge"}],
        "knowledge_bases": [{"kb_id": "BL6", "name": "aurora-deck-docs",
                             "description": "Aurora Deck product documentation"}],
        "skills": ["s3://bkt/skills/meeting-summarizer/"],
    }
    skills = reg.derive_card_skills(spec)
    assert [s["id"] for s in skills] == [
        "code-interpreter", "aws-knowledge", "aurora-deck-docs",
        "meeting-summarizer",
    ]
    kb = skills[2]
    assert kb["description"] == "Aurora Deck product documentation"  # routing signal
    assert kb["tags"] == ["knowledge"]


def test_derive_card_skills_zip_template_and_dedup():
    # template agents advertise the baked-in tools even with empty spec.tools
    assert [s["id"] for s in reg.derive_card_skills({"method": "zip_runtime"})] == [
        "calculator", "current-time",
    ]
    # code-carrying agents don't get template tools; duplicate names get suffixes
    studio = {"method": "studio", "code": "x", "tools": [
        {"type": "mcp", "name": "search"}, {"type": "gateway", "name": "Search"}]}
    assert [s["id"] for s in reg.derive_card_skills(studio)] == ["search", "search-2"]


def test_a2a_model_block_is_the_shared_slice_above_the_memory_library():
    """N8 item 4: the A2A template carries no model implementation of its own — its model
    block is `model_support.render_model_source()` (one key cache, one credential path, C4),
    rendered at module scope ABOVE the inlined memory library, which reads
    `_resolve_bedrock_api_key` / `MODEL_REGION` / `BEDROCK_BEARER_ENV` from it."""
    from app.templates import model_support
    from app.templates.memory_support import MEMORY_SOURCE_MARKER

    spec = _spec(protocol="a2a")
    code = render_a2a_main_py(spec)
    template = Path(model_support.STRANDS_TEMPLATE).with_name("main.py.tmpl")
    a2a_template = Path(render_a2a_main_py.__code__.co_filename).with_name("main.py.tmpl")
    assert "__LAUNCHPAD_MODEL_SOURCE__" in a2a_template.read_text(encoding="utf-8")
    for needle in ("def _resolve_bedrock_api_key", "def build_model",
                   "_BEDROCK_API_KEY: dict[str, str] = {}", "_WORKLOAD_TOKEN: dict[str, str] = {}",
                   "def _workload_access_token", "def _launchpad_bedrock_model", '\nMODEL_ID = "'):
        assert a2a_template.read_text(encoding="utf-8").count(needle) == 0, needle
        assert code.count(needle) == 1, needle
    block = model_support.render_model_source(spec)
    for chunk in block.split("\n\n\n"):
        assert chunk.strip() in code
    assert code.index(block) < code.index(MEMORY_SOURCE_MARKER)
    assert "MODEL_MAX_TOKENS = 16384" in code  # the GLM default, rendered by the slice
    # the A2A-only token capture still feeds the shared exchange: A2AServer has no
    # per-request context hook, so the middleware fills the slot the slice consults first
    assert '_WORKLOAD_TOKEN["value"] = token' in code
    assert '_WORKLOAD_TOKEN.get("value")' in block
    assert template.read_text(encoding="utf-8").count("_WORKLOAD_TOKEN") >= 2
    compile(code, "main.py", "exec")


def test_a2a_captured_token_wins_over_the_injected_one(tmp_path: Path, monkeypatch):
    module = _import_a2a_rendered(_spec(protocol="a2a"), tmp_path, monkeypatch, "a2a_token_main")
    identity = module._identity_client()
    module._WORKLOAD_TOKEN["value"] = "captured-from-header"
    module.build_model()
    assert identity.calls[-1]["workloadIdentityToken"] == "captured-from-header"


# ─── N10v3-bug5 live correction: the token comes from the caller's runtimeUserId ─
def test_a2a_middleware_captures_the_header_token_and_resolves_the_key_on_the_request_path(
    tmp_path: Path, monkeypatch,
):
    """The Runtime injects ``WorkloadAccessToken`` only when the caller sent runtimeUserId;
    the middleware keeps it and exchanges it right there, so the executor's agent_factory
    (no request context) finds the key cached."""
    from fastapi.testclient import TestClient

    module = _import_a2a_rendered(_spec(protocol="a2a"), tmp_path, monkeypatch, "a2a_mw_main")
    identity = module._identity_client()
    monkeypatch.setattr(module.BedrockAgentCoreContext, "get_workload_access_token",
                        staticmethod(lambda: None))  # A2AServer never fills the SDK context
    module._WORKLOAD_TOKEN.clear()
    module._BEDROCK_API_KEY.clear()
    with TestClient(module.app) as client:
        assert client.get("/ping").status_code == 200  # no token, no exchange
        assert identity.calls == [] and module._WORKLOAD_TOKEN == {}
        res = client.get("/ping", headers={"WorkloadAccessToken": "hdr-token"})
        assert res.status_code == 200
        assert module._WORKLOAD_TOKEN == {"value": "hdr-token"}
        assert identity.calls == [{"workloadIdentityToken": "hdr-token",
                                   "resourceCredentialProviderName": "launchpad-global-bedrock"}]
        assert module._BEDROCK_API_KEY["key"] == "bedrock-api-key"
        client.get("/ping", headers={"WorkloadAccessToken": "hdr-token-2"})
        assert len(identity.calls) == 1  # cached: one exchange per process
    # agent_factory, off the request path, builds on the cached key without Identity
    module.agent_factory("ctx-0123456789abcdef")
    assert len(identity.calls) == 1


def test_a2a_middleware_leaves_a_failed_exchange_to_build_model(tmp_path: Path, monkeypatch):
    from fastapi.testclient import TestClient

    module = _import_a2a_rendered(_spec(protocol="a2a"), tmp_path, monkeypatch, "a2a_mw_fail")
    identity = module._identity_client()
    module._WORKLOAD_TOKEN.clear()
    module._BEDROCK_API_KEY.clear()

    def refuse(**kw):
        identity.calls.append(kw)
        raise RuntimeError("AccessDeniedException: no GetResourceApiKey")

    monkeypatch.setattr(identity, "get_resource_api_key", refuse)
    with TestClient(module.app) as client:
        assert client.get("/ping", headers={"WorkloadAccessToken": "hdr-token"}).status_code == 200
    assert "key" not in module._BEDROCK_API_KEY
    with pytest.raises(RuntimeError, match="AccessDeniedException"):
        module.build_model()  # the readable cause surfaces inside the task


def test_invoke_a2a_text_sends_runtime_user_id_bounded():
    stub = _DataStub({"result": {"kind": "message", "parts": [{"kind": "text", "text": "hi"}]}})
    rt.invoke_a2a_text(stub, "arn:rt-1", "x", runtime_user_id="river")
    assert stub.invoked_with["runtimeUserId"] == "river"
    rt.invoke_a2a_text(stub, "arn:rt-1", "x", runtime_user_id="u" * 2000)
    assert stub.invoked_with["runtimeUserId"] == "u" * 1024
    rt.invoke_a2a_text(stub, "arn:rt-1", "x")
    assert "runtimeUserId" not in stub.invoked_with
