"""samples/itsm-cn: the state_dir asset cache follows the package (N17-obs558) and the tools
host's run marker survives the obs557 digest change (N17-obs559).

``lab.initialize()`` used to copy ``lab-assets/`` and ``policies/`` into the state_dir once;
every later script read that copy, so a repo asset update never reached an existing
environment. ``lab.sync_assets()`` now refreshes the cache and every asset-reading script
calls it first. ``refresh_service.py`` / ``reset_lab.py`` compared the host's ``.run`` marker
with a bare ``test`` that failed silently once ``model_id`` left the digest;
``lab.run_marker_guard`` accepts the legacy digests, rewrites the marker and explains a
mismatch on stdout. Hermetic: the real ``lab`` module under a throwaway config (no AWS call is
made at import), stub ``remote`` / IAM, and ``bash`` for the guard itself.
"""
from __future__ import annotations

import importlib.util
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/itsm-cn"
SCRIPTS = SAMPLE / "scripts"


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


def load_lab(tmp_path, monkeypatch, name="lab_under_test", **overrides):
    path = tmp_path / f"{name}.json"
    path.write_text(json.dumps(lab_config(tmp_path, **overrides)))
    monkeypatch.setenv("LAUNCHPAD_LAB_CONFIG", str(path))
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / "lab.py")
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture
def lab(tmp_path, monkeypatch):
    return load_lab(tmp_path, monkeypatch)


def stub_package(root: Path) -> Path:
    """A miniature package tree: two asset dirs, a nested knowledge file, a __pycache__."""
    pkg = root / "package"
    (pkg / "lab-assets/knowledge").mkdir(parents=True)
    (pkg / "lab-assets/tools.json").write_text('{"v": 1}')
    (pkg / "lab-assets/service.py").write_text("VERSION = 1\n")
    (pkg / "lab-assets/knowledge/KB0010001.md").write_text("# one\n")
    (pkg / "lab-assets/__pycache__").mkdir()
    (pkg / "lab-assets/__pycache__/service.cpython-313.pyc").write_bytes(b"\x00")
    (pkg / "policies").mkdir()
    (pkg / "policies/itsm_requester_is_self.cedar").write_text("forbid (template);\n")
    return pkg


# ----- sync_assets -----------------------------------------------------------------------
def test_first_sync_copies_both_asset_dirs_and_skips_pycache(lab, tmp_path, capsys):
    pkg, state = stub_package(tmp_path), tmp_path / "state"
    changed = lab.sync_assets(pkg, state)
    assert changed == ["lab-assets/knowledge/KB0010001.md", "lab-assets/service.py",
                       "lab-assets/tools.json", "policies/itsm_requester_is_self.cedar"]
    assert (state / "lab-assets/tools.json").read_text() == '{"v": 1}'
    assert not (state / "lab-assets/__pycache__").exists()
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("Assets synced from the package (4 file(s)): ")


def test_sync_is_idempotent_and_silent_when_nothing_changed(lab, tmp_path, capsys):
    pkg, state = stub_package(tmp_path), tmp_path / "state"
    lab.sync_assets(pkg, state)
    capsys.readouterr()
    assert lab.sync_assets(pkg, state) == []
    assert capsys.readouterr().out == ""


def test_a_changed_package_file_propagates_to_the_state_dir(lab, tmp_path, capsys):
    pkg, state = stub_package(tmp_path), tmp_path / "state"
    lab.sync_assets(pkg, state)
    capsys.readouterr()
    (pkg / "lab-assets/service.py").write_text("VERSION = 2  # token search shipped\n")
    (pkg / "lab-assets/knowledge/KB0010002.md").write_text("# two\n")
    changed = lab.sync_assets(pkg, state)
    assert changed == ["lab-assets/knowledge/KB0010002.md", "lab-assets/service.py"]
    assert (state / "lab-assets/service.py").read_text() == "VERSION = 2  # token search shipped\n"
    assert "service.py" in capsys.readouterr().out


def test_extra_state_dir_files_are_kept_and_rendered_policies_are_not_clobbered(lab, tmp_path):
    pkg, state = stub_package(tmp_path), tmp_path / "state"
    lab.sync_assets(pkg, state)
    (state / "lab-assets/scratch.json").write_text("{}")
    (state / "policies/itsm_kb_publish_servicedesk_only.cedar").write_text("forbid (x);\n")
    # prepare_policy.py renders the requester rule for the configured staff into this file
    (state / "policies/itsm_requester_is_self.cedar").write_text("forbid (rendered itdesk03);\n")
    (pkg / "policies/itsm_requester_is_self.cedar").write_text("forbid (template v2);\n")
    (pkg / "policies/new_seed.cedar").write_text("permit (seed);\n")
    assert lab.sync_assets(pkg, state, quiet=True) == ["policies/new_seed.cedar"]
    assert (state / "lab-assets/scratch.json").exists()
    assert (state / "policies/itsm_kb_publish_servicedesk_only.cedar").exists()
    assert (state / "policies/itsm_requester_is_self.cedar").read_text() == (
        "forbid (rendered itdesk03);\n")


def test_defaults_are_the_package_and_the_configured_state_dir(lab):
    """Without arguments the real package tree lands in ``lab.ROOT`` (the state_dir)."""
    changed = lab.sync_assets(quiet=True)
    assert "lab-assets/tools.json" in changed and "lab-assets/prompts.json" in changed
    assert "policies/itsm_requester_is_self.cedar" in changed
    assert (lab.ROOT / "lab-assets/tools.json").read_bytes() == (
        SAMPLE / "lab-assets/tools.json").read_bytes()
    assert lab.sync_assets(quiet=True) == []


def test_initialize_uses_sync_assets(lab, monkeypatch):
    calls = []
    monkeypatch.setattr(lab, "sync_assets", lambda *a, **kw: calls.append((a, kw)) or [])
    monkeypatch.setattr(lab.AWS, "client", lambda name: type("Sts", (), {
        "get_caller_identity": staticmethod(lambda: {"Account": "123456789012"})})())
    lab.initialize()
    assert calls == [((), {})]
    assert (lab.ROOT / "handbook.md").exists() and (lab.ROOT / "manifest.json").exists()


ASSET_READERS = sorted(
    p.name for p in SCRIPTS.glob("*.py")
    if re.search(r'ROOT / "lab-assets|ROOT / "policies', p.read_text())
    and p.name not in ("lab.py", "test_business.py")
)


def test_every_asset_reading_script_syncs_first():
    """Static guard for the import-level contract: a script that reads the state_dir copy of
    an asset calls ``sync_assets()`` (the dynamic checks below cover two of them end to end)."""
    assert ASSET_READERS, "the glob found no asset readers; was the layout renamed?"
    missing = [n for n in ASSET_READERS if "sync_assets()" not in (SCRIPTS / n).read_text()]
    assert missing == [], f"scripts reading ROOT assets without sync_assets(): {missing}"


# ----- run_marker_guard --------------------------------------------------------------------
def run_guard(lab, host_dir: Path, legacy=(), *, scenario=None, run=None):
    (host_dir / ".scenario").write_text(scenario if scenario is not None else lab.SCENARIO)
    (host_dir / ".run").write_text(run if run is not None else lab.CONFIG_DIGEST)
    script = "set -eu\n" + lab.run_marker_guard(shlex.quote(str(host_dir)),
                                                legacy_digests=legacy) + "\necho reached\n"
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_guard_passes_a_current_marker_silently(lab, tmp_path):
    out = run_guard(lab, tmp_path, legacy=(lab.LEGACY_CONFIG_DIGEST,))
    assert out.returncode == 0 and out.stdout == "reached\n"
    assert (tmp_path / ".run").read_text() == lab.CONFIG_DIGEST


def test_guard_migrates_a_legacy_marker_and_says_so(lab, tmp_path):
    legacy = lab.legacy_config_digest("litellm.gpt-oss-120b")  # the manifest's model at install
    assert legacy != lab.CONFIG_DIGEST and legacy != lab.LEGACY_CONFIG_DIGEST
    out = run_guard(lab, tmp_path, legacy=(lab.LEGACY_CONFIG_DIGEST, legacy), run=legacy)
    assert out.returncode == 0, out.stderr
    assert out.stdout == (f"run marker migrated: {legacy} -> {lab.CONFIG_DIGEST}"
                          " (N17-obs557 digest change)\nreached\n")
    assert (tmp_path / ".run").read_text() == lab.CONFIG_DIGEST
    # a second run is the plain current-marker path
    again = run_guard(lab, tmp_path, legacy=(lab.LEGACY_CONFIG_DIGEST, legacy))
    assert again.stdout == "reached\n"


def test_guard_explains_a_run_mismatch_on_stdout_and_exits_nonzero(lab, tmp_path):
    out = run_guard(lab, tmp_path, legacy=(lab.LEGACY_CONFIG_DIGEST,), run="deadbeef")
    assert out.returncode == 1
    assert out.stdout == f"run mismatch: host .run=deadbeef expected={lab.CONFIG_DIGEST}\n"
    assert (tmp_path / ".run").read_text() == "deadbeef"  # never rewritten on a mismatch


def test_guard_explains_a_scenario_mismatch_before_looking_at_the_run(lab, tmp_path):
    out = run_guard(lab, tmp_path, scenario="other-class", run="deadbeef")
    assert out.returncode == 1
    assert out.stdout == (
        f"scenario mismatch: host .scenario=other-class expected={lab.SCENARIO}\n")


def test_guard_without_legacy_digests_has_no_migration_branch(lab):
    text = lab.run_marker_guard("/srv/x", legacy_digests=(lab.CONFIG_DIGEST, "", None))
    assert "migrated" not in text and lab.CONFIG_DIGEST in text


# ----- refresh_service.py / reset_lab.py end to end -----------------------------------------
def prepared_lab(tmp_path, monkeypatch, *, manifest_model):
    """The real lab under a throwaway config with a stale state_dir copy, a manifest whose
    ``model_id`` is what the host was installed with, and stub ``remote`` / IAM / Lambda."""
    lab = load_lab(tmp_path, monkeypatch)
    lab.ROOT.mkdir(parents=True)
    (lab.ROOT / "lab-assets").mkdir()
    (lab.ROOT / "lab-assets/service.py").write_text("STALE = True\n")
    (lab.ROOT / "lab-assets/lambda_bridge.py").write_text("STALE = True\n")
    manifest = {
        **lab.load(), "model_id": manifest_model, "service_installed": True,
        "service": {"host": lab.CONFIG["tools_host"], "unit": "launchpad-itsm-lab.service",
                    "path": "/srv/launchpad-demo/scenarios/" + lab.SCENARIO},
        "kb": {"data_sources": [{"bucket": "kb-bucket", "prefix": "kb/shared"}]},
    }
    lab.save(manifest)
    lab.remote_calls = []

    def remote(script, label, timeout=300):
        lab.remote_calls.append((label, script))
        return {"CommandId": "cmd", "Status": "Success",
                "StandardOutputContent": 'active\n{"ok":true}\n\nchanged=0\n'}

    monkeypatch.setattr(lab, "remote", remote)
    monkeypatch.setattr(lab, "grant_publish_targets", lambda m, targets, **kw: {
        "role": "launchpad-itsm-workshop-lambda", "endpoint": None, "endpoint_changed": False,
        "targets": sorted(targets)})
    monkeypatch.setattr(lab.AWS, "client", lambda name: pytest.fail(f"unexpected {name}"))
    monkeypatch.setitem(sys.modules, "lab", lab)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return lab


def test_refresh_service_syncs_the_cache_then_guards_with_the_legacy_digests(
        tmp_path, monkeypatch, capsys):
    import runpy

    lab = prepared_lab(tmp_path, monkeypatch, manifest_model="litellm.gpt-oss-120b")
    monkeypatch.setattr(sys, "argv", ["refresh_service.py"])
    runpy.run_path(str(SCRIPTS / "refresh_service.py"), run_name="__main__")
    out = capsys.readouterr().out
    # 1. the stale copy was refreshed from the package before the script read it
    assert "Assets synced from the package" in out and "lab-assets/service.py" in out
    assert (lab.ROOT / "lab-assets/service.py").read_bytes() == (
        SAMPLE / "lab-assets/service.py").read_bytes()
    # 2. the remote script carries the guard, not a bare test, with BOTH legacy digests
    [(label, script)] = lab.remote_calls
    assert label == "service-refresh"
    assert 'test "$(cat' not in script
    assert "run marker migrated" in script and "run mismatch: host .run=" in script
    assert lab.LEGACY_CONFIG_DIGEST in script
    assert lab.legacy_config_digest("litellm.gpt-oss-120b") in script
    assert f"expected={lab.CONFIG_DIGEST}" in script
    assert script.index("RUN_MARKER") < script.index("install -d")  # guard runs first
    # 3. the manifest records the package's service digest, not the stale copy's
    manifest = lab.load()
    assert manifest["service"]["service_sha256"] == lab.hashlib.sha256(
        (SAMPLE / "lab-assets/service.py").read_bytes()).hexdigest()
    assert "Service refreshed for staff accounts: itdesk01" in out


def test_reset_lab_uses_the_same_guard(tmp_path, monkeypatch):
    lab = prepared_lab(tmp_path, monkeypatch, manifest_model="litellm.gpt-oss-120b")
    spec = importlib.util.spec_from_file_location("reset_lab_under_test", SCRIPTS / "reset_lab.py")
    reset_lab = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset_lab)
    reset_lab.reset_tickets(lab.load())
    [(label, script)] = lab.remote_calls
    assert label == "reset-business"
    assert 'test "$(cat' not in script
    assert lab.legacy_config_digest("litellm.gpt-oss-120b") in script
    assert "run marker migrated" in script
    assert script.index("RUN_MARKER") < script.index("systemctl stop")


def test_shutil_copy_of_the_sample_tree_is_not_required_for_the_static_guard():
    """Sanity: the asset readers list names the scripts the ledger blames (N17-obs558)."""
    for name in ("refresh_service.py", "refresh_targets.py", "refresh_datasets.py",
                 "student_kit.py", "agents.py", "reset_student.py", "prepare_console.py"):
        assert name in ASSET_READERS, name
    assert shutil.which("bash"), "the guard tests run the generated shell under bash"
