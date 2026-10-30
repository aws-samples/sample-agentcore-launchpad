"""Studio local-debug AI-fix pipeline (slice 3).

Covers the ported codegen fix-half: contract/ruff/import-smoke validators,
fix-workspace layout + error tail truncation, diagnosis.json normalization,
the environment-category revert guard, and the full fix_code_events event
sequence driven by a FAKE coding-agent backend (no live Kimi). The Kimi
backend itself is covered by test_codegen_kimi.py.
"""

import asyncio
import json
import os
import tempfile
from pathlib import Path

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

from app.codegen import config, service, validators, workspace_builder  # noqa: E402
from app.codegen.backends import registry  # noqa: E402
from app.codegen.backends.base import CodingAgentBackend  # noqa: E402
from app.codegen.validators import (  # noqa: E402
    ValidationIssue,
    ValidationReport,
    validate_contract,
)
from app.core.config import get_settings  # noqa: E402

# --- contract fixtures ----------------------------------------------------

GOOD_CODE = (
    "import argparse\n"
    "import asyncio\n"
    "\n"
    "\n"
    "async def main(user_input_arg=None, messages_arg=None):\n"
    '    """Handles --user-input and --messages."""\n'
    '    return "ok"\n'
    "\n"
    "\n"
    'if __name__ == "__main__":\n'
    "    parser = argparse.ArgumentParser()\n"
    '    parser.add_argument("--user-input")\n'
    '    parser.add_argument("--messages")\n'
    "    args = parser.parse_args()\n"
    "    print(asyncio.run(main(args.user_input, args.messages)))\n"
)

NO_STREAM_FLOW = {"nodes": [], "edges": []}


async def _collect(gen):
    return [item async for item in gen]


# --- Stage 1: AST contract validation -------------------------------------

def test_contract_passes_for_good_code():
    assert validate_contract(GOOD_CODE, NO_STREAM_FLOW) == []


def test_contract_flags_missing_callback_handler():
    code = (
        "async def main(user_input_arg=None, messages_arg=None):\n"
        "    # supports --user-input and --messages\n"
        '    agent = Agent(model="x")\n'
        '    return "ok"\n'
        "\n"
        'if __name__ == "__main__":\n'
        "    pass\n"
    )
    issues = validate_contract(code, NO_STREAM_FLOW)
    assert any("callback_handler=None" in i.message and i.stage == "ast" for i in issues)


def test_contract_flags_missing_main_guard():
    code = (
        "async def main(user_input_arg=None, messages_arg=None):\n"
        '    """Handles --user-input and --messages."""\n'
        '    return "ok"\n'
    )
    issues = validate_contract(code, NO_STREAM_FLOW)
    assert [i.message for i in issues] == ['Missing \'if __name__ == "__main__"\' guard']


def test_contract_flags_stream_async_mismatch():
    code = (
        "import asyncio\n"
        "\n"
        "async def main(user_input_arg=None, messages_arg=None):\n"
        '    """Handles --user-input and --messages."""\n'
        "    async for event in agent.stream_async('hi'):\n"
        "        print(event)\n"
        "\n"
        'if __name__ == "__main__":\n'
        "    pass\n"
    )
    # Flow has no streaming agent but code uses stream_async -> mismatch error.
    issues = validate_contract(code, NO_STREAM_FLOW)
    assert any("stream_async" in i.message for i in issues)


def test_contract_flags_missing_async_main():
    code = 'if __name__ == "__main__":\n    pass  # --user-input --messages\n'
    issues = validate_contract(code, NO_STREAM_FLOW)
    assert any("async def main" in i.message for i in issues)


# --- Stage 2: ruff --------------------------------------------------------

def test_ruff_stage_passes_clean_code(tmp_path):
    (tmp_path / config.GENERATED_FILENAME).write_text(GOOD_CODE, encoding="utf-8")
    assert asyncio.run(validators.run_ruff(tmp_path)) == []


def test_ruff_stage_flags_undefined_name(tmp_path):
    (tmp_path / config.GENERATED_FILENAME).write_text(
        "x = undefined_name_here\n", encoding="utf-8"
    )
    issues = asyncio.run(validators.run_ruff(tmp_path))
    assert issues and all(i.stage == "ruff" for i in issues)
    assert any("F821" in i.message for i in issues)


# --- Stage 3: import smoke skip path --------------------------------------

def test_import_smoke_skips_when_exec_python_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "studio_exec_python", "/no/such/python-xyz")
    (tmp_path / config.GENERATED_FILENAME).write_text(GOOD_CODE, encoding="utf-8")
    # Missing interpreter -> stage skipped-with-note, returns no issues.
    assert asyncio.run(validators.run_import_smoke(tmp_path)) == []


def test_validate_pipeline_short_circuits_on_ast(tmp_path):
    # Contract failure returns before ruff/import ever run.
    (tmp_path / config.GENERATED_FILENAME).write_text("def main(): pass\n", encoding="utf-8")
    report = asyncio.run(validators.validate_generated_code(tmp_path, NO_STREAM_FLOW))
    assert report.passed is False
    assert all(i["stage"] == "ast" for i in report.to_dict()["errors"])


# --- fix workspace layout -------------------------------------------------

def test_build_fix_workspace_layout_and_tail_truncation():
    big_error = ("A" * 10000) + "ROOT_CAUSE_MARKER"
    flow = {
        "nodes": [{"id": "n1", "type": "agent", "position": {"x": 1, "y": 2}, "data": {"k": "v"}}],
        "edges": [],
    }
    ws = workspace_builder.build_fix_workspace(
        GOOD_CODE, big_error, flow, graph_mode=True, input_data="my question"
    )
    try:
        # Guidance + flow + failing code all present.
        assert (ws / "CLAUDE.md").exists()  # FIX_CLAUDE.md renamed
        assert (ws / "contract_spec.md").exists()
        assert (ws / "flow_semantics.md").exists()
        assert (ws / config.GENERATED_FILENAME).read_text() == GOOD_CODE

        flow_json = json.loads((ws / "flow.json").read_text())
        assert flow_json["graph_mode"] is True
        # Layout fields stripped, data preserved.
        assert "position" not in flow_json["nodes"][0]
        assert flow_json["nodes"][0]["data"] == {"k": "v"}

        error_txt = (ws / "error.txt").read_text()
        assert "my question" in error_txt  # input header prepended
        assert "truncated" in error_txt  # 8KB tail truncation marker
        assert "ROOT_CAUSE_MARKER" in error_txt  # tail (root cause) preserved
        assert len(error_txt.encode("utf-8")) < len(big_error.encode("utf-8"))
    finally:
        workspace_builder.cleanup_workspace(ws)
    assert not ws.exists()


def test_error_not_truncated_when_small():
    ws = workspace_builder.build_fix_workspace(
        GOOD_CODE, "short error", {"nodes": [], "edges": []}, graph_mode=False
    )
    try:
        error_txt = (ws / "error.txt").read_text()
        assert error_txt == "short error"  # no header (no input), no truncation
    finally:
        workspace_builder.cleanup_workspace(ws)


def test_canonicalize_flow_strips_layout_and_adds_graph_mode():
    flow = {
        "nodes": [{"id": "a", "position": {"x": 0}, "width": 10, "data": {"x": 1}}],
        "edges": [{"id": "e"}],
    }
    canon = workspace_builder.canonicalize_flow(flow, graph_mode=True)
    assert canon["graph_mode"] is True
    assert "position" not in canon["nodes"][0] and "width" not in canon["nodes"][0]
    assert canon["nodes"][0]["data"] == {"x": 1}
    assert canon["edges"] == [{"id": "e"}]


# --- diagnosis.json normalization -----------------------------------------

def _write_diag(tmp_path: Path, payload) -> Path:
    (tmp_path / service.DIAGNOSIS_FILENAME).write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return tmp_path


def test_read_diagnosis_normalizes_valid(tmp_path):
    ws = _write_diag(
        tmp_path,
        {
            "category": "config",
            "summary": "Model id is invalid",
            "suggestions": [
                {"node_label": "Agent", "property": "model", "action": "pick a valid id"},
                "not-a-dict",
                {"unknown_key": "dropped"},
            ],
        },
    )
    diag = service._read_diagnosis(ws, fallback_summary="fb")
    assert diag["category"] == "config"
    assert diag["summary"] == "Model id is invalid"
    assert diag["suggestions"] == [
        {"node_label": "Agent", "property": "model", "action": "pick a valid id"}
    ]


def test_read_diagnosis_coerces_bad_category(tmp_path):
    ws = _write_diag(tmp_path, {"category": "bogus", "summary": "x"})
    assert service._read_diagnosis(ws, fallback_summary="fb")["category"] == "code"


def test_read_diagnosis_uses_fallback_summary_when_blank(tmp_path):
    ws = _write_diag(tmp_path, {"category": "code", "summary": "   "})
    assert service._read_diagnosis(ws, fallback_summary="the last agent message")["summary"] == (
        "the last agent message"
    )


def test_read_diagnosis_missing_file_degrades(tmp_path):
    diag = service._read_diagnosis(tmp_path, fallback_summary="")
    assert diag["category"] == "code"
    assert diag["suggestions"] == []
    assert "did not produce a structured diagnosis" in diag["summary"]


# --- FAKE backend for full fix_code_events flow ---------------------------

class _FakeBackend(CodingAgentBackend):
    """Deterministic stand-in for the Kimi backend.

    Writes a diagnosis.json and (optionally) rewrites generated_agent.py on the
    first round; asserts nothing live. Configured via class attributes set per
    test through a factory.
    """

    name = "kimi"
    new_code: str | None = None
    diagnosis: dict | None = None
    available: tuple[bool, str] = (True, "")

    async def check_available(self):
        return self.available

    async def generate(self, workspace, task, on_progress):
        await on_progress("Reading error.txt")
        await on_progress("[tool] Read generated_agent.py")
        if self.diagnosis is not None:
            (workspace / service.DIAGNOSIS_FILENAME).write_text(
                json.dumps(self.diagnosis), encoding="utf-8"
            )
        if self.new_code is not None:
            (workspace / config.GENERATED_FILENAME).write_text(
                self.new_code, encoding="utf-8"
            )
        await on_progress("Wrote diagnosis.json")

    async def close(self):
        return None


def _install_fake_backend(monkeypatch, *, new_code, diagnosis, available=(True, "")):
    backend_cls = type(
        "_TestFakeBackend",
        (_FakeBackend,),
        {"new_code": new_code, "diagnosis": diagnosis, "available": available},
    )
    monkeypatch.setitem(registry._BACKENDS, "kimi", backend_cls)


FIXED_CODE = GOOD_CODE.replace('return "ok"', 'return "fixed"')


def test_fix_flow_applies_change_and_emits_done(monkeypatch):
    _install_fake_backend(
        monkeypatch,
        new_code=FIXED_CODE,
        diagnosis={"category": "code", "summary": "Off-by-one in main", "suggestions": []},
    )
    # Keep validation hermetic (no uvx/exec-python dependency in the flow test).
    monkeypatch.setattr(service, "validate_generated_code", _passing_validate)

    events = asyncio.run(
        _collect(service.fix_code_events(GOOD_CODE, "Traceback boom", NO_STREAM_FLOW))
    )
    kinds = [e["event"] for e in events]
    assert "progress" in kinds
    assert "agent_activity" in kinds
    assert "validation" in kinds  # code changed -> validation ran
    assert kinds[-1] == "done"

    done = events[-1]["data"]
    assert set(done) == {"code", "changed", "diagnosis", "validation_report", "duration_ms"}
    assert done["changed"] is True
    assert done["code"] == FIXED_CODE
    assert done["diagnosis"]["category"] == "code"
    assert done["validation_report"]["passed"] is True
    assert isinstance(done["duration_ms"], int)


def test_fix_flow_environment_category_reverts_code(monkeypatch):
    # Agent changed the code, but diagnosed an environment issue -> revert.
    _install_fake_backend(
        monkeypatch,
        new_code=FIXED_CODE,
        diagnosis={"category": "environment", "summary": "Missing OPENAI_API_KEY"},
    )
    events = asyncio.run(
        _collect(service.fix_code_events(GOOD_CODE, "KeyError OPENAI_API_KEY", NO_STREAM_FLOW))
    )
    done = events[-1]["data"]
    assert done["changed"] is False
    assert done["code"] == GOOD_CODE  # reverted
    assert done["diagnosis"]["category"] == "environment"
    assert done["validation_report"] is None  # validation never ran
    # No validation event when nothing was validated.
    assert "validation" not in [e["event"] for e in events]


def test_fix_flow_reverts_when_validation_never_passes(monkeypatch):
    _install_fake_backend(
        monkeypatch,
        new_code=FIXED_CODE,
        diagnosis={"category": "code", "summary": "Attempted fix"},
    )
    monkeypatch.setattr(service, "validate_generated_code", _failing_validate)

    events = asyncio.run(
        _collect(service.fix_code_events(GOOD_CODE, "boom", NO_STREAM_FLOW))
    )
    done = events[-1]["data"]
    assert done["changed"] is False
    assert done["code"] == GOOD_CODE  # reverted to original — never ship broken code
    assert done["validation_report"]["passed"] is False
    assert "failed contract validation" in done["diagnosis"]["summary"]


def test_fix_flow_no_code_change_reports_unchanged(monkeypatch):
    # Agent wrote only a diagnosis, left the code alone.
    _install_fake_backend(
        monkeypatch,
        new_code=None,
        diagnosis={"category": "config", "summary": "Increase max_tokens on the agent node"},
    )
    events = asyncio.run(
        _collect(service.fix_code_events(GOOD_CODE, "boom", NO_STREAM_FLOW))
    )
    done = events[-1]["data"]
    assert done["changed"] is False
    assert done["code"] == GOOD_CODE
    assert done["diagnosis"]["category"] == "config"


def test_fix_flow_emits_error_when_backend_unavailable(monkeypatch):
    _install_fake_backend(
        monkeypatch, new_code=None, diagnosis=None, available=(False, "no creds")
    )
    events = asyncio.run(
        _collect(service.fix_code_events(GOOD_CODE, "boom", NO_STREAM_FLOW))
    )
    assert events[-1]["event"] == "error"
    assert "no creds" in events[-1]["data"]["message"]


# --- get_status shape -----------------------------------------------------

def test_get_status_reports_unavailable_reason(monkeypatch):
    _install_fake_backend(
        monkeypatch, new_code=None, diagnosis=None,
        available=(False, "AWS_BEARER_TOKEN_BEDROCK is not set"),
    )
    status = asyncio.run(service.get_status())
    assert status["backend"] == "kimi"
    assert status["available"] is False
    assert status["reason"] == "AWS_BEARER_TOKEN_BEDROCK is not set"
    assert status["model"] == config.get_model()


def test_get_status_unknown_backend(monkeypatch):
    monkeypatch.setattr(get_settings(), "codegen_backend", "does-not-exist")
    status = asyncio.run(service.get_status())
    assert status["available"] is False
    assert "Unknown codegen backend" in status["reason"]


# --- validation stubs (module-level so monkeypatch can install them) ------

async def _passing_validate(workspace, flow_data):
    return ValidationReport(passed=True)


async def _failing_validate(workspace, flow_data):
    return ValidationReport(
        passed=False,
        errors=[ValidationIssue(stage="ast", message="still broken")],
    )
