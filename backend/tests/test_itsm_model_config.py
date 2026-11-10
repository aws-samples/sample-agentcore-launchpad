"""samples/itsm-cn: the model is an experiment-time switch (N17-obs557).

``lab.py`` keeps ``model_id`` out of the config digest (changing the model no longer makes
every script refuse the state_dir), the lab config is the single source of truth, and
``student_kit_lib.sync_model_id`` brings the manifest copy — what the resource card and
``build_manual.py`` print — in line; ``require_model`` refuses a model the console does not
offer before anything is deployed. Hermetic: the real ``lab.py`` is imported against a temp
config (boto3 builds a session, no call), everything else runs against stub modules.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/itsm-cn"
SCRIPTS = SAMPLE / "scripts"


def module(path, name=None):
    spec = importlib.util.spec_from_file_location(name or path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def lib():
    return module(SCRIPTS / "student_kit_lib.py")


SYNC_LINE = "model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b (from config)"
CATALOG = {"models": [
    {"model_id": "global.zai.glm-5.3", "provider": "bedrock", "tools": True, "available": True},
    {"model_id": "litellm.gpt-oss-120b", "provider": "litellm", "tools": True,
     "available": True},
    {"model_id": "litellm.judge-only", "provider": "litellm", "tools": False, "available": True},
    {"model_id": "global.unconfigured", "provider": "other", "tools": True, "available": False},
]}


# ----- lab.py: the digest -------------------------------------------------------------------
def lab_config(tmp_path, **overrides):
    cfg = {
        "scenario": "itsm-workshop", "account_id": "123456789012", "region": "cn-northwest-1",
        "console": "https://console.example.org:8443",
        "api_base": "https://console.example.org:8443", "tls_verify": False,
        "workspace": "default", "admin_username": "admin",
        "admin_password_parameter": "launchpad-console-password",
        "staff_accounts": ["itdesk01"], "staff_publish_targets": {},
        "model_id": "global.zai.glm-5.3", "tools_host": "i-0123456789abcdef0",
        "tools_user": "ubuntu", "tools_port": 8128, "lambda_subnet_cidr": "10.44.50.0/24",
        "state_dir": "state",
    }
    cfg.update(overrides)
    return cfg


def load_lab(tmp_path, monkeypatch, name, **overrides):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(lab_config(tmp_path, **overrides)))
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", str(path))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    return module(SCRIPTS / "lab.py", name=f"lab_{name}")


def test_model_id_is_mutable_and_excluded_from_the_config_digest(tmp_path, monkeypatch):
    glm = load_lab(tmp_path, monkeypatch, "glm", model_id="global.zai.glm-5.3")
    oss = load_lab(tmp_path, monkeypatch, "oss", model_id="litellm.gpt-oss-120b")
    assert "model_id" in glm.MUTABLE_KEYS
    assert glm.CONFIG_DIGEST == oss.CONFIG_DIGEST, "changing the model keeps the run identity"
    # a manifest written under the old model is accepted by the new config ...
    glm.ROOT.mkdir(parents=True, exist_ok=True)
    glm.save(glm.load())
    assert json.loads((glm.ROOT / "manifest.json").read_text())["model_id"] == "global.zai.glm-5.3"
    state = oss.load()
    assert state["config_sha256"] == oss.CONFIG_DIGEST and state["model_id"] == "global.zai.glm-5.3"
    # ... while a change of anything else still is another environment
    other = load_lab(tmp_path, monkeypatch, "ws", workspace="team-b")
    assert other.CONFIG_DIGEST != glm.CONFIG_DIGEST
    with pytest.raises(RuntimeError, match="belongs to another config"):
        other.load()


def test_employee_agent_prebuilt_is_mutable_and_validated(tmp_path, monkeypatch):
    """The card-wording key is boolean, defaults to true when absent, and flipping it keeps
    the run identity (so ``--cards-only`` after the change never refuses the state_dir)."""
    absent = load_lab(tmp_path, monkeypatch, "absent")
    on = load_lab(tmp_path, monkeypatch, "on", employee_agent_prebuilt=True)
    off = load_lab(tmp_path, monkeypatch, "off", employee_agent_prebuilt=False)
    assert "employee_agent_prebuilt" in absent.MUTABLE_KEYS
    assert "employee_agent_prebuilt" not in absent.CONFIG  # the default is read at use time
    assert on.CONFIG["employee_agent_prebuilt"] is True
    assert off.CONFIG["employee_agent_prebuilt"] is False
    assert absent.CONFIG_DIGEST == on.CONFIG_DIGEST == off.CONFIG_DIGEST
    assert absent.LEGACY_CONFIG_DIGEST == off.LEGACY_CONFIG_DIGEST
    with pytest.raises(ValueError, match="employee_agent_prebuilt must be a boolean"):
        load_lab(tmp_path, monkeypatch, "bad", employee_agent_prebuilt="yes")


def test_a_manifest_stamped_before_obs557_is_adopted_and_restamped(tmp_path, monkeypatch):
    """Existing environments carry the digest that still included ``model_id``; they load,
    and the next save writes the current digest. A digest that matches neither is still foreign."""
    lab = load_lab(tmp_path, monkeypatch, "legacy", model_id="global.zai.glm-5.3")
    lab.ROOT.mkdir(parents=True, exist_ok=True)
    legacy = dict(lab.load())
    legacy["config_sha256"] = lab.LEGACY_CONFIG_DIGEST
    assert lab.LEGACY_CONFIG_DIGEST != lab.CONFIG_DIGEST
    (lab.ROOT / "manifest.json").write_text(json.dumps(legacy))
    state = lab.load()
    assert state["config_sha256"] == lab.CONFIG_DIGEST
    lab.save(state)
    written = json.loads((lab.ROOT / "manifest.json").read_text())
    assert written["config_sha256"] == lab.CONFIG_DIGEST
    # the config may ALREADY name the new model when the first post-upgrade run happens: the
    # manifest still carries the model the legacy digest was stamped with, and that is enough
    moved = load_lab(tmp_path, monkeypatch, "moved", model_id="litellm.gpt-oss-120b")
    moved.ROOT.mkdir(parents=True, exist_ok=True)
    legacy["config_sha256"] = lab.LEGACY_CONFIG_DIGEST  # stamped under glm-5.3
    (moved.ROOT / "manifest.json").write_text(json.dumps(legacy))
    assert moved.load()["config_sha256"] == moved.CONFIG_DIGEST
    (lab.ROOT / "manifest.json").write_text(json.dumps(legacy))
    # a foreign manifest (different workspace / unknown digest) is still refused
    legacy["config_sha256"] = "0" * 64
    (lab.ROOT / "manifest.json").write_text(json.dumps(legacy))
    with pytest.raises(RuntimeError, match="belongs to another config"):
        lab.load()


def test_fresh_manifest_records_the_configured_model(tmp_path, monkeypatch):
    oss = load_lab(tmp_path, monkeypatch, "fresh", model_id="litellm.gpt-oss-120b")
    assert oss.load()["model_id"] == "litellm.gpt-oss-120b"


# ----- the catalog check --------------------------------------------------------------------
def test_offered_models_are_the_tool_capable_configured_rows(lib):
    assert lib.offered_model_ids(CATALOG) == ["global.zai.glm-5.3", "litellm.gpt-oss-120b"]
    assert lib.offered_model_ids({}) == []


def test_require_model_accepts_an_offered_row(lib):
    row = lib.require_model(CATALOG, "litellm.gpt-oss-120b")
    assert row["provider"] == "litellm"


@pytest.mark.parametrize("model_id, code, phrase", [
    ("litellm.gpt-oss-20b", "model.not_offered", "not in this console's model catalog"),
    ("global.unconfigured", "model.provider_not_configured", "has no credential"),
    ("litellm.judge-only", "model.no_tool_use", "does not support tool calling"),
])
def test_require_model_refuses_with_the_reason_and_the_usable_list(lib, model_id, code, phrase):
    with pytest.raises(lib.ModelNotUsable) as info:
        lib.require_model(CATALOG, model_id)
    assert info.value.code == code
    text = str(info.value)
    assert phrase in text and model_id in text
    assert "global.zai.glm-5.3, litellm.gpt-oss-120b" in text
    assert isinstance(info.value, RuntimeError)


def test_check_model_reads_the_capabilities_of_the_signed_in_user(lib):
    calls = []
    stub = types.SimpleNamespace(api=lambda c, m, p, body=None, **kw: calls.append((c, m, p))
                                 or CATALOG)
    assert lib.check_model(stub, "session", "litellm.gpt-oss-120b")["tools"] is True
    assert calls == [("session", "GET", "/api/capabilities")]


# ----- sync_model_id ------------------------------------------------------------------------
def stub_lab(root, config_model, manifest_model):
    stub = types.ModuleType("lab")
    stub.ROOT, stub.PUBLIC, stub.PREFIX = root, "https://console.example.org", "/launchpad/x/"
    stub.CONFIG = {"workspace": "default", "model_id": config_model}
    stub.sync_assets = lambda: []  # N17-obs558
    manifest = {"model_id": manifest_model, "workspace": "default", "users": {},
                "students": {"lab03": {"staff": {"username": "itdesk03"},
                                       "kb": {"kb_id": "KB1LAB03", "ds_id": "DS1LAB03"},
                                       "datasets": {}, "agents": {}, "ownership": "created",
                                       "prepared_at": "2026-11-06T01:02:03Z"}}}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    stub.load = lambda: json.loads((root / "manifest.json").read_text())
    stub.saves = []

    def save(state):
        stub.saves.append(json.loads(json.dumps(state)))
        (root / "manifest.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))

    stub.save = save
    stub.evidence = lambda name, obj: None
    stub.calls = []
    stub.session = lambda user=None: stub.calls.append(("login", user)) or object()

    def api(c, method, path, body=None, **kw):
        stub.calls.append((method, path))
        if (method, path) == ("GET", "/api/capabilities"):
            return CATALOG
        raise AssertionError(f"unexpected API call {method} {path}")

    stub.api = api
    return stub


def manifest_of(stub):
    return json.loads((stub.ROOT / "manifest.json").read_text())


def test_sync_is_a_no_op_when_the_manifest_already_matches(lib, tmp_path, capsys):
    stub = stub_lab(tmp_path, "global.zai.glm-5.3", "global.zai.glm-5.3")
    manifest = stub.load()
    assert lib.sync_model_id(stub, manifest) == "global.zai.glm-5.3"
    assert stub.saves == [] and capsys.readouterr().out == ""


def test_sync_merges_only_the_model_field_and_prints_one_line(lib, tmp_path, capsys):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    manifest = stub.load()
    # another writer (agents.py) added a row on disk after this process loaded the manifest
    on_disk = stub.load()
    on_disk["agents"] = {"lab03": {"id": "ag-1"}}
    (stub.ROOT / "manifest.json").write_text(json.dumps(on_disk))
    assert lib.sync_model_id(stub, manifest) == "litellm.gpt-oss-120b"
    assert capsys.readouterr().out == SYNC_LINE + "\n"
    assert manifest["model_id"] == "litellm.gpt-oss-120b", "the caller's copy is updated too"
    written = manifest_of(stub)
    assert written["model_id"] == "litellm.gpt-oss-120b"
    assert written["agents"] == {"lab03": {"id": "ag-1"}}, "the concurrent row survives"
    assert written["students"] == manifest["students"]
    assert len(stub.saves) == 1


def test_sync_dry_run_prints_the_planned_value_and_writes_nothing(lib, tmp_path, capsys):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    manifest = stub.load()
    assert lib.sync_model_id(stub, manifest, dry_run=True) == "litellm.gpt-oss-120b"
    out = capsys.readouterr().out
    assert out == ("model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b"
                   " (from config; dry run, manifest not written)\n")
    assert manifest["model_id"] == "litellm.gpt-oss-120b"
    assert stub.saves == [] and manifest_of(stub)["model_id"] == "global.zai.glm-5.3"


# ----- student_kit.py -----------------------------------------------------------------------
def load_kit(monkeypatch, stub):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setitem(sys.modules, "lab", stub)
    return module(SCRIPTS / "student_kit.py")


def test_cards_only_syncs_the_manifest_and_prints_the_configured_model(tmp_path, monkeypatch,
                                                                       capsys):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    sk = load_kit(monkeypatch, stub)
    assert sk.main(["--cards-only"]) == 0
    out = capsys.readouterr().out
    assert "model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b (from config)" in out
    assert manifest_of(stub)["model_id"] == "litellm.gpt-oss-120b"
    card = (stub.ROOT / "cards" / "lab03.md").read_text()
    assert "| 模型 | litellm.gpt-oss-120b |" in card and "glm-5.3" not in card
    assert stub.calls == [], "cards-only neither logs in nor calls the API"


def test_student_card_always_shows_the_configured_model(tmp_path, monkeypatch, lib):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    sk = load_kit(monkeypatch, stub)
    manifest = stub.load()  # still the stale copy: the card must not trust it
    card = sk.student_card(stub, manifest, "lab03", manifest["students"]["lab03"])
    assert "| 模型 | litellm.gpt-oss-120b |" in card


def test_kit_run_refuses_an_unknown_model_before_creating_anything(tmp_path, monkeypatch, lib):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-20b", "global.zai.glm-5.3")
    sk = load_kit(monkeypatch, stub)
    kit = sk.Kit(sk.parse_args(["lab03"]))
    with pytest.raises(RuntimeError, match="not in this console's model catalog") as info:
        kit.run()
    assert getattr(info.value, "code", None) == "model.not_offered"
    assert stub.calls == [("login", None), ("GET", "/api/capabilities")]
    assert manifest_of(stub)["model_id"] == "global.zai.glm-5.3", "refused before the sync"
    assert stub.saves == []


def test_kit_run_syncs_the_manifest_after_the_catalog_check(tmp_path, monkeypatch, capsys):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    sk = load_kit(monkeypatch, stub)
    kit = sk.Kit(sk.parse_args(["lab03"]))

    class Stop(Exception):
        pass

    def stop():
        raise Stop

    kit.ensure_staff = stop  # the first live step after the model gate
    with pytest.raises(Stop):
        kit.run()
    assert SYNC_LINE in capsys.readouterr().out
    assert manifest_of(stub)["model_id"] == "litellm.gpt-oss-120b"
    assert kit.agent_spec.__self__.m["model_id"] == "litellm.gpt-oss-120b"


def test_dry_run_card_shows_the_configured_model_without_writing(tmp_path, monkeypatch, capsys):
    stub = stub_lab(tmp_path, "litellm.gpt-oss-120b", "global.zai.glm-5.3")
    sk = load_kit(monkeypatch, stub)
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", "/nonexistent/handled-by-the-stub.json")
    assert sk.main(["lab03", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "nothing is called" in out and "| 模型 | litellm.gpt-oss-120b |" in out
    assert stub.saves == [] and stub.calls == []
    assert manifest_of(stub)["model_id"] == "global.zai.glm-5.3"
