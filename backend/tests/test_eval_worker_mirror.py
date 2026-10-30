"""N12 E1-3b — the worker ships the backend's engine library byte-for-byte.

The zip carries ``launchpad_eval/<module>.py`` copies of ``app/evaluation/jobs.py``,
``app/evaluation/telemetry.py`` and every ``app/services/strands_eval/*.py``; the ONLY
difference allowed is the documented import rewrite (``app.… → launchpad_eval``), so dev
(in-process executor) and prod (the worker) run identical code."""

import ast
from pathlib import Path

from app.templates import eval_worker as ew

APP = Path(ew.APP_DIR)


def test_every_library_module_is_the_repository_module_modulo_the_import_rewrite():
    files = ew.library_files()
    manifest = ew.library_manifest()
    assert set(files) == set(manifest) | {"launchpad_eval/__init__.py"}
    for rel, source_path in manifest.items():
        shipped = files[rel]
        original = source_path.read_text(encoding="utf-8")
        assert shipped == ew.rewrite_imports(original), rel
        # the rewrite touches import lines only
        changed = [(a, b) for a, b in zip(original.splitlines(), shipped.splitlines(),
                                           strict=True) if a != b]
        assert all(a.startswith("from app.") and b.startswith("from launchpad_eval")
                   for a, b in changed), (rel, changed)
        # no `app.*` import survives (statements, not comments — E2's docstrings mention
        # "imports nothing from app.*")
        tree = ast.parse(shipped)
        app_imports = [
            node for node in ast.walk(tree)
            if (isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "app")
            or (isinstance(node, ast.Import)
                and any(alias.name.split(".")[0] == "app" for alias in node.names))
        ]
        assert app_imports == [], rel


def test_the_whole_strands_eval_package_is_shipped():
    """A module added to services/strands_eval (E2's jobs_online / jobs_recommend /
    jobs_optimize) must join the manifest — the worker cannot import what it lacks."""
    package = {p.name for p in (APP / "services" / "strands_eval").glob("*.py")} - {"__init__.py"}
    shipped = {Path(rel).name for rel in ew.library_manifest()}
    assert package <= shipped, sorted(package - shipped)
    assert {"jobs.py", "telemetry.py"} <= shipped


def test_shipped_modules_compile_and_import_only_each_other_or_third_parties():
    for rel, content in ew.library_files().items():
        compile(content, rel, "exec")
        for line in content.splitlines():
            if line.startswith(("from ", "import ")) and "app" in line.split()[1].split(".")[0]:
                raise AssertionError(f"{rel}: {line}")


def test_release_digest_tracks_every_shipped_byte(monkeypatch):
    before = ew.release()
    assert before["version"] == ew.EVAL_WORKER_VERSION
    assert set(before["files"]) >= set(ew.library_files()) | {"main.py.tmpl", "requirements.txt"}
    assert ew.release_tag(before) == f"{before['version']}-{before['digest'][:12]}"
    original = ew.library_files

    def altered():
        files = dict(original())
        files["launchpad_eval/results.py"] += "\n# changed\n"
        return files

    monkeypatch.setattr(ew, "library_files", altered)
    after = ew.release()
    assert after["digest"] != before["digest"]
    assert ew.release_version(ew.release_tag(after)) == ew.EVAL_WORKER_VERSION


def test_requirements_are_the_plan_pins_and_the_lock_covers_them():
    reqs = ew.base_requirements()
    assert reqs == [
        "strands-agents[otel,openai]==1.47.0",  # N-R6: OpenAIModel for SiliconFlow judges
        "bedrock-agentcore==1.17.*",
        "boto3>=1.43.109,<2",
        "strands-agents-evals==1.5.0",
        "strands-harness-optimizer==0.0.1",
    ]
    lock = (ew.TEMPLATE_DIR / "requirements.lock").read_text(encoding="utf-8")
    header = lock.splitlines()[1]
    assert "--python-platform aarch64-manylinux_2_28" in header
    assert "--python-version 3.13" in header and "--only-binary :all:" in header
    pinned = {line.split("==")[0].strip() for line in lock.splitlines()
              if "==" in line and not line.startswith("#")}
    assert {"strands-agents", "bedrock-agentcore", "boto3", "strands-agents-evals",
            "strands-harness-optimizer"} <= pinned
    assert "strands-agents==1.47.0" in lock and "strands-agents-evals==1.5.0" in lock
    assert "openai==" in lock  # N-R6
    assert "strands-harness-optimizer==0.0.1" in lock and "bedrock-agentcore==1.17." in lock
    assert all("--hash=sha256:" in line for line in lock.splitlines() if "--hash" in line)
    assert len(pinned) >= 80  # 87 wheels verified 2026-10-10 (plan §F)
