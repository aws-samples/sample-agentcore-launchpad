"""Stream A3b — the principal carrier in a self-built Harness bundle (contract I2, T10):
off, the bundle is byte-identical; on, ``launchpad_principal_carrier.py`` ships beside it,
``invoke`` captures the token first, and the SigV4 Gateway transport carries it."""

from __future__ import annotations

import ast

from app.deployer import harness_local
from app.services import harness_convert as hc
from tests.conftest import ws_ctx
from tests.policy_principal.test_principal_render import HARNESS_SPEC, RES

WS = ws_ctx(RES, region="cn-northwest-1")


def test_carrier_off_is_byte_identical():
    off = harness_local.render_harness_bundle(HARNESS_SPEC, WS)
    explicit = harness_local.render_harness_bundle(HARNESS_SPEC, WS, principal_carrier=False)
    assert off.files == explicit.files and off.requirements == explicit.requirements
    assert f"{hc.PRINCIPAL_MODULE_NAME}.py" not in off.files
    for text in off.files.values():
        assert "launchpad-principal" not in text and "launchpad_principal" not in text


def test_carrier_on_ships_the_module_and_grafts_capture_and_transport():
    on = harness_local.render_harness_bundle(HARNESS_SPEC, WS, principal_carrier=True)
    module = on.files[f"{hc.PRINCIPAL_MODULE_NAME}.py"]
    compile(module, "carrier.py", "exec")
    assert module == __import__("app.templates.principal_support", fromlist=["x"]) \
        .render_carrier_source()
    main = on.files["main.py"]
    assert main.count(hc.PRINCIPAL_IMPORT_MARK) == 1 and main.count(hc.PRINCIPAL_CAPTURE_MARK) == 1
    fns = {fn.name: fn for fn in ast.parse(main).body if isinstance(fn, ast.AsyncFunctionDef)}
    invoke, unscoped = fns["invoke"], fns[hc.PRINCIPAL_UNSCOPED_NAME]
    assert [ast.unparse(d) for d in invoke.decorator_list] == ["app.entrypoint"]
    assert unscoped.decorator_list == []  # the one entrypoint is the turn-scoped wrapper
    assert ast.unparse(invoke.body[0]) == (
        "async for event in launchpad_principal_turn(payload, "
        f"{hc.PRINCIPAL_UNSCOPED_NAME}(payload, context)):\n    yield event")
    assert "tool_executor=SequentialToolExecutor()" in main  # T10 (the vendored template)
    client = on.files["mcp_client/client.py"]
    assert hc.PRINCIPAL_CLIENT_IMPORT in client
    assert "request = _launchpad_with_principal(request)" in client
    assert "launchpad_principal" in on.notes["principal"]


def test_the_grafts_are_idempotent():
    on = harness_local.render_harness_bundle(HARNESS_SPEC, WS, principal_carrier=True)
    assert hc.graft_principal_carrier(on.files["main.py"]) == on.files["main.py"]


def _unscope(main: str) -> str:
    """The principal graft undone: no import, no wrapper, the generated `invoke` back."""
    main = "".join(line for line in main.splitlines(keepends=True)
                   if hc.PRINCIPAL_IMPORT_MARK not in line)
    start = main.index("\n\n@app.entrypoint  # <launchpad-principal-turn:v1>\n")
    end = main.index("        yield event\n", start) + len("        yield event\n")
    main = main[:start] + main[end:]
    return main.replace(f"async def {hc.PRINCIPAL_UNSCOPED_NAME}(",
                        "@app.entrypoint\nasync def invoke(", 1)


def test_on_minus_the_grafts_is_off():
    off = harness_local.render_harness_bundle(HARNESS_SPEC, WS)
    on = harness_local.render_harness_bundle(HARNESS_SPEC, WS, principal_carrier=True)
    assert _unscope(on.files["main.py"]) == off.files["main.py"]
    assert {k for k in on.files if k != f"{hc.PRINCIPAL_MODULE_NAME}.py"} == set(off.files)
