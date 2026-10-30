"""N12 E0-1: the Strands evaluation / optimizer stack is pinned explicitly.

`bedrock-agentcore[simulation]` only asks for `strands-agents-evals>=0.1.0`, so the lock
used to resolve 1.0.2 by accident; the evaluation engine (plan §F) is written against
1.5.0 and `strands-harness-optimizer` 0.0.1, and the worker zip pins the same versions.
A silent drift would break the inline mirror test (E1-3b) long after the fact, so the
pins fail loudly here instead.
"""

import importlib
import importlib.metadata as md

import pytest

PINS = {
    "strands-agents": "1.47.0",
    "strands-agents-evals": "1.5.0",
    "strands-harness-optimizer": "0.0.1",
}

# (module, attribute) — the entry points the engine, spans provider, insights,
# simulator and recommendation provider build on (plan §B.2 / §B.4 / §B.5).
IMPORTS = [
    ("strands_evals.providers", "CloudWatchProvider"),
    ("strands_evals.mappers", "CloudWatchSessionMapper"),
    ("strands_evals.detectors", "diagnose_session"),
    ("strands_evals.simulation", "ActorSimulator"),
    ("strands_evals.tools.evaluation_tools", "in_order_match_scorer"),
    ("strands_harness_optimizer.optimizers.system_prompt", "ContrastiveReflectionOptimizer"),
    ("strands_harness_optimizer.rollout_engines", "AgentRolloutEngine"),
]


@pytest.mark.parametrize("dist,version", sorted(PINS.items()))
def test_distribution_is_pinned(dist, version):
    assert md.version(dist) == version


@pytest.mark.parametrize("module,attr", IMPORTS)
def test_engine_entry_points_import(module, attr):
    mod = importlib.import_module(module)
    assert getattr(mod, attr) is not None


def test_pyproject_carries_the_explicit_pins():
    """Transitive resolution is not a pin: the three packages must be direct deps."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    for dist, version in PINS.items():
        # N-R6: strands-agents carries the [openai] extra (OpenAIModel for the
        # OpenAI-compatible providers); the pin itself is unchanged
        candidates = (f'"{dist}=={version}"', f'"{dist}[openai]=={version}"')
        assert any(c in text for c in candidates), f"{dist}=={version} missing from pyproject"
