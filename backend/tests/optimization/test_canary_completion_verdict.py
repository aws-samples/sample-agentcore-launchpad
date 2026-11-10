"""N17-obs451 — a completed canary records the verdict it was promoted on.

Every completed canary read 「候选版本获胜…」 in the console, including a tie promoted with
the operator override (live: 0394f402, 0871f702). ``act_complete`` now records the locked
verdict on ``artifacts.complete`` (additive ``verdict`` / ``significant`` / ``override``,
``canary_service.locked_verdict`` — the same rule ``assert_verdict_allows`` applies), so the
console words the promotion by what the evidence said.
"""

from unittest.mock import MagicMock

import pytest

import app.optimization.canary_service as canary_svc
from app.schemas.agent import AgentSpec
from tests.optimization.test_runtime_canaries import (
    _abtest_artifact,
    _agent,
    _mk_canary,
    _persist_agents,
    _reload,
    _round,
    _setup_artifact,
)


@pytest.mark.parametrize(("verdict", "override_flag", "expected"), [
    ({"verdict": "treatment-wins", "significant": True}, False,
     {"verdict": "treatment-wins", "significant": True, "override": False}),
    ({"verdict": "treatment-wins", "significant": False}, True,
     {"verdict": "treatment-wins", "significant": False, "override": True}),
    ({"verdict": "tie", "significant": False}, True,
     {"verdict": "tie", "significant": False, "override": True}),
])
def test_complete_records_the_locked_verdict(monkeypatch, verdict, override_flag, expected):
    spec = AgentSpec(name="subject", method="zip_runtime", system_prompt="orig").model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=spec))
    row = _mk_canary(agent_id=agent_id, artifacts={
        "agent_meta": {"id": agent_id}, "edited_spec": spec,
        "setup": _setup_artifact(ramp_stage=2), "abtest": _abtest_artifact(ramp_stage=2),
        "rounds": [_round(2, verdict=verdict)]})
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    result = canary_svc.act_complete(row.id, lambda _m: None,
                                     allow_non_significant=override_flag)
    assert {k: result[k] for k in expected} == expected
    assert {k: _reload(row.id).artifacts["complete"][k] for k in expected} == expected
    assert result["winner"] == "challenger"  # the existing field is unchanged
