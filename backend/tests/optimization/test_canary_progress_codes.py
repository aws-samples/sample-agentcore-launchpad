"""N17-obs447 — a canary action's progress lines carry a stable code + params.

The stage card showed ``canary_service`` progress strings verbatim in English
(「dispatching replay of 20 sessions to the evaluation worker…」, 「endpoint stable… status:
CREATING」, 「minting candidate runtime version…」). Every canary-owned call site now passes a
``progress_steps.step(code, text, **params)``: the same ``str`` (the stored ``progress`` text is
unchanged) plus a code ``run_action`` stores additively (``progress_detail`` → the payload's
``progress_code`` / ``progress_params``) and the console words in zh / en. A plain-text line
(a shared deployer helper's) clears the code, so the console falls back to the text.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

import app.optimization.canary_service as canary_svc
from app.optimization import canary_infra, progress_steps, traffic
from app.optimization.progress_steps import STEP_CODES, step
from tests.optimization.test_runtime_canaries import _mk_canary, _reload

SOURCES = [pathlib.Path(m.__file__) for m in (canary_svc, canary_infra, traffic)]


def _calls():
    """Every ``progress(…)`` / ``log(…)`` call in the canary modules: (file, line, first arg)."""
    for path in SOURCES:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("progress", "log") and node.args):
                yield path.name, node.lineno, node.args[0]


def test_every_canary_progress_call_site_passes_a_coded_step():
    plain = [f"{name}:{line}" for name, line, arg in _calls()
             if not (isinstance(arg, ast.Call) and isinstance(arg.func, ast.Name)
                     and arg.func.id == "step")]
    assert plain == [], "a canary progress line must be step(code, text, **params)"


def test_every_code_is_used_and_every_used_code_is_registered():
    used = {arg.args[0].value for _n, _l, arg in _calls()
            if isinstance(arg, ast.Call) and getattr(arg.func, "id", "") == "step"}
    assert used == STEP_CODES


def test_a_step_is_still_its_text():
    line = step("minting_candidate", "minting candidate runtime version…")
    assert line == "minting candidate runtime version…" and isinstance(line, str)
    assert line[:300] == "minting candidate runtime version…"
    assert progress_steps.detail_of(line) == {"code": "minting_candidate", "params": {}}
    assert progress_steps.detail_of("plain") is None
    with pytest.raises(ValueError):
        step("not_a_code", "x")


def test_run_action_stores_the_code_and_clears_it_with_plain_text_and_at_the_end(monkeypatch):
    row = _mk_canary()
    seen: list[tuple] = []

    def action(progress):
        progress(step("endpoint_status", "endpoint stableab status: CREATING",
                      endpoint="stableab", status="CREATING"))
        stored = _reload(row.id)
        seen.append((stored.progress, stored.progress_detail))
        progress("skill 'x' not found in registry — skipped")  # a deployer helper's line
        stored = _reload(row.id)
        seen.append((stored.progress, stored.progress_detail))

    monkeypatch.setattr(canary_svc, "_spawn", lambda target: target())
    canary_svc.run_action(row.id, "setup", action)
    assert seen == [
        ("endpoint stableab status: CREATING",
         {"code": "endpoint_status", "params": {"endpoint": "stableab", "status": "CREATING"}}),
        ("skill 'x' not found in registry — skipped", None),
    ]
    done = _reload(row.id)
    assert (done.progress, done.progress_detail, done.running_action) == (None, None, None)


def test_the_payload_carries_the_code_and_params(client):
    row = _mk_canary()
    from app.core.db import SessionLocal
    from app.optimization.models import RuntimeCanary

    db = SessionLocal()
    stored = db.get(RuntimeCanary, row.id)
    stored.running_action, stored.progress = "traffic", "dispatching replay of 20 sessions…"
    stored.progress_detail = {"code": "dispatching_replay", "params": {"sessions": 20}}
    db.commit()
    db.close()
    body = client.get(f"/api/runtime-canaries/{row.id}").json()
    assert body["progress"] == "dispatching replay of 20 sessions…"  # the old field stays
    assert (body["progress_code"], body["progress_params"]) == (
        "dispatching_replay", {"sessions": 20})


def test_the_console_mirrors_every_code():
    root = pathlib.Path(__file__).parents[3] / "frontend/src"
    src = (root / "v2/pages/canary/common.ts").read_text()
    block = re.search(r"CANARY_PROGRESS_CODES = \[(.*?)\]", src, re.S)
    assert block, "common.ts must export CANARY_PROGRESS_CODES"
    assert set(re.findall(r'"([a-z_]+)"', block.group(1))) == STEP_CODES
    import json

    for locale in ("en", "zh-CN"):
        strings = json.loads((root / f"locales/{locale}/common.json").read_text())
        assert set(strings["canaryProgress"]) == STEP_CODES, locale
