"""Stream A3 (part 1) — the principal carrier rendered into the Strands template (contract I2,
design T10, §14.2 A3): off, the entrypoint and the SigV4 transport are byte-identical; on, the
turn's token rides in ``params._meta["launchpad/principal"]`` of every Gateway tools/call and
tools/list the transport signs, with Global's policy-session header, and tools run in order."""

from __future__ import annotations

import base64
import json
import re

import httpx
import pytest
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from app.schemas.agent import AgentSpec
from app.templates import gateway_support
from app.templates import principal_support as ps
from app.templates import strands_agent as strands_pkg
from app.templates.strands_agent import render_main_py

# the specs the retired guard-render module used to provide (test_policy_bundle_render.py is gone
# with the runtime guard — Global parity 2026-10-27)
GATEWAY_TOOL = {"type": "gateway", "name": "hr-database",
                "config": {"gateway_id": "gw-1", "tools": ["hr-database___get_employee"]}}
GW_SPEC = AgentSpec(name="guard-agent", method="zip_runtime", system_prompt="p",
                    tools=[GATEWAY_TOOL])
PLAIN_SPEC = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="p")
HARNESS_SPEC = AgentSpec(name="hr-harness", method="harness", system_prompt="HR questions.",
                         tools=[GATEWAY_TOOL])
RES = {"artifacts_bucket": "launchpad-artifacts-test", "memory_id": "mem-1",
       "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
       "gateway_url": "https://gw.example.test/mcp", "gateway_id": "gw-1"}

META = "launchpad/principal"
HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
PSID = "7ac775f9a1a629c68602a56592539e9ff041037cc3bbfaa3aa3d6fc6143b2414-v3-g0"


def _token(psid: str = PSID) -> str:
    def seg(obj) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{seg({'alg': 'ES256'})}.{seg({'psid': psid, 'v': 1})}.c2ln"


def _strip_principal_seams(text: str) -> str:
    return re.sub(r"^[ \t]*__LAUNCHPAD_PRINCIPAL_(SOURCE|CAPTURE|EXECUTOR|INJECT)__[ \t]*\n", "",
                  text, flags=re.MULTILINE)


# ── off: byte identity ─────────────────────────────────────────────────────


@pytest.mark.parametrize("spec", [GW_SPEC, PLAIN_SPEC], ids=["gateway", "plain"])
def test_carrier_off_is_byte_identical(spec, tmp_path, monkeypatch):
    rendered = render_main_py(spec)
    assert rendered == render_main_py(spec, principal_carrier=False)
    assert "__LAUNCHPAD_PRINCIPAL" not in rendered and "launchpad-principal" not in rendered
    assert "SequentialToolExecutor" not in rendered
    template = (strands_pkg.TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    assert template.count("__LAUNCHPAD_PRINCIPAL_") == 3
    (tmp_path / "main.py.tmpl").write_text(_strip_principal_seams(template), encoding="utf-8")
    monkeypatch.setattr(strands_pkg, "TEMPLATE_DIR", tmp_path)
    monkeypatch.setattr(strands_pkg, "render_principal_placeholders", lambda code, enabled: code)
    transport = gateway_support.SIGV4_TRANSPORT_TEMPLATE.read_text(encoding="utf-8")
    monkeypatch.setattr(gateway_support, "render_sigv4_transport",
                        lambda principal_carrier=False: _strip_principal_seams(transport))
    assert render_main_py(spec) == rendered


def test_the_transport_off_is_the_template_without_the_seam():
    raw = gateway_support.SIGV4_TRANSPORT_TEMPLATE.read_text(encoding="utf-8")
    assert raw.count(ps.PH_INJECT) == 1
    assert gateway_support.render_sigv4_transport() == _strip_principal_seams(raw)


# ── on: what is rendered ───────────────────────────────────────────────────


def test_carrier_on_renders_every_part_once_and_compiles():
    code = render_main_py(GW_SPEC, principal_carrier=True)
    compile(code, "main.py", "exec")
    assert code.count(ps.CARRIER_START) == 1 and code.count(ps.CARRIER_END) == 1
    assert "__LAUNCHPAD_PRINCIPAL" not in code
    # capture right after the bundle pointer of the same payload
    capture = code.index("launchpad_set_principal(payload)")
    assert code.rindex("_launchpad_capture_bundle_ref(payload)", 0, capture) < capture
    # T10: the executor is set in the agent builder, before the Agent is built
    executor = code.index('kwargs["tool_executor"] = SequentialToolExecutor()')
    assert executor < code.index("return Agent(**kwargs)", executor)
    # the transport signs the carried request
    inject = code.index("request = _launchpad_with_principal(request)")
    assert inject < code.index("SigV4Auth(credentials", inject)


def test_carrier_on_without_gateway_tools_still_compiles():
    compile(render_main_py(PLAIN_SPEC, principal_carrier=True), "main.py", "exec")


def test_the_carrier_never_logs():
    source = ps.render_carrier_source()
    assert "print(" not in source and "logging" not in source and "logger" not in source


# ── on: what the transport sends ───────────────────────────────────────────


@pytest.fixture
def carrier() -> dict:
    scope: dict = {"httpx": httpx}
    exec(ps.render_carrier_source(), scope)  # noqa: S102
    return scope


def _call(method: str, params=None, id_=1) -> dict:
    msg = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def test_tools_call_and_list_carry_the_token(carrier):
    token = _token()
    carrier["launchpad_set_principal"]({"prompt": "hi", "launchpad": {"principal": token}})
    body = carrier["launchpad_principal_body"]
    call = json.loads(body(json.dumps(_call("tools/call", {"name": "hr___x", "arguments": {}}))
                           .encode()))
    assert call["params"]["_meta"] == {META: token}
    listed = json.loads(body(json.dumps(_call("tools/list")).encode()))
    assert listed["params"]["_meta"] == {META: token}
    kept = {"_meta": {"progressToken": 7}, "name": "a"}
    merged = json.loads(body(json.dumps(_call("tools/call", kept)).encode()))
    assert merged["params"]["_meta"] == {"progressToken": 7, META: token}


def test_other_methods_batches_and_garbage(carrier):
    token = _token()
    carrier["launchpad_set_principal"]({"launchpad": {"principal": token}})
    body = carrier["launchpad_principal_body"]
    init = json.dumps(_call("initialize", {"capabilities": {}})).encode()
    assert body(init) is init  # untouched, same object
    batch = json.loads(body(json.dumps([_call("notifications/initialized"),
                                        _call("tools/call", {"name": "t"}, 2)]).encode()))
    assert "_meta" not in (batch[0].get("params") or {})
    assert batch[1]["params"]["_meta"] == {META: token}
    assert body(b"not json") == b"not json"


def test_no_token_means_no_change(carrier):
    carrier["launchpad_set_principal"]({"prompt": "hi"})
    body = json.dumps(_call("tools/call", {"name": "t"})).encode()
    assert carrier["launchpad_principal_body"](body) is body
    request = httpx.Request("POST", "https://gw.example/mcp", content=body)
    assert carrier["_launchpad_with_principal"](request) is request


def test_a_new_turn_replaces_the_token(carrier):
    carrier["launchpad_set_principal"]({"launchpad": {"principal": _token()}})
    carrier["launchpad_set_principal"]({"launchpad": {}})
    body = json.dumps(_call("tools/list")).encode()
    assert carrier["launchpad_principal_body"](body) is body


def test_the_signed_request_carries_meta_and_the_session_header(carrier):
    """The transport's auth flow (on) signs the REWRITTEN body."""
    class Frozen:
        access_key, secret_key, token = "AKIDEXAMPLE", "secret", "session-token"

    class Session:
        def get_credentials(self):
            return type("C", (), {"get_frozen_credentials": lambda self: Frozen()})()

    seen: dict = {}
    scope = dict(carrier)
    scope.update({
        "boto3": type("B", (), {"Session": Session}), "SigV4Auth": SigV4Auth,
        "AWSRequest": AWSRequest,
        "streamablehttp_client": lambda url, auth: seen.update(url=url, auth=auth),
    })
    exec(gateway_support.render_sigv4_transport(principal_carrier=True), scope)  # noqa: S102
    token = _token()
    scope["launchpad_set_principal"]({"launchpad": {"principal": token}})
    scope["_launchpad_sigv4_streamablehttp"]("https://gw.example/mcp", "cn-northwest-1")
    request = httpx.Request("POST", "https://gw.example/mcp",
                            json=_call("tools/call", {"name": "hr___x", "arguments": {"a": 1}}))
    signed = next(seen["auth"].auth_flow(request))
    sent = json.loads(signed.content)
    assert sent["params"]["_meta"] == {META: token}
    assert signed.headers[HEADER] == PSID
    assert signed.headers["content-length"] == str(len(signed.content))
    assert signed.headers["authorization"].startswith("AWS4-HMAC-SHA256")
    assert "x-amz-security-token" in signed.headers



def test_the_rendered_agent_runs_its_tools_in_order(tmp_path, monkeypatch):
    """T10: the carrier-on entrypoint builds the Agent with a SequentialToolExecutor."""
    import importlib.util
    import types

    import strands
    from strands.tools.executors import SequentialToolExecutor

    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    target = tmp_path / "carrier_main.py"
    target.write_text(render_main_py(GW_SPEC, principal_carrier=True), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("carrier_main", target)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "build_model", lambda: None)  # no workload token here
    agent = module.build_agent("hr-intern", "s" * 40)
    assert isinstance(agent.tool_executor, SequentialToolExecutor)
    module.launchpad_set_principal({"launchpad": {"principal": _token()}})
    body = json.dumps(_call("tools/list")).encode()
    assert json.loads(module.launchpad_principal_body(body))["params"]["_meta"][META]


def test_byoc_helper_mirrors_the_carrier_byte_for_byte():
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    helper = root / "samples" / "byoc" / "launchpad_policy_principal.py"
    assert helper.read_bytes() == ps.CARRIER_TEMPLATE.read_bytes()


def test_the_carrier_is_self_contained_at_module_scope():
    """Stdlib imports, constants and definitions only — importable anywhere (BYOC)."""
    import ast

    for node in ast.parse(ps.render_carrier_source()).body:
        assert isinstance(node, (ast.Import, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.Assign)), ast.dump(node)[:80]
        if isinstance(node, ast.Import):
            assert {a.name for a in node.names} <= {"base64", "contextlib", "json", "threading"}


def test_the_eval_worker_release_is_unchanged_by_the_carrier_placeholders(monkeypatch):
    """The worker inlines the FLAG-OFF Strands model block: the carrier placeholders are no
    new release, so a flag-off deploy reuses the release already running. Pinned two ways:
    the model block hash is the template's after Global parity removed the three runtime-guard
    placeholder lines (2026-10-27 — ONE deliberate release change: the next worker deploy
    rolls a new release; before that it was e0c111a3's 69970b4d…; 2026-10-29 N17-obs527
    tightened the credential-redaction regex in the model block — the second deliberate
    change, a9710dee… before), and the release tag equals
    the one computed from the template with its placeholder LINES simply deleted — whatever
    else the worker library carries."""
    import hashlib

    from app.templates import eval_worker

    rel = eval_worker.release()
    assert rel["files"]["model_support"] == (
        "e901721f5460d7cfb85dc51096c7ae145cf3118afd7b0403c20f1357eb2e332c")
    raw = (eval_worker.APP_DIR / "templates" / "strands_agent" / "main.py.tmpl").read_text(
        encoding="utf-8")
    stripped = "".join(line for line in raw.splitlines(keepends=True)
                       if not any(ph in line for ph in ps.MAIN_PLACEHOLDERS))
    monkeypatch.setattr(eval_worker, "_strands_template_off", lambda: stripped)
    assert eval_worker.release_tag(eval_worker.release()) == eval_worker.release_tag(rel)
    assert hashlib.sha256(stripped.encode()).hexdigest() == rel["files"]["model_support"]
