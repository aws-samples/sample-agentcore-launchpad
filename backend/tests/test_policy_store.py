"""Policy store (plan §6 — test_policy_store): CRUD, versioning, publish with the S3 copy,
retire / delete guards, the journal, workspace isolation, the compiled-set cache and
its invalidation, settings / mode rules, decision rows, scoped tables + migration."""

from datetime import UTC, datetime, timedelta

import pytest

from app.core import db as db_module
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.models.policy import PolicyChange, PolicyDecision
from app.services import aws_clients, policy_backend, policy_bundle, policy_cedar, policy_store
from app.services.policy_backend import AuthzRequest
from app.services.policy_cedar import CedarEngine
from app.services.policy_entities import Principal, request_context, tool_ref
from tests.conftest import ws_ctx
from tests.policy_fakes import BASELINE, BROKEN, FORBID_KEY, FakeEngine, FakeS3

WS = "default"
BUCKET = "launchpad-artifacts-test"


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    return eng


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def workspace():
    return ws_ctx({"artifacts_bucket": BUCKET}, id=WS)


def _agent(db, agent_id="ag1", name="hr-agent", **fields) -> Agent:
    row = Agent(id=agent_id, workspace_id=WS, name=name, method="zip_runtime", status="active",
                spec=fields.pop("spec", {}), **fields)
    db.add(row)
    db.commit()
    return row


def _ops(db):
    return [c.operation for c in db.query(PolicyChange).order_by(PolicyChange.created_at).all()]


# ── CRUD + versions ─────────────────────────────────────────────────────────


def test_create_is_a_validated_draft_v1(db, engine):
    policy = policy_store.create_policy(db, WS, name="baseline", statement=BASELINE,
                                        operator="river", prefill="permit_users_call_tools")
    assert policy.id.startswith("pol_") and policy.status == "draft"
    assert policy.enforcement_mode == "log_only"  # Global parity: a new policy starts LOG_ONLY
    assert policy.draft_version == 1 and policy.active_version is None
    view = policy_store.policy_view(db, policy, versions=True)
    assert view["validation"]["ok"] is True and view["statement"] == BASELINE
    assert view["policy_ids"] == ["policy0", "policy1", "policy2", "policy3"]
    assert [v["version"] for v in view["versions"]] == [1]
    assert view["versions"][0]["schema_digest"]
    assert _ops(db) == ["policy.create"]

    with pytest.raises(AppError) as taken:
        policy_store.create_policy(db, WS, name="baseline", statement=BASELINE, operator="river")
    assert taken.value.code == "policy.name_taken" and taken.value.status_code == 409
    with pytest.raises(AppError) as bad_name:
        policy_store.create_policy(db, WS, name="1bad", statement=BASELINE, operator="river")
    assert bad_name.value.code == "policy.invalid_name"


def test_invalid_statement_is_422_with_the_report(db, engine):
    with pytest.raises(AppError) as exc:
        policy_store.create_policy(db, WS, name="broken", statement=BROKEN, operator="river")
    assert exc.value.code == "policy.invalid_statement" and exc.value.status_code == 422
    assert exc.value.detail["ok"] is False and exc.value.detail["errors"]
    assert db.query(PolicyChange).count() == 0  # nothing journaled, nothing stored


def test_update_of_active_creates_a_draft_and_keeps_active_live(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    policy = policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert policy.status == "active" and policy.active_version == 1 and policy.draft_version is None

    policy = policy_store.update_policy(db, WS, policy.id, statement=BASELINE + FORBID_KEY.replace(
        "__KEY__", "k9"), operator="river")
    assert policy.status == "active" and policy.active_version == 1 and policy.draft_version == 2
    view = policy_store.policy_view(db, policy)
    assert "forbid" in view["statement"]  # the editor shows the draft
    assert "forbid" not in policy_store.active_version(db, policy).statement

    # same statement again → no new version; description only → no new version
    same = policy_store.update_policy(db, WS, policy.id, statement=view["statement"],
                                      description="d", operator="river")
    assert same.draft_version == 2 and same.description == "d"
    assert len(policy_store.list_versions(db, same)) == 2
    assert _ops(db)[-3:] == ["policy.publish", "policy.update", "policy.update"]


def test_publish_writes_the_immutable_s3_copy(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    policy_store.publish_policy(db, workspace, policy.id, reason="first", operator="river")
    assert len(s3.calls) == 1
    call = s3.calls[0]
    assert call["Bucket"] == BUCKET and call["Key"] == f"policy/policies/{policy.id}/v1.cedar"
    assert call["IfNoneMatch"] == "*" and call["Body"].decode() == BASELINE
    version = policy_store.get_version(db, policy, 1)
    assert version.s3_key == call["Key"] and version.published_at is not None
    assert version.published_by == "river"
    change = db.query(PolicyChange).filter_by(operation="policy.publish").one()
    assert change.status == "succeeded" and change.after["s3_key"] == call["Key"]
    assert change.override_reason == "first"

    # re-publishing a version that already has its copy never rewrites the key
    policy_store.retire_policy(db, WS, policy.id, operator="river")
    again = policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert again.status == "active" and len(s3.calls) == 1


def test_publish_is_idempotent_when_the_key_already_exists(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    s3.objects[(BUCKET, f"policy/policies/{policy.id}/v1.cedar")] = b"earlier attempt"
    published = policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert published.status == "active"
    assert policy_store.get_version(db, published, 1).s3_key.endswith("/v1.cedar")


def test_publish_fails_closed_when_s3_fails(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    s3.fail_with = "AccessDenied"
    with pytest.raises(AppError) as exc:
        policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert exc.value.code == "policy.s3_copy_failed" and exc.value.status_code == 502
    db.expire_all()
    policy = policy_store.get_policy(db, WS, policy.id)
    assert policy.status == "draft" and policy.active_version is None
    failed = db.query(PolicyChange).filter_by(operation="policy.publish").one()
    assert failed.status == "failed" and "AccessDenied" in failed.error

    bare = ws_ctx({}, id=WS)
    with pytest.raises(AppError) as missing:
        policy_store.publish_policy(db, bare, policy.id, operator="river")
    assert missing.value.code == "workspace.not_ready"


def test_publish_guards(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    with pytest.raises(AppError) as nover:
        policy_store.publish_policy(db, workspace, policy.id, version=7, operator="river")
    assert nover.value.code == "policy.version_not_found"
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    with pytest.raises(AppError) as nodraft:
        policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert nodraft.value.code == "policy.no_draft"


def test_retire_and_delete_guards(db, engine, s3, workspace):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    with pytest.raises(AppError) as not_active:
        policy_store.retire_policy(db, WS, policy.id, operator="river")
    assert not_active.value.code == "policy.not_active"
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    with pytest.raises(AppError) as active:
        policy_store.delete_policy(db, WS, policy.id, operator="river")
    assert active.value.code == "policy.active" and active.value.status_code == 409

    # Global: the last ACTIVE policy may be retired (an ENFORCE gateway then denies everything)
    policy_store.retire_policy(db, WS, policy.id, reason="done", operator="river")
    policy_store.delete_policy(db, WS, policy.id, operator="river")
    with pytest.raises(AppError) as gone:
        policy_store.get_policy(db, WS, policy.id)
    assert gone.value.status_code == 404
    assert policy_store.list_versions(db, policy) == []
    assert _ops(db)[-2:] == ["policy.retire", "policy.delete"]


def test_workspace_isolation(db, engine):
    policy = policy_store.create_policy(db, WS, name="p", statement=BASELINE, operator="river")
    with pytest.raises(AppError) as exc:
        policy_store.get_policy(db, "other-ws", policy.id)
    assert exc.value.code == "policy.not_found" and exc.value.status_code == 404
    assert policy_store.list_policies(db, "other-ws") == []
    # the same name is free in another workspace
    other = policy_store.create_policy(db, "other-ws", name="p", statement=BASELINE,
                                       operator="river")
    assert other.id != policy.id


def test_tables_are_workspace_scoped_and_registered():
    for table in ("policy_engines", "policies", "policy_versions", "policy_settings",
                  "policy_changes", "policy_decisions"):
        assert table in db_module.WORKSPACE_SCOPED_TABLES
    assert "app.models.policy" in db_module.MODEL_MODULES
    import inspect as py_inspect

    source = py_inspect.getsource(db_module._migrate_workspace_columns)
    for table in ("policy_engines", "policies", "policy_versions", "policy_settings",
                  "policy_changes", "policy_decisions"):
        assert f"ALTER TABLE {table} ADD COLUMN workspace_id" in source


# ── compiled-set cache ─────────────────────────────────────────────────────


def _member():
    class _Id:
        username = "river"
        role = "member"
        user_id = None

    return Principal.from_identity(_Id())


EMPLOYEE = "hr-database___get_employee"


def _call(agent, tool=EMPLOYEE):
    """A member's ``call_tool`` on ``tool`` from ``agent`` — the only decision shape left
    (Global parity: invoking an agent is not a policy decision)."""
    return AuthzRequest(_member().ref(), "call_tool", tool_ref(tool),
                        request_context("call_tool", channel="chat", agent_id=agent.id))


def test_compiled_set_is_cached_and_invalidated_on_publish(db, engine, s3, workspace):
    agent = _agent(db)
    eid = policy_store.settings_row(db, WS).id  # the cache is keyed by the (default) engine
    first = policy_cedar.compiled_set(db, WS, "log_only")
    assert first.active_count == 0 and policy_cedar.cached_or_none(eid) is first
    assert policy_cedar.compiled_set(db, WS, "log_only") is first  # same object: cache hit
    # Global parity: zero ACTIVE policies is Cedar's default-deny (knob default `deny`)
    empty = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)
    assert empty.outcome == "DENY" and empty.policy_versions == {}
    assert empty.reason is None and empty.determining == []

    policy = policy_store.create_policy(db, WS, name="baseline", statement=BASELINE,
                                        operator="river")
    assert policy_cedar.cached_or_none(eid) is first  # a draft changes nothing
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    assert policy_cedar.cached_or_none(eid) is None  # invalidated in-process
    second = policy_cedar.compiled_set(db, WS, "log_only")
    assert second is not first and second.active_count == 1
    assert second.version_stamp != first.version_stamp
    allowed = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)
    assert allowed.outcome == "ALLOW"
    assert allowed.determining == [f"{policy.id}:v1#0"]  # BASELINE's member permit
    assert allowed.determining_policy_ids == [f"{policy.id}:v1"]
    assert allowed.policy_ids == [policy.id]
    assert allowed.policy_versions == {policy.id: 1}

    policy_store.retire_policy(db, WS, policy.id, operator="river")
    assert policy_cedar.cached_or_none(WS) is None
    assert policy_cedar.compiled_set(db, WS, "log_only").active_count == 0


def test_compiled_set_restamps_after_the_ttl(db, engine, s3, workspace, monkeypatch):
    policy = policy_store.create_policy(db, WS, name="baseline", statement=BASELINE,
                                        operator="river")
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    current = policy_cedar.compiled_set(db, WS, "log_only")
    # another process retires the policy: no in-process invalidation happens
    policy.status = "retired"
    db.commit()
    assert policy_cedar.compiled_set(db, WS, "log_only") is current  # inside the TTL
    monkeypatch.setattr(policy_cedar, "STAMP_TTL_SECONDS", 0.0)
    rebuilt = policy_cedar.compiled_set(db, WS, "log_only")
    assert rebuilt is not current and rebuilt.active_count == 0
    # a mode change alone is a new stamp too
    assert policy_cedar.compiled_set(db, WS, "enforce").mode == "enforce"


def test_drafts_and_pinned_versions_evaluate_uncached(db, engine, s3, workspace):
    agent = _agent(db)
    policy = policy_store.create_policy(db, WS, name="baseline", statement=BASELINE,
                                        operator="river")
    draft_only = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent,
                                        include_drafts=True)
    assert draft_only.outcome == "ALLOW" and draft_only.source_set == "draft"
    assert draft_only.reason is None  # a draft IS a policy for the simulator: the engine decided
    # the ACTIVE set is still empty → Cedar default-deny (Global parity)
    active_only = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)
    assert active_only.outcome == "DENY" and active_only.reason is None
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    forbid_all = 'forbid(principal, action == Launchpad::Action::"call_tool", resource);'
    policy_store.update_policy(db, WS, policy.id, statement=BASELINE + forbid_all, operator="river")
    assert policy_bundle.evaluate(db, WS, _call(agent), principal=_member(),
                                  agent=agent).outcome == "ALLOW"
    with_draft = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent,
                                        include_drafts=True)
    assert with_draft.outcome == "DENY" and with_draft.determining == [f"{policy.id}:v2#4"]
    pinned = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent,
                                    pinned={policy.id: 2})
    assert pinned.outcome == "DENY" and pinned.source_set == "pinned"


def test_broken_active_version_is_no_decision_not_allow(db, engine, s3, workspace):
    """An ACTIVE statement that stops parsing (wheel/schema change) must fail closed."""
    agent = _agent(db)
    policy = policy_store.create_policy(db, WS, name="baseline", statement=BASELINE,
                                        operator="river")
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    policy_store.get_version(db, policy, 1).statement = BROKEN
    db.commit()
    policy_cedar.invalidate(WS)
    result = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)
    assert result.outcome == "NO_DECISION" and result.decision.errors


def test_the_agent_principal_decides_its_own_tool_calls(db, engine, s3, workspace):
    """Stage A in the simulator: the agent itself as the principal — the per-principal forbid
    of `TOOLS_ADMIN_ONLY` applies to it too, an unmatched target is Cedar's default-deny."""
    agent = _agent(db)
    from tests.policy_fakes import TOOLS_ADMIN_ONLY

    policy = policy_store.create_policy(db, WS, name="tools", statement=TOOLS_ADMIN_ONLY,
                                        operator="river")
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    who = Principal.from_agent(agent)

    def outcome(tool):
        return policy_bundle.evaluate(
            db, WS, AuthzRequest(who.ref(), "call_tool", tool_ref(tool),
                                 request_context("call_tool", channel="runtime",
                                                 agent_id=agent.id)),
            principal=who, agent=agent, tool_ids=(tool,)).outcome

    # the agent principal is neither admin nor member: payout is forbidden, crm unmatched
    assert outcome("hr-database___get_employee") == "ALLOW"
    assert outcome("hr-database___create_payout") == "DENY"
    assert outcome("crm___lookup") == "DENY"


def test_fake_engine_failure_surfaces_not_allows(db, s3, workspace):
    # zero ACTIVE policies is default-deny on the (fake) engine's path — nothing short-circuits
    policy_backend.set_backend(FakeEngine(decide="no_decision"))
    agent = _agent(db)
    result = policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)
    assert result.outcome == "NO_DECISION"
    policy_backend.set_backend(FakeEngine(raises=True))
    policy_cedar.invalidate(WS)
    with pytest.raises(RuntimeError):
        policy_bundle.evaluate(db, WS, _call(agent), principal=_member(), agent=agent)


# ── N17-obs508: a gateway ALLOW a log_only policy would have denied ─────────────────────────


def test_log_only_would_deny_gateway_rows_are_flagged_and_found_by_the_would_deny_filter(
        db, engine, s3, workspace):
    """The interceptor allows under a LOG_ONLY policy and only appends the note
    "log_only policy would deny" to the ALLOW row — the view flags it (`log_only_would`) and the
    decision=would_deny filter returns it beside the real would_deny rows; a plain allow and a
    hub row are untouched."""
    common = dict(channel="chat", principal_kind="user", principal_id="river",
                  action="call_tool", resource_kind="tool",
                  resource_id="hr-database___create_payout", mode="enforce", agent_id="ag1")
    noted = policy_store.record_decision(
        db, WS, source="gateway", decision="allow",
        reason=f"{policy_store.LOG_ONLY_WOULD_NOTE}; engine set ps-3 = bundle ps-3", **common)
    plain = policy_store.record_decision(db, WS, source="gateway", decision="allow",
                                         reason=None, **common)
    observed = policy_store.record_decision(db, WS, source="simulate", decision="would_deny",
                                            reason="simulate:active", **common)
    hub = policy_store.record_decision(db, WS, source="hub", decision="allow",
                                       reason=policy_store.LOG_ONLY_WOULD_NOTE, **common)
    assert policy_store.decision_view(noted)["log_only_would"] is True
    assert policy_store.decision_view(plain)["log_only_would"] is False
    assert "log_only_would" not in policy_store.decision_view(hub)  # gateway rows only
    found = {r.id for r in policy_store.list_decisions(db, WS, decision="would_deny")}
    assert found == {noted.id, observed.id}
    assert {r.id for r in policy_store.list_decisions(db, WS, decision="allow")} == {
        noted.id, plain.id, hub.id}


# ── settings / mode ────────────────────────────────────────────────────────


def test_change_listeners_fire_on_evaluation_relevant_changes(db, engine, s3, workspace):
    events = []
    policy_store.register_change_listener(events.append)
    policy_store.register_change_listener(lambda e: 1 / 0)  # a broken listener never breaks the op
    policy = policy_store.create_policy(db, WS, name="b", statement=BASELINE, operator="river")
    assert events == []
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    policy_store.set_enforcement_mode(db, WS, policy.id, "active", operator="a")
    policy_store.retire_policy(db, WS, policy.id, operator="river")
    policy_store.delete_policy(db, WS, policy.id, operator="river")
    assert [e["event"] for e in events] == ["publish", "enforcement_mode", "retire", "delete"]
    assert events[0] == {"event": "publish", "workspace_id": WS, "engine_id": policy.engine_id,
                         "policy_id": policy.id, "version": 1}


# ── decisions / capabilities ───────────────────────────────────────────────


def test_decision_rows_views_and_pruning(db, engine):
    row = policy_store.record_decision(
        db, WS, source="gateway", channel="v1", principal_kind="api_key", principal_id="key_1",
        principal_label="ci", action="call_tool", resource_kind="tool", resource_id=EMPLOYEE,
        agent_id="ag1", session_id="s1", request_id="r1", decision="deny", mode="enforce",
        policy_ids=["pol_a"], policy_versions={"pol_a": 2}, reason="forbid", latency_ms=0.4)
    view = policy_store.decision_view(row)
    assert view["principal"] == {"kind": "api_key", "id": "key_1", "label": "ci"}
    assert view["resource"] == {"kind": "tool", "id": EMPLOYEE}
    assert view["policy_versions"] == {"pol_a": 2}
    assert policy_store.list_decisions(db, WS, principal="ci")[0].id == row.id
    assert policy_store.list_decisions(db, WS, decision="allow") == []
    assert policy_store.list_decisions(db, "other") == []
    old = db.get(PolicyDecision, row.id)
    old.created_at = datetime.now(UTC) - timedelta(days=40)
    db.commit()
    assert policy_store.prune_decisions(db) == 1
    assert policy_store.prune_decisions(db, keep_days=0) == 0


def test_capability_summary_and_overview_semantics(db, engine, s3, workspace):
    from app.models.policy import PolicyEngineAttachment

    assert policy_store.capability_summary(db, WS) == {"engines": 0, "attached_gateways": 0}
    status = policy_store.overview_status(db, WS, engine_registered=True)
    assert status["enabled"] is False and status["detail"] == "0 engine(s) · 0 gateway(s)"
    policy = policy_store.create_policy(db, WS, name="b", statement=BASELINE, operator="river")
    policy_store.publish_policy(db, workspace, policy.id, operator="river")
    # one (default) engine, ACTIVE policy, no gateway attached: nothing is guarded yet
    assert policy_store.overview_status(db, WS, engine_registered=True) == {
        "enabled": False, "detail": "1 engine(s) · 0 gateway(s)", "engines": 1,
        "attached_gateways": 0}
    db.add(PolicyEngineAttachment(gateway_id="gw-1", engine_id=policy.engine_id, mode="ENFORCE"))
    db.commit()
    assert policy_store.overview_status(db, WS, engine_registered=True) == {
        "enabled": True, "detail": "1 engine(s) · 1 gateway(s)", "engines": 1,
        "attached_gateways": 1}
    assert policy_store.overview_status(db, WS, engine_registered=False)["enabled"] is False
    assert policy_store.capability_summary(db, WS) == {"engines": 1, "attached_gateways": 1}
