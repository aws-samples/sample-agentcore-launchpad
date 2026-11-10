"""Runtime-generated key material for the stage-0 contract tests: nothing secret is ever
committed (fixtures are public / synthetic; see tests/fixtures/policy_principal/README.md)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.policy_principal.fakes import EphemeralSigner, FakeEngine, ephemeral_hmac_key

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "policy_principal"
REPO = Path(__file__).resolve().parents[3]

# The real-engine interceptor tests (``test_interceptor_real_engine.py``) run against THIS
# worktree's release binary — the one `make verify` builds in its "policy-engine · cargo test +
# release build" step, which runs before the backend pytest step — and the crate's assets.
# ``LP_ENGINE_BIN`` / ``LP_ENGINE_ASSETS`` still override (another worktree's build); without
# a binary the tests skip, they never fail (N17-obs516: the defaults were missing, so the gate
# never ran the only real-engine end-to-end test of the interceptor).
ENGINE_BIN = Path(os.environ.get("LP_ENGINE_BIN")
                  or REPO / "policy-engine" / "target" / "release" / "launchpad-policy-engine")
ENGINE_ASSETS = Path(os.environ.get("LP_ENGINE_ASSETS")
                     or REPO / "policy-engine" / "crates" / "lp-dogwood" / "assets")
needs_real_engine = pytest.mark.skipif(
    not (ENGINE_BIN.is_file() and ENGINE_ASSETS.is_dir()),
    reason="launchpad-policy-engine release binary / assets not found "
           "(build policy-engine or set LP_ENGINE_BIN / LP_ENGINE_ASSETS)")


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def signer() -> EphemeralSigner:
    return EphemeralSigner()


@pytest.fixture
def hmac_key() -> bytes:
    return ephemeral_hmac_key()


@pytest.fixture
def fake_engine(hmac_key: bytes) -> FakeEngine:
    return FakeEngine(key=hmac_key)
