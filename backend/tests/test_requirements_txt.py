"""User requirements.txt handling (T10 hardening): the pip-format parser and
its supply-chain rejections, the resolver-failure summarizer, the configurable
resolution platform, and the upload-time pre-resolve — all hermetic (the
resolver subprocess is stubbed)."""

import subprocess
from types import SimpleNamespace

import pytest

from app.core import runtime_target
from app.core.config import get_settings
from app.services.requirements_txt import (
    MAX_REQUIREMENT_ENTRIES,
    RequirementsFileError,
    parse_requirements_txt,
    pip_python_version,
    preresolve,
    summarize_resolver_failure,
)

# ── parser: the pip requirements-file format ─────────────────────────────────

def test_parser_joins_continuations_and_drops_hashes():
    text = (
        "requests==2.32.3 \\\n"
        "    --hash=sha256:" + "a" * 64 + " \\\n"
        "    --hash=sha256:" + "b" * 64 + "\n"
    )
    assert parse_requirements_txt(text) == ["requests==2.32.3"]


def test_parser_strips_comments_and_blank_lines():
    text = "# header\n\nrequests==2.32.3  # pinned\n   \n# trailer\n"
    assert parse_requirements_txt(text) == ["requests==2.32.3"]


def test_parser_keeps_environment_markers():
    text = 'tomli==2.0.1 ; python_version < "3.11"\n'
    assert parse_requirements_txt(text) == ['tomli==2.0.1 ; python_version < "3.11"']


def test_parser_allows_unpinned_and_extras():
    """BYOC zips are not held to the spec.requirements pinning rule — the
    hashed lock the package stage compiles is what makes the build reproducible."""
    assert parse_requirements_txt("chromadb\nuvicorn[standard]>=0.30\n") == [
        "chromadb", "uvicorn[standard]>=0.30",
    ]


@pytest.mark.parametrize(
    "line,reason",
    [
        ("-r extra.txt", "nested requirements"),
        ("--requirement extra.txt", "nested requirements"),
        ("-c constraints.txt", "constraints files"),
        ("-e .", "editable"),
        ("--editable ./pkg", "editable"),
        ("--index-url https://mirror.example/simple", "own package index"),
        ("--extra-index-url https://mirror.example/simple", "own package index"),
        ("--find-links ./wheels", "own package index"),
        ("--no-index", "own package index"),
        ("https://example.com/pkg.whl", "package index only"),
        ("git+https://github.com/org/repo@main", "package index only"),
        ("pkg @ https://example.com/pkg.whl", "package index only"),
        ("file:./vendored", "package index only"),
        ("./local-dir", "local paths"),
        ("/abs/path", "local paths"),
        ("requests==2.32.3 --global-option=x", "pip options"),
    ],
)
def test_parser_rejects_the_supply_chain_boundary(line, reason):
    with pytest.raises(RequirementsFileError, match=reason):
        parse_requirements_txt(line + "\n")


def test_parser_caps_entry_count():
    text = "\n".join(f"pkg{i}==1.0" for i in range(MAX_REQUIREMENT_ENTRIES + 1))
    with pytest.raises(RequirementsFileError, match="more than"):
        parse_requirements_txt(text)


def test_pip_python_version_shapes():
    assert pip_python_version("PYTHON_3_13") == "3.13"
    assert pip_python_version("3.12") == "3.12"


# ── resolver-failure summaries ───────────────────────────────────────────────

def test_summarizer_names_the_wheelless_package():
    raw = (
        "  × No solution found when resolving dependencies:\n"
        "  ╰─▶ Because google-re2==1.1.20240702 has no usable wheels and you "
        "require google-re2==1.1.20240702, we can conclude that your "
        "requirements are unsatisfiable.\n"
    )
    msg = summarize_resolver_failure(raw)
    assert "google-re2" in msg
    assert "no wheel installable" in msg
    assert "aarch64-manylinux_2_28" in msg  # current target named in the reason
    assert "we can conclude" not in msg  # the derivation tree stays out


def test_summarizer_names_the_missing_package():
    raw = "Because nosuchpkg was not found in the package registry and you require…"
    msg = summarize_resolver_failure(raw)
    assert "nosuchpkg does not exist on the package index" in msg


def test_summarizer_names_the_missing_version():
    raw = "Because there is no version of requests==999.0 and you require requests==999.0"
    msg = summarize_resolver_failure(raw)
    assert "requests has no release matching the requested version" in msg


def test_summarizer_reports_conflicts_without_echoing_requirements():
    raw = (
        "Because pkg-a==1.0 depends on shared<2 and pkg-b==2.0 depends on "
        "shared>=2, we can conclude that your requirements are unsatisfiable."
    )
    msg = summarize_resolver_failure(raw)
    assert "conflict" in msg


def test_summarizer_appends_caller_hints():
    raw = "Because x==1 has no usable wheels …"
    msg = summarize_resolver_failure(raw, hints=("try the container path",))
    assert msg.rstrip(".").endswith("try the container path")


def test_summarizer_bounds_unknown_output():
    msg = summarize_resolver_failure("mystery " * 500)
    assert len(msg) < 600


# ── resolution platform: default + override ──────────────────────────────────

def test_platform_default_is_manylinux_2_28(monkeypatch):
    get_settings.cache_clear()
    try:
        assert runtime_target.uv_platform() == "aarch64-manylinux_2_28"
        platforms = runtime_target.pip_platforms()
        # pip does not widen --platform tags itself, so the ladder must carry
        # every level from the target down to 2014 — else the install refuses
        # the manylinux_2_17 wheels most projects publish.
        assert platforms[0] == "manylinux_2_28_aarch64"
        assert "manylinux_2_17_aarch64" in platforms
        assert platforms[-1] == "manylinux2014_aarch64"
    finally:
        get_settings.cache_clear()


def test_platform_env_override(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_RUNTIME_PYTHON_PLATFORM", "manylinux2014")
    get_settings.cache_clear()
    try:
        assert runtime_target.uv_platform() == "aarch64-manylinux2014"
        assert runtime_target.pip_platforms() == [
            "manylinux_2_17_aarch64", "manylinux2014_aarch64",
        ]
    finally:
        monkeypatch.delenv("LAUNCHPAD_RUNTIME_PYTHON_PLATFORM")
        get_settings.cache_clear()


def test_platform_setting_refuses_non_manylinux(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_RUNTIME_PYTHON_PLATFORM", "macosx_11_0")
    get_settings.cache_clear()
    try:
        with pytest.raises(Exception, match="pattern"):
            get_settings()
    finally:
        monkeypatch.delenv("LAUNCHPAD_RUNTIME_PYTHON_PLATFORM")
        get_settings.cache_clear()


# ── upload-time pre-resolve ──────────────────────────────────────────────────

def _ok_runner(cmd, **_kw):
    from pathlib import Path

    out = Path(cmd[cmd.index("-o") + 1])
    out.write_text("requests==2.32.3\nurllib3==2.2.2\n", encoding="utf-8")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def test_preresolve_ok_counts_locked_packages():
    result = preresolve("requests==2.32.3\n", runner=_ok_runner)
    assert result == {"status": "ok", "package_count": 2, "error": None}


def test_preresolve_ok_on_effectively_empty_file():
    result = preresolve("# nothing but comments\n\n", runner=_ok_runner)
    assert result == {"status": "ok", "package_count": 0, "error": None}


def test_preresolve_failed_carries_the_summary():
    def failing(cmd, **_kw):
        return SimpleNamespace(
            returncode=1, stdout="",
            stderr="Because google-re2==1.0 has no usable wheels …",
        )

    result = preresolve("google-re2==1.0\n", runner=failing)
    assert result["status"] == "failed"
    assert "google-re2" in result["error"]
    assert result["package_count"] is None


def test_preresolve_failed_on_boundary_violation_without_running_uv():
    def never(cmd, **_kw):  # pragma: no cover - must not be reached
        raise AssertionError("resolver must not run for a refused file")

    result = preresolve("-e .\n", runner=never)
    assert result["status"] == "failed"
    assert "editable" in result["error"]


def test_preresolve_skipped_on_timeout():
    def timing_out(cmd, timeout=None, **_kw):
        raise subprocess.TimeoutExpired(cmd, timeout)

    result = preresolve("requests==2.32.3\n", runner=timing_out, timeout_s=5)
    assert result["status"] == "skipped"
    assert "did not finish" in result["error"]


def test_preresolve_skipped_when_uv_is_missing():
    def missing(cmd, **_kw):
        raise FileNotFoundError("uv")

    result = preresolve("requests==2.32.3\n", runner=missing)
    assert result["status"] == "skipped"
    assert "uv" in result["error"]


def test_preresolve_targets_the_requested_python(monkeypatch):
    seen = {}

    def capture(cmd, **_kw):
        seen["cmd"] = cmd
        return _ok_runner(cmd)

    preresolve("requests==2.32.3\n", python_version="3.11", runner=capture)
    cmd = seen["cmd"]
    assert cmd[cmd.index("--python-version") + 1] == "3.11"
    assert cmd[cmd.index("--python-platform") + 1] == "aarch64-manylinux_2_28"
    assert "--only-binary=:all:" in cmd
