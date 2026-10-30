"""Rendering seam of the per-user principal carrier (stream A3; contract I2 in
docs/cn/interfaces/policy-principal-contracts.md, design §13.1 T10, §14.2 A3).

ONE module source, ``principal_carrier.py.tmpl``, plus four one-line splice placeholders:

- ``__LAUNCHPAD_PRINCIPAL_SOURCE__`` (``strands_agent/main.py.tmpl``): the carrier module;
- ``__LAUNCHPAD_PRINCIPAL_CAPTURE__`` (the entrypoint): the turn's events wrapped in
  ``launchpad_principal_turn(payload, …)`` — the token is set when the turn starts and cleared
  in its ``finally`` (turn-scoped);
- ``__LAUNCHPAD_PRINCIPAL_EXECUTOR__`` (the agent builder): ``SequentialToolExecutor`` for an
  agent with governed tools (T10 — the engine's per-session history needs calls in order);
- ``__LAUNCHPAD_PRINCIPAL_INJECT__`` (``sigv4_gateway_transport.py.tmpl``'s auth flow): the
  request it signs carries the token in ``params._meta`` and the policy-session header.

Everything renders ONLY when the deployer says the carrier is on
(``policy_enforcement.principal_carrier_for``); off, every placeholder line is removed so the
output is byte-identical to before (``test_principal_render.py``).
"""

from __future__ import annotations

import re
from pathlib import Path

CARRIER_TEMPLATE = Path(__file__).with_name("principal_carrier.py.tmpl")
CARRIER_START = "# <launchpad-principal-carrier:v1>"
CARRIER_END = "# </launchpad-principal-carrier:v1>"

PH_SOURCE = "__LAUNCHPAD_PRINCIPAL_SOURCE__"
PH_CAPTURE = "__LAUNCHPAD_PRINCIPAL_CAPTURE__"
PH_EXECUTOR = "__LAUNCHPAD_PRINCIPAL_EXECUTOR__"
PH_INJECT = "__LAUNCHPAD_PRINCIPAL_INJECT__"
MAIN_PLACEHOLDERS = (PH_SOURCE, PH_CAPTURE, PH_EXECUTOR)

CAPTURE_SNIPPET = (
    "turn = launchpad_principal_turn(payload, turn)  # <launchpad-principal-capture:v1>")
EXECUTOR_SNIPPET = (
    "# <launchpad-principal-executor:v1>  # --- CN T10: governed tools run in order ---\n"
    "from strands.tools.executors import SequentialToolExecutor\n"
    'kwargs["tool_executor"] = SequentialToolExecutor()\n'
    "# </launchpad-principal-executor:v1>"
)
INJECT_SNIPPET = "request = _launchpad_with_principal(request)  # <launchpad-principal-inject:v1>"



def render_optional_line(code: str, placeholder: str, source: str) -> str:
    """Replace the line that holds ``placeholder`` with ``source`` (each line re-indented to
    the placeholder's column) — or remove the whole line, newline included, when ``source``
    is empty. The placeholder must stand alone on its line, exactly once."""
    pattern = re.compile(rf"^([ \t]*){re.escape(placeholder)}[ \t]*\n", re.MULTILINE)
    matches = list(pattern.finditer(code))
    if len(matches) != 1:
        raise ValueError(f"{placeholder} must appear exactly once on its own line "
                         f"(found {len(matches)})")
    match = matches[0]
    if not source:
        return code[: match.start()] + code[match.end():]
    indent = match.group(1)
    body = "\n".join((indent + line) if line else "" for line in source.rstrip("\n").split("\n"))
    return code[: match.start()] + body + "\n" + code[match.end():]

def render_carrier_source() -> str:
    """The carrier module source: the same text for every spec."""
    return CARRIER_TEMPLATE.read_text(encoding="utf-8")


def render_principal_placeholders(code: str, *, enabled: bool) -> str:
    """The three placeholders of the Strands entrypoint in one pass."""
    code = render_optional_line(code, PH_SOURCE,
                                render_carrier_source().rstrip("\n") if enabled else "")
    code = render_optional_line(code, PH_CAPTURE, CAPTURE_SNIPPET if enabled else "")
    return render_optional_line(code, PH_EXECUTOR, EXECUTOR_SNIPPET if enabled else "")


def render_transport(transport: str, *, enabled: bool) -> str:
    """The SigV4 transport block with (or without) the principal injection line."""
    return render_optional_line(transport, PH_INJECT, INJECT_SNIPPET if enabled else "")
