"""Legacy I4 wire fixtures remain readable, but Global rejects invoke policies at compile."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest

from app.policy import contracts
from app.policy.contracts import codec
from tests.policy_principal.conftest import load_fixture
from tests.policy_principal.engine_harness import ASSETS, ENGINE

SPEC = load_fixture("engine_invoke.json")


def _patched(case: dict) -> dict:
    item = copy.deepcopy(SPEC["request"])
    for path, value in case["patch"].items():
        node, *rest = path.split(".")
        if rest:
            item[node][rest[0]] = value
        else:
            item[node] = value
    return item


def test_invoke_item_matches_the_i4_schema_and_its_session_is_the_turns():
    item = SPEC["request"]
    jsonschema.validate(item, contracts.load_schema("engine_decide_request"))
    assert item["action"] == 'AgentCore::Action::"invoke"'
    assert item["record"] is False and "tsv" not in item
    # the resource is the agent block's own agent (the engine builds that entity)
    assert item["resource"] == f'AgentCore::Agent::"{item["agent"]["agent_id"]}"'
    psid = codec.psid_for(item["ws"], SPEC["runtime_session_id"], SPEC["tsv"])
    assert item["session"] == codec.session_key(item["ws"], psid, "user", "hr-intern")
    for case in SPEC["cases"]:
        jsonschema.validate(_patched(case), contracts.load_schema("engine_decide_request"))


@pytest.mark.skipif(not ENGINE.exists(), reason="engine binary not built")
def test_legacy_invoke_policies_are_rejected_by_the_engine_compiler():
    from app.policy import dogwood_compiler

    compiled = dogwood_compiler.compile(
        json.loads((ASSETS / "demo-tools.json").read_text()), SPEC["policies"])
    assert not compiled.ok
    assert any('unrecognized action `AgentCore::Action::"invoke"`' in error["message"]
               for error in compiled.errors)
