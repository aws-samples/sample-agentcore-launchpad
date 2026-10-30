# ruff: noqa: F811 — the make_world / routed fixtures are imported for their side effects
"""N17-obs488 (SECURITY / FAIL-OPEN, found live in M2 step 9u, 2026-10-27): the hub wrote the
gateway bundle's ``governed_actions`` / ``temporal_referenced_actions`` as the compiler's BARE
tool names (``hr-database___get_employee``) while the interceptor compares the call's
``action_entity(name)`` (``AgentCore::Action::"hr-database___get_employee"``) by string
(``bundle.any_enforcing``, ``core.handle`` step 2) — nothing was governed, every call passed
before the token check. Contract amendment 6: the bundle carries the entity form; this file is
the round trip the review asked for — a REAL compile on a live local engine → ``engine_state``
→ the composed bundle → the Lambda's own parser and request path."""
from __future__ import annotations

import pytest

from app.policy.interceptor import bundle as interceptor_bundle
from app.policy.interceptor import core
from app.services import (
    policy_bundle,
    policy_engine_sync,
    policy_engines,
    policy_gateway_publish,
    policy_store,
)
from tests.policy_principal.engine_harness import ENGINE
from tests.policy_principal.test_engine_parity import make_world  # noqa: F401 — fixture
from tests.policy_principal.test_engine_routing import routed  # noqa: F401 — fixture
from tests.policy_principal.test_interceptor import (
    GW,
    PAYOUT,
    ROLE_ARN,
    World,
    call_event,
    error_of,
)

WS = "default"
ENTITY_PREFIX = 'AgentCore::Action::"'
GATEWAY_ARN = f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:gateway/{GW}"


def test_gateway_actions_is_the_interceptor_entity_form_and_refuses_a_double_conversion():
    assert policy_engine_sync.gateway_actions(["hr-database___create_payout", "a-b___c"]) == [
        core.action_entity("a-b___c"), core.action_entity("hr-database___create_payout")]
    assert policy_engine_sync.gateway_actions([]) == []
    # the guard: an already-formatted id (or an empty name) can never be converted again
    with pytest.raises(ValueError):
        policy_engine_sync.gateway_actions([core.action_entity("hr-database___get_employee")])
    with pytest.raises(ValueError):
        policy_engine_sync.gateway_actions([""])


@pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")
def test_hub_bundle_governs_the_compiled_tools_in_the_interceptor(routed, signer, hmac_key):
    """compile (live engine) → engine_state → bundle → Lambda parser: an enforcing workspace
    governs ``create_payout`` for an unknown caller, and a known caller without a token is
    refused at the token check (never ``pass_request`` because the action looked ungoverned)."""
    db, agent = routed
    from app.models.policy import PolicyEngineAttachment

    engine_row = policy_store.settings_row(db, WS)  # the workspace's default engine
    db.add(PolicyEngineAttachment(gateway_id=GW, engine_id=engine_row.id, mode="ENFORCE"))
    db.commit()

    state = engine_row.engine_state
    assert state["governed_actions"], "the compile must have governed something"

    # the bundle exactly as policy_gateway_publish.compose builds it (minus KMS: the test signer)
    attachment = policy_gateway_publish._attachment_state(
        db, policy_engines.attachment_of(db, GW))
    assert attachment is not None and attachment.engine_id == engine_row.id
    role = core.role_name(ROLE_ARN)
    bundle = policy_bundle.build_gateway_bundle(
        gateway_id=GW, gateway_arn=GATEWAY_ARN, version=8, env="", hub_role="launchpad-ec2-role",
        pubkeys={signer.kid: signer.public_spki_b64},
        role_map={role: {"agent_id": agent.id, "ws": WS, "method": agent.method or "zip_runtime",
                         "system": False, "byoc": False, "exempt": False}},
        attachment=attachment, endpoints=["10.0.1.10:7443", "10.0.1.10:7444"], shards=2,
    )
    parsed = interceptor_bundle.parse_bundle(bundle, expect_gateway_id=GW)
    assert parsed.any_enforcing(core.action_entity(PAYOUT)) == (True, False)
    assert parsed.any_enforcing(core.action_entity("hr-database___get_employee")) == (True, False)
    assert parsed.any_enforcing(core.action_entity("not-a___tool")) == (True, False)
    ws_block = parsed.attachment
    # the routed world installs the two-half parity baseline (agents + users) + PERMIT_HR
    assert ws_block is not None and ws_block.active_count == 3 and ws_block.mode == "enforce"

    # the Lambda's own request path over the hub-composed bundle: a KNOWN caller without a
    # token reaches the token check (principal_missing) instead of passing the call through
    world = World(signer, hmac_key)
    world.publish(bundle)
    world.source.refresh(force=True)
    out = world.run(call_event(None, tool=PAYOUT, arn=ROLE_ARN))
    error = error_of(out)
    assert error is not None, "the call passed the interceptor although its action is governed"
    assert error["data"]["reason_code"] == "principal_missing"
    assert "transformedGatewayRequest" not in out["mcp"]
    # and an UNKNOWN caller (no role_map entry) is judged by any_enforcing → unknown_caller deny
    out = world.run(call_event(None, tool=PAYOUT,
                               arn="arn:aws-cn:iam::123456789012:user/example-operator"))
    error = error_of(out)
    assert error is not None and error["data"]["reason_code"] == "unknown_caller"

    # the format guard on the hub's writer itself (what the review asked for): every item the
    # hub stores is an entity id, never a bare name — a regression fails here even before a
    # bundle is composed
    for key in ("governed_actions", "temporal_referenced_actions"):
        bad = [a for a in state[key] if not a.startswith(ENTITY_PREFIX)]
        assert not bad, f"engine_state.{key} must carry entity-formatted action ids: {bad}"
    assert core.action_entity(PAYOUT) in state["governed_actions"]
