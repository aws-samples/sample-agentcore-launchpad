"""The runtime's config-bundle seam (N12 E2-6, CA-2): the rendered Strands template reads
the bundle pointer from the payload alias, the Custom header or the baggage header, fetches
the immutable S3 copy once per ref, and falls back to its defaults; the Harness-export
graft and the Studio shim embed the very same reader text."""

from __future__ import annotations

import importlib.util
import json
import logging
import types
from pathlib import Path

import pytest
import strands

from app.optimization import bundles
from app.schemas.agent import AgentSpec
from app.services import harness_convert
from app.templates import studio_agent
from app.templates.strands_agent import render_main_py

SPEC = AgentSpec(name="bundle-agent", method="zip_runtime", system_prompt="DEFAULT PROMPT")
TEMPLATE = Path(__file__).resolve().parents[1] / "app/templates/strands_agent/main.py.tmpl"


class _FakeTool:
    def __init__(self, fn=None, **kwargs):
        self.fn = fn
        self.kwargs = kwargs
        self.tool_name = getattr(fn, "__name__", "tool")
        self.tool_spec = {"description": kwargs.get("description", "")}

    def __call__(self, *args, **kwargs):
        if self.fn is None:
            return _FakeTool(args[0], **self.kwargs)
        return self.fn(*args, **kwargs)


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3Module:
    """``boto3`` stand-in: ``session.Session().client("s3")`` → this object."""

    def __init__(self, objects: dict[tuple[str, str], dict | bytes], missing_error=KeyError):
        self.objects = objects
        self.missing_error = missing_error
        self.calls: list[dict] = []
        self.session = types.SimpleNamespace(Session=lambda: types.SimpleNamespace(
            client=lambda service: self))

    def get_object(self, **kw):
        self.calls.append(kw)
        if (kw["Bucket"], kw["Key"]) not in self.objects:
            raise self.missing_error(kw["Key"])
        data = self.objects[(kw["Bucket"], kw["Key"])]
        return {"Body": _Body(data if isinstance(data, bytes) else json.dumps(data).encode())}


BUNDLE = {"components": {"configuration": {
    "system_prompt": "TREATMENT PROMPT",
    "tools": {"calculator": {"description": "TREATMENT calculator"}},
}}}


@pytest.fixture
def module(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(strands, "Agent", lambda **kwargs: types.SimpleNamespace(**kwargs))
    monkeypatch.setattr(strands, "tool", _FakeTool)
    monkeypatch.setenv("LAUNCHPAD_BUNDLE_BUCKET", "launchpad-artifacts")
    monkeypatch.setenv("LAUNCHPAD_AGENT_ID", "agent-1")
    target = tmp_path / "rendered_main.py"
    target.write_text(render_main_py(SPEC), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("bundle_rendered_main", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    s3 = FakeS3Module({("launchpad-artifacts", "bundles/agent-1/bndl_abc/v2.json"): BUNDLE})
    monkeypatch.setattr(mod, "_lp_boto3", s3)
    mod._launchpad_bundle_cache.clear()
    mod._launchpad_bundle_ref_var.set(None)
    monkeypatch.setattr(mod._LPBundleContext, "get_request_headers", staticmethod(lambda: {}))
    return mod, s3


def test_no_pointer_means_defaults_and_no_s3_call(module):
    mod, s3 = module
    assert mod._config_bundle() == {}
    assert mod.resolve_system_prompt() == "DEFAULT PROMPT"
    assert mod.resolve_tool_description("calculator") == mod.DEFAULT_TOOL_DESCRIPTIONS["calculator"]
    assert s3.calls == []


def test_payload_alias_channel_wins(module, monkeypatch):
    mod, s3 = module
    monkeypatch.setattr(mod._LPBundleContext, "get_request_headers", staticmethod(lambda: {
        "baggage": "launchpad.bundle=bndl_other:9"}))
    mod._launchpad_capture_bundle_ref({"prompt": "hi", "launchpad": {"bundle": "bndl_abc:2"}})
    assert mod.resolve_system_prompt() == "TREATMENT PROMPT"
    assert mod.resolve_tool_description("calculator") == "TREATMENT calculator"
    assert mod.resolve_tool_description("current_utc_time") == (
        mod.DEFAULT_TOOL_DESCRIPTIONS["current_utc_time"])
    assert s3.calls == [{"Bucket": "launchpad-artifacts",
                         "Key": "bundles/agent-1/bndl_abc/v2.json"}]
    # a payload without the alias clears the capture (the next request must not inherit it)
    mod._launchpad_capture_bundle_ref({"prompt": "hi"})
    assert mod._launchpad_bundle_ref() == "bndl_other:9"  # falls through to the headers


def test_custom_header_channel(module, monkeypatch):
    mod, s3 = module
    monkeypatch.setattr(mod._LPBundleContext, "get_request_headers", staticmethod(lambda: {
        "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Launchpad-Bundle": " bndl_abc:2 "}))
    assert mod._launchpad_bundle_ref() == "bndl_abc:2"
    assert mod.resolve_system_prompt() == "TREATMENT PROMPT"


def test_baggage_channel_with_experiment_keys_and_percent_encoding(module, monkeypatch):
    mod, s3 = module
    baggage = bundles.baggage_for("bndl_abc:2", "exp-1", "T1")
    monkeypatch.setattr(mod._LPBundleContext, "get_request_headers",
                        staticmethod(lambda: {"Baggage": baggage}))
    assert mod._launchpad_bundle_ref() == "bndl_abc:2"
    assert mod.resolve_system_prompt() == "TREATMENT PROMPT"
    monkeypatch.setattr(mod._LPBundleContext, "get_request_headers", staticmethod(lambda: {
        "baggage": "other=1, launchpad.bundle=bndl%5Fabc%3A2;prop=x"}))
    assert mod._launchpad_bundle_ref() == "bndl_abc:2"


def test_bundle_is_fetched_once_per_ref_and_errors_degrade_to_defaults(module, monkeypatch):
    mod, s3 = module
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
    for _ in range(3):
        assert mod.resolve_system_prompt() == "TREATMENT PROMPT"
        mod.resolve_tool_description("calculator")
    assert len(s3.calls) == 1  # cached per (id, version)
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_missing:1"}})
    assert mod._config_bundle() == {}  # S3 miss → defaults, never an exception
    assert mod.resolve_system_prompt() == "DEFAULT PROMPT"


def test_entrypoint_captures_the_payload_alias():
    code = TEMPLATE.read_text(encoding="utf-8")
    entry = code[code.index("@app.entrypoint"):]
    assert "_launchpad_capture_bundle_ref(payload)" in entry
    assert "get_config_bundle()" not in code


def _reader_block(text: str) -> str:
    start = text.index(bundles.READER_START)
    end = text.index(bundles.READER_END) + len(bundles.READER_END)
    return text[start:end].strip()


def test_the_three_runtime_copies_embed_the_one_reader():
    canonical = bundles.RUNTIME_BUNDLE_READER.strip()
    assert _reader_block(TEMPLATE.read_text(encoding="utf-8")) == canonical
    assert _reader_block(harness_convert.BUNDLE_GRAFT) == canonical
    assert _reader_block(studio_agent.BUNDLE_SHIM) == canonical
    # the graft keeps its anchors and public helpers (harness-template.md A1)
    assert "def resolve_system_prompt()" in harness_convert.BUNDLE_GRAFT
    assert "def _launchpad_config_bundle()" in harness_convert.BUNDLE_GRAFT
    assert "def launchpad_config_bundle()" in studio_agent.BUNDLE_SHIM
    for text in (harness_convert.BUNDLE_GRAFT, studio_agent.BUNDLE_SHIM):
        assert "get_config_bundle()" not in text
        compile(text, "<copy>", "exec")


def test_graft_and_shim_compile_and_capture_the_payload():
    grafted = harness_convert.graft_config_bundle(
        'DEFAULT_SYSTEM_PROMPT = """base"""\n\n'
        "def get_or_create_agent(session_id, user_id, plugins):\n"
        "    return Agent(system_prompt=DEFAULT_SYSTEM_PROMPT)\n\n"
        "def invoke(payload):\n"
        "    agent = get_or_create_agent(session_id, user_id, _skill_plugins)\n"
        "    return agent\n",
        default_system_prompt="promoted",
    )
    compile(grafted, "grafted.py", "exec")
    assert bundles.READER_START in grafted and "resolve_system_prompt()" in grafted
    adapted = studio_agent.adapt_studio_code(
        "async def main(user_input, messages):\n    print(user_input)\n")
    compile(adapted, "studio.py", "exec")
    assert bundles.READER_START in adapted
    entry = adapted[adapted.index("@app.entrypoint"):]
    assert "_launchpad_capture_bundle_ref(payload)" in entry


# ── N12-obs37: the reader is fail-soft but never silent ──────────────────────
# One INFO line per fetch, one WARNING per failure (key + exception class, message ≤ 200 chars),
# one WARNING when LAUNCHPAD_AGENT_ID is unset — and never a byte of the bundle body.

BUNDLE_BYTES = len(json.dumps(BUNDLE).encode())
# every string a log line could leak from the fixtures: the bundle body, the production prompt
CONTENT_MARKERS = ("TREATMENT", "DEFAULT PROMPT", "system_prompt", "calculator", "SECRET")


class NoSuchKey(Exception):
    """Stand-in for botocore's ``ClientError`` subclass of the same name."""


def _bundle_records(caplog):
    return [r for r in caplog.records if r.name == "launchpad.bundle"]


def _assert_no_bundle_content(caplog):
    for record in caplog.records:
        line = record.getMessage()
        assert not any(marker in line for marker in CONTENT_MARKERS), line


def test_fetch_emits_one_info_line_with_key_bytes_and_version(module, caplog):
    mod, s3 = module
    caplog.set_level(logging.INFO, logger="launchpad.bundle")
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
    assert mod.resolve_system_prompt() == "TREATMENT PROMPT"
    mod.resolve_tool_description("calculator")  # cache hit — no second line
    records = _bundle_records(caplog)
    assert [r.levelno for r in records] == [logging.INFO]
    assert records[0].getMessage() == (
        "launchpad.bundle: fetched key=bundles/agent-1/bndl_abc/v2.json "
        f"bytes={BUNDLE_BYTES} version=2"
    )
    _assert_no_bundle_content(caplog)


def test_missing_object_warns_with_key_and_class_and_falls_back(module, monkeypatch, caplog):
    mod, _ = module
    s3 = FakeS3Module({}, missing_error=NoSuchKey)
    monkeypatch.setattr(mod, "_lp_boto3", s3)
    caplog.set_level(logging.INFO, logger="launchpad.bundle")
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_missing:1"}})
    assert mod._config_bundle() == {}
    assert mod.resolve_system_prompt() == "DEFAULT PROMPT"
    records = _bundle_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]  # one per attempt
    for record in records:
        assert record.getMessage().startswith(
            "launchpad.bundle: fetch failed key=bundles/agent-1/bndl_missing/v1.json "
            "error=NoSuchKey: "
        )
    assert "bndl_missing:1" not in mod._launchpad_bundle_cache  # a failure is never cached
    assert len(s3.calls) == 2
    _assert_no_bundle_content(caplog)


def test_bad_json_warns_without_echoing_the_body(module, monkeypatch, caplog):
    mod, _ = module
    s3 = FakeS3Module({("launchpad-artifacts", "bundles/agent-1/bndl_abc/v2.json"):
                       b'{"components": {"configuration": {"system_prompt": "SECRET' + b"x" * 500})
    monkeypatch.setattr(mod, "_lp_boto3", s3)
    caplog.set_level(logging.INFO, logger="launchpad.bundle")
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
    assert mod._config_bundle() == {}
    assert mod.resolve_system_prompt() == "DEFAULT PROMPT"
    [first, _second] = _bundle_records(caplog)
    assert first.levelno == logging.WARNING
    message = first.getMessage()
    prefix = ("launchpad.bundle: fetch failed key=bundles/agent-1/bndl_abc/v2.json "
              "error=JSONDecodeError: ")
    assert message.startswith(prefix)
    assert len(message) <= len(prefix) + 200
    _assert_no_bundle_content(caplog)


def test_exception_message_is_truncated_to_200_chars(module, monkeypatch, caplog):
    mod, _ = module

    class Timeout(Exception):
        pass

    s3 = FakeS3Module({}, missing_error=lambda key: Timeout("t " * 400 + "SECRET"))
    monkeypatch.setattr(mod, "_lp_boto3", s3)
    caplog.set_level(logging.INFO, logger="launchpad.bundle")
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
    assert mod._config_bundle() == {}
    [record] = _bundle_records(caplog)
    prefix = "launchpad.bundle: fetch failed key=bundles/agent-1/bndl_abc/v2.json error=Timeout: "
    assert record.getMessage().startswith(prefix)
    assert len(record.getMessage()) == len(prefix) + 200
    _assert_no_bundle_content(caplog)


def test_unset_agent_id_disables_the_reader_loudly_and_skips_s3(module, monkeypatch, caplog):
    mod, s3 = module
    monkeypatch.setenv("LAUNCHPAD_AGENT_ID", "")
    caplog.set_level(logging.INFO, logger="launchpad.bundle")
    mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
    assert mod._config_bundle() == {}
    assert mod.resolve_system_prompt() == "DEFAULT PROMPT"  # the N12-obs36 shape, now visible
    records = _bundle_records(caplog)
    assert [r.levelno for r in records] == [logging.WARNING, logging.WARNING]
    assert records[0].getMessage() == (
        "launchpad.bundle: bundle reader disabled: LAUNCHPAD_AGENT_ID not set (ref=bndl_abc:2)")
    assert s3.calls == []  # never asks S3 for `bundles//…`
    _assert_no_bundle_content(caplog)


def test_reader_logs_to_stderr_when_the_host_configured_no_logging(module, monkeypatch, capsys):
    """A Harness export has no logging setup at all: Python's last-resort handler would drop
    INFO, so the reader attaches one stderr handler of its own (and only then)."""
    mod, s3 = module
    logger = mod._launchpad_bundle_logger
    monkeypatch.setattr(logger, "hasHandlers", lambda: False)  # pytest's root capture hidden
    try:
        mod._launchpad_capture_bundle_ref({"launchpad": {"bundle": "bndl_abc:2"}})
        assert mod.resolve_system_prompt() == "TREATMENT PROMPT"
        err = capsys.readouterr().err
        assert err.splitlines() == [
            "INFO launchpad.bundle: fetched key=bundles/agent-1/bndl_abc/v2.json "
            f"bytes={BUNDLE_BYTES} version=2"
        ]
        assert not any(marker in err for marker in CONTENT_MARKERS)
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)


def test_reader_v2_marker_and_stdlib_only_imports():
    """Reader text v2 (N12-obs37) everywhere; the grafted code adds no dependency beyond the
    stdlib `logging` — every host already ships boto3 and the AgentCore SDK."""
    assert bundles.READER_START == "# <launchpad-bundle-reader:v2>"
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "launchpad-bundle-reader:v1" not in text
    assert "launchpad-bundle-reader:v1" not in harness_convert.BUNDLE_GRAFT
    assert "launchpad-bundle-reader:v1" not in studio_agent.BUNDLE_SHIM
    modules = set()
    for line in bundles.RUNTIME_BUNDLE_READER.splitlines():
        if line.startswith("import "):
            modules.add(line.split()[1].split(".")[0])
        elif line.startswith("from "):
            modules.add(line.split()[1].split(".")[0])
    assert modules == {
        "json", "logging", "os", "urllib", "contextvars", "boto3", "bedrock_agentcore"}

