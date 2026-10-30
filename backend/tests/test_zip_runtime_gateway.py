"""Generated zip-runtime agents reach the shared Gateway over SigV4, signed with
the runtime's own execution role — not an AgentCore Identity OAuth M2M exchange.

N1 flipped the shared ``launchpad-gw``'s authorizerType to ``AWS_IAM``, which has
never accepted an OAuth bearer, so the old token-exchange plumbing in
``gateway_tools.py.tmpl`` was dead weight that only ever failed. This module pins
the replacement contract: the rendered client source carries the shared SigV4
transport and nothing from the old M2M flow, and ``runtime_user_id`` no longer
fires for a bare gateway ToolRef (only a tool-level ``auth`` block still needs the
Runtime-injected workload token).
"""

from app.schemas.agent import AgentSpec, ToolRef
from app.templates.gateway_support import render_gateway_source, runtime_user_id


def _spec_with_gateway() -> AgentSpec:
    return AgentSpec(
        name="gw-agent",
        method="zip_runtime",
        model_id="global.zai.glm-5.3",
        system_prompt="x",
        tools=[ToolRef(
            type="gateway", name="launchpad_gw",
            config={"gateway_id": "launchpad-gw-abc1234567"},
        )],
    )


def test_rendered_gateway_source_has_no_oauth_token_exchange():
    source = render_gateway_source(_spec_with_gateway())
    assert "GetResourceOauth2Token" not in source
    assert "oauth2Flow" not in source
    assert "gateway_bearer_token" not in source
    assert "_launchpad_sigv4_streamablehttp" in source
    assert "SigV4Auth" in source


def test_rendered_gateway_source_has_no_runtime_user_id_dependency():
    source = render_gateway_source(_spec_with_gateway())
    assert "WORKLOAD_NAME" not in source
    assert "LAUNCHPAD_WORKLOAD_NAME" not in source



def test_runtime_user_id_is_still_requested_for_a_tool_level_auth_block():
    spec = AgentSpec(
        name="auth-agent", method="zip_runtime", model_id="global.zai.glm-5.3",
        system_prompt="x",
        tools=[ToolRef(
            type="rest", name="t", config={"url": "https://x"},
            auth={"connection": "c", "kind": "oauth2", "mode": "as_agent"},
        )],
    )
    assert runtime_user_id(spec.model_dump(), "alice") == "alice"
