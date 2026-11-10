"""Cedar schema and baseline policy templates of the self-hosted Policy module (D20).

``schema.cedarschema.json`` is the ``Launchpad`` namespace every statement is validated
against on save (plan §2.1); the ``templates/`` directory holds the editor's prefill
statements (Global's ``allow_gateway_tools.cedar`` analogues). Both are plain data files
read once per process; ``schema_digest()`` stamps every stored version so a later
schema change is visible on the row.
"""

import hashlib
import json
from functools import cache
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SCHEMA_PATH = _HERE / "schema.cedarschema.json"
TEMPLATES_DIR = _HERE / "templates"
NAMESPACE = "Launchpad"

# Context attributes the schema declares per action. A request context is filtered
# to these before evaluation: an undeclared attribute turns a decision into
# ``NoDecision`` (fail-closed) rather than being ignored, so the hub strips what the
# schema cannot type (``input`` — see docs/cn/interfaces/policy-backend.md §"Schema").
# Global parity (contract amendment 9, 2026-10-27): policies decide Gateway tool calls only —
# the `invoke` action is gone from the vocabulary (a statement naming it does not validate).
CONTEXT_ATTRIBUTES: dict[str, tuple[str, ...]] = {
    "call_tool": ("channel", "agent_id"),
    "list_tool": ("agent_id",),
}
ACTIONS = tuple(CONTEXT_ATTRIBUTES)


@cache
def schema_json() -> str:
    """The schema as the compact JSON text the engine parses."""
    return json.dumps(json.loads(SCHEMA_PATH.read_text(encoding="utf-8")), sort_keys=True)


@cache
def schema_digest() -> str:
    return hashlib.sha256(schema_json().encode("utf-8")).hexdigest()


def entity_type(short: str) -> str:
    """``User`` → ``Launchpad::User``."""
    return f"{NAMESPACE}::{short}"


def action_uid(action: str) -> dict[str, str]:
    return {"type": f"{NAMESPACE}::Action", "id": action}


@cache
def list_templates() -> tuple[dict[str, str], ...]:
    """``({name, statement}, …)`` for the editor prefill, sorted by name."""
    out = []
    for path in sorted(TEMPLATES_DIR.glob("*.cedar")):
        out.append({"name": path.stem, "statement": path.read_text(encoding="utf-8")})
    return tuple(out)


def template(name: str) -> str | None:
    for row in list_templates():
        if row["name"] == name:
            return row["statement"]
    return None
