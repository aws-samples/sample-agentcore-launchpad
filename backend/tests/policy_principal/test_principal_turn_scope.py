"""Stream A3 review fix D1 (runtime side): the principal token lives for ONE turn — set when
the turn starts, cleared in a ``finally`` when it ends; a token-less turn clears a previous
one; an ending turn never clears another turn's token. And the policy-session header rides
only on tools/call and tools/list (contract I2)."""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import types

import httpx
import pytest

from app.templates import principal_support as ps
from app.templates.strands_agent import render_main_py


def _carrier() -> types.SimpleNamespace:
    ns: dict = {}
    exec(compile(ps.render_carrier_source(), "carrier.py", "exec"), ns)  # noqa: S102
    return types.SimpleNamespace(**{k: v for k, v in ns.items() if not k.startswith("__")})


def _token(psid: str = "psid-1") -> str:
    def seg(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{seg({'alg': 'ES256'})}.{seg({'psid': psid})}.sig"


def _payload(token: str | None) -> dict:
    return {"prompt": "hi", **({"launchpad": {"principal": token}} if token else {})}


def _current(c) -> str | None:
    return c._launchpad_principal_current()[0]


def test_the_token_lives_for_the_turn_only():
    c = _carrier()
    seen: list[str | None] = []

    async def events():
        seen.append(_current(c))  # inside the turn: a tool call would carry it
        yield {"event": "text"}
        seen.append(_current(c))

    async def run():
        return [e async for e in c.launchpad_principal_turn(_payload(_token()), events())]

    assert asyncio.run(run()) == [{"event": "text"}]
    assert seen == [_token(), _token()]
    assert _current(c) is None  # cleared when the turn ended


def test_a_failing_or_abandoned_turn_clears_the_token_too():
    c = _carrier()

    async def boom():
        yield {"event": "text"}
        raise RuntimeError("model failed")

    async def failing():
        async for _ in c.launchpad_principal_turn(_payload(_token()), boom()):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(failing())
    assert _current(c) is None

    async def endless():
        while True:
            yield {"event": "heartbeat"}

    async def abandoned():
        turn = c.launchpad_principal_turn(_payload(_token("p2")), endless())
        await turn.__anext__()
        assert _current(c) == _token("p2")
        await turn.aclose()  # the heartbeat wrapper's finally closes the iterator

    asyncio.run(abandoned())
    assert _current(c) is None


def test_a_token_less_turn_clears_the_previous_token():
    c = _carrier()
    c.launchpad_set_principal(_payload(_token()))  # a turn that never cleaned up
    seen: list[str | None] = []

    async def events():
        seen.append(_current(c))
        yield {"event": "text"}

    async def run():
        return [e async for e in c.launchpad_principal_turn(_payload(None), events())]

    asyncio.run(run())
    assert seen == [None]


def test_an_ending_turn_never_clears_a_later_turns_token():
    c = _carrier()
    first = c.launchpad_set_principal(_payload(_token("a")))
    second = c.launchpad_set_principal(_payload(_token("b")))
    c.launchpad_clear_principal(first)  # turn A ends after turn B started
    assert _current(c) == second
    c.launchpad_clear_principal(second)
    assert _current(c) is None


def test_the_scope_context_manager_for_byoc():
    c = _carrier()
    with c.launchpad_principal_scope(_payload(_token())) as token:
        assert token == _token() and _current(c) == token
    assert _current(c) is None
    with pytest.raises(ValueError), c.launchpad_principal_scope(_payload(_token())):
        raise ValueError("handler failed")
    assert _current(c) is None


def _request(method: str) -> httpx.Request:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}).encode()
    return httpx.Request("POST", "https://gw.example/mcp", content=body,
                         headers={"content-type": "application/json"})


@pytest.mark.parametrize("method", ["initialize", "notifications/initialized", "ping",
                                    "resources/list"])
def test_other_requests_carry_neither_the_token_nor_the_session_header(method):
    c = _carrier()
    with c.launchpad_principal_scope(_payload(_token())):
        request = _request(method)
        assert c._launchpad_with_principal(request) is request


@pytest.mark.parametrize("method", ["tools/call", "tools/list"])
def test_tool_requests_carry_the_token_and_the_session_header(method):
    c = _carrier()
    with c.launchpad_principal_scope(_payload(_token("psid-9"))):
        out = c._launchpad_with_principal(_request(method))
    assert out.headers[c.LAUNCHPAD_POLICY_SESSION_HEADER] == "psid-9"
    meta = json.loads(out.content)["params"]["_meta"]
    assert meta[c.LAUNCHPAD_PRINCIPAL_META_KEY] == _token("psid-9")
    assert int(out.headers["content-length"]) == len(out.content)


def test_the_strands_entrypoint_wraps_the_turn_it_streams():
    """The capture line sits between building the turn and streaming it — the token is set
    while the turn's events (the tool calls among them) run, and the early `return` paths
    never set one."""
    from app.schemas.agent import AgentSpec
    from tests.test_policy_deploy_narrowing import _gateway_spec

    code = render_main_py(AgentSpec(**_gateway_spec()), principal_carrier=True)
    invoke = next(fn for fn in ast.parse(code).body
                  if isinstance(fn, ast.AsyncFunctionDef) and fn.name == "invoke")
    stmts = [ast.unparse(s) for s in invoke.body]
    i = stmts.index("turn = _turn_events(payload, prompt, session_id)")
    assert stmts[i + 1] == "turn = launchpad_principal_turn(payload, turn)"
    assert stmts[i + 2].startswith("async for event in _events_with_heartbeat(turn")
    assert "launchpad_set_principal(payload)" not in "\n".join(stmts)
