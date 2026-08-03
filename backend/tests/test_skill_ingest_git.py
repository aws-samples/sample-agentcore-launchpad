"""Git skill ingestion: shallow-clone acquire against a local repo fixture,
archive fallback when git is missing, token redaction, capability + install
helpers, and the inspect JSON git branch. No network — the clone path uses a
file:// repo built here, the fallback path monkeypatches the downloader."""

import io
import os
import subprocess
import types
import zipfile

import pytest

import app.routers.registry as registry_router
from app.services import skill_ingest as si


def _md(name: str, description: str = "d", version: str = "0.1.0") -> str:
    return f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n# {name}\n"


def _git(cwd, *args) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)


def _make_repo(path, files: dict[str, str]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    for rel, content in files.items():
        p = path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


def _archive_zip(entries: dict[str, str], wrapper: str | None = None) -> bytes:
    """Build an in-memory zip; ``wrapper`` mimics the host archive's repo-ref/ dir."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(f"{wrapper}/{name}" if wrapper else name, content)
    return buf.getvalue()


def _allow_file_scheme(monkeypatch) -> None:
    monkeypatch.setattr(si, "_ALLOWED_GIT_SCHEMES", ("https://", "file://"))


# ---------- clone path (local repo fixture, git present) ----------

def test_git_clone_multi_skill_discovery(tmp_path, monkeypatch):
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "skills/alpha/SKILL.md": _md("alpha"),
        "skills/alpha/scripts/run.py": "print(1)\n",
        "skills/beta/SKILL.md": _md("beta"),
        "README.md": "top-level, not part of any skill\n",
    })
    bundles = si.bundles_from_git(f"file://{repo}")
    try:
        assert sorted(b.name for b in bundles) == ["alpha", "beta"]
        alpha = next(b for b in bundles if b.name == "alpha")
        assert alpha.files == ["SKILL.md", "scripts/run.py"]  # .git dropped, README excluded
        assert alpha.source.kind == "git"
        assert alpha.source.subdir == "skills/alpha"
        assert alpha.source.url == f"file://{repo}"
    finally:
        for b in bundles:
            b.close()


def test_git_clone_records_the_resolved_commit(tmp_path, monkeypatch):
    """A record has to say which revision it carries. `ref` is what was asked for
    and may be a branch; `commit` is what was actually cloned. Read inside
    _git_clone because .git is deleted right after."""
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {"SKILL.md": _md("committed")})
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    bundles = si.bundles_from_git(f"file://{repo}")
    try:
        assert bundles[0].source.commit == head
        assert len(bundles[0].source.commit) == 40
    finally:
        bundles[0].close()


def test_git_clone_commit_differs_between_revisions(tmp_path, monkeypatch):
    """Re-importing after an upstream change has to surface a different commit —
    that is the whole point of recording it."""
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {"SKILL.md": _md("moving", version="1.0.0")})
    first = si.bundles_from_git(f"file://{repo}")
    try:
        commit_one = first[0].source.commit
    finally:
        first[0].close()

    (repo / "SKILL.md").write_text(_md("moving", version="2.0.0"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "bump")

    second = si.bundles_from_git(f"file://{repo}")
    try:
        assert second[0].source.commit != commit_one
        assert second[0].version == "2.0.0"
    finally:
        second[0].close()


def test_a_source_dict_without_a_commit_still_loads(tmp_path, monkeypatch):
    """Records imported before commits were recorded must keep working."""
    legacy = {"kind": "git", "url": "https://github.com/o/r", "ref": "main"}
    source = si.SkillSource(**legacy)
    assert source.commit is None


def test_git_clone_ref_checkout(tmp_path, monkeypatch):
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {"SKILL.md": _md("root-skill", version="1.0.0")})
    _git(repo, "tag", "v1")
    (repo / "SKILL.md").write_text(_md("root-skill", version="2.0.0"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "v2")

    bundles = si.bundles_from_git(f"file://{repo}", ref="v1")
    try:
        assert bundles[0].version == "1.0.0"  # the tagged revision, not HEAD
        assert bundles[0].source.ref == "v1"
        assert bundles[0].source.subdir is None  # skill at repo root
    finally:
        bundles[0].close()


def test_git_clone_subdir_selection(tmp_path, monkeypatch):
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {
        "skills/alpha/SKILL.md": _md("alpha"),
        "skills/beta/SKILL.md": _md("beta"),
    })
    bundles = si.bundles_from_git(f"file://{repo}", subdir="skills/alpha")
    try:
        assert [b.name for b in bundles] == ["alpha"]
    finally:
        bundles[0].close()


def test_git_subdir_traversal_rejected(tmp_path, monkeypatch):
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {"SKILL.md": _md("root-skill")})
    with pytest.raises(si.SkillValidationError, match="subdir"):
        si.bundles_from_git(f"file://{repo}", subdir="../../etc")


def test_git_zero_skills_error(tmp_path, monkeypatch):
    _allow_file_scheme(monkeypatch)
    repo = tmp_path / "repo"
    _make_repo(repo, {"README.md": "no skills here\n"})
    with pytest.raises(si.SkillValidationError, match="no SKILL.md"):
        si.bundles_from_git(f"file://{repo}")


def test_git_rejects_non_https():
    # default guard: only https:// is accepted
    for bad in ("http://github.com/o/r", "git://github.com/o/r", "file:///tmp/x", "ssh://x/y"):
        with pytest.raises(si.SkillValidationError, match="https"):
            si.bundles_from_git(bad)


def test_git_rejects_non_public_host():
    # SSRF parity with the url acquirer: cloud-metadata / loopback hosts refused
    for bad in ("https://169.254.169.254/repo.git", "https://127.0.0.1/o/r"):
        with pytest.raises(si.SkillValidationError):
            si.bundles_from_git(bad)


# ---------- token redaction ----------

def test_git_clone_redacts_token_on_failure(tmp_path, monkeypatch):
    token = "supersecrettoken123"

    def fake_run(cmd, **kwargs):
        assert any(token in str(c) for c in cmd)  # token IS injected into clone URL
        return types.SimpleNamespace(
            returncode=1,
            stderr=f"fatal: unable to access 'https://x-access-token:{token}@github.com/o/r/'",
            stdout="",
        )

    monkeypatch.setattr(si.subprocess, "run", fake_run)
    with pytest.raises(si.SkillValidationError) as exc:
        si._git_clone("https://github.com/o/r", None, token, tmp_path)
    message = str(exc.value)
    assert "***" in message
    assert token not in message


def test_git_clone_redacts_url_embedded_creds(tmp_path, monkeypatch):
    # creds pasted into the URL field (not the token param) must not leak either
    def fake_run(cmd, **kwargs):
        return types.SimpleNamespace(
            returncode=1,
            stderr="fatal: unable to access 'https://alice:hunter2@github.com/o/r/'",
            stdout="",
        )

    monkeypatch.setattr(si.subprocess, "run", fake_run)
    with pytest.raises(si.SkillValidationError) as exc:
        si._git_clone("https://alice:hunter2@github.com/o/r", None, None, tmp_path)
    message = str(exc.value)
    assert "hunter2" not in message
    assert "alice" not in message
    assert "***@github.com" in message


# ---------- archive fallback (git missing) ----------

def test_git_missing_archive_fallback_known_host(monkeypatch):
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    zip_bytes = _archive_zip(
        {"SKILL.md": _md("archived"), "scripts/x.py": "x\n"}, wrapper="repo-main"
    )
    captured: dict = {}

    def fake_download(url, headers, max_bytes=si.SKILL_BUNDLE_MAX_BYTES):
        captured["url"] = url
        captured["headers"] = headers
        return zip_bytes

    monkeypatch.setattr(si, "_download_archive", fake_download)
    # The archive path can only be immutable when the caller names a commit, so a
    # SHA ref is now required here (a branch archive can move under the record).
    bundles = si.bundles_from_git(
        "https://github.com/org/repo", ref="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", token="tkn"
    )
    try:
        assert bundles[0].name == "archived"
        assert bundles[0].files == ["SKILL.md", "scripts/x.py"]  # wrapper dir stripped
        assert bundles[0].source.kind == "git"
        assert bundles[0].source.commit == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        assert captured["url"] == "https://github.com/org/repo/archive/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip"
        assert captured["headers"]["Authorization"] == "Bearer tkn"
    finally:
        bundles[0].close()


def test_git_missing_archive_fallback_gitlab_ref(monkeypatch):
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    captured: dict = {}

    def fake_download(url, headers, max_bytes=si.SKILL_BUNDLE_MAX_BYTES):
        captured["url"] = url
        return _archive_zip({"SKILL.md": _md("x-skill")}, wrapper="repo-v1")

    monkeypatch.setattr(si, "_download_archive", fake_download)
    bundles = si.bundles_from_git(
        "https://gitlab.com/org/repo", ref="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    try:
        assert bundles[0].name == "x-skill"
        assert captured["url"] == (
            "https://gitlab.com/org/repo/-/archive/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/repo-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip"
        )
    finally:
        bundles[0].close()


def test_git_missing_archive_requires_a_commit_ref(monkeypatch):
    """A skill becomes deployable code, so its source must be immutable. The
    archive endpoint can only serve a named ref, so without the git CLI the caller
    has to supply a commit SHA — and is told both ways forward."""
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))

    def boom(url, headers, max_bytes=si.SKILL_BUNDLE_MAX_BYTES):  # pragma: no cover
        raise AssertionError("must not download before the commit check")

    monkeypatch.setattr(si, "_download_archive", boom)
    with pytest.raises(si.SkillValidationError) as exc:
        si.bundles_from_git("https://gitlab.com/org/repo")
    assert "immutable commit" in exc.value.message
    assert "git-install" in exc.value.message
    assert "40-character" in exc.value.message


def test_git_missing_archive_refuses_a_branch_ref(monkeypatch):
    """A branch moves, so it cannot pin what a deployed agent carries."""
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    with pytest.raises(si.SkillValidationError, match="immutable commit"):
        si.bundles_from_git("https://github.com/org/repo", ref="main")


def test_git_missing_archive_fallback_large_repo(monkeypatch):
    """The archive is a WHOLE repo, so the per-skill 200-file cap must not
    apply at extraction — a >200-file monorepo with one small skill in the
    requested subdir imports fine (regression: live anthropics/skills failed)."""
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    files = {f"src/mod{i}/file{i}.py": "x\n" for i in range(250)}
    files["skills/tiny/SKILL.md"] = _md("tiny")
    zip_bytes = _archive_zip(files, wrapper="repo-main")
    monkeypatch.setattr(
        si, "_download_archive",
        lambda url, headers, max_bytes=si.SKILL_BUNDLE_MAX_BYTES: zip_bytes,
    )
    bundles = si.bundles_from_git(
        "https://github.com/org/repo",
        ref="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        subdir="skills/tiny",
    )
    try:
        assert len(bundles) == 1
        assert bundles[0].name == "tiny"
        assert bundles[0].files == ["SKILL.md"]
    finally:
        bundles[0].close()


def test_git_missing_unknown_host_raises(monkeypatch):
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    with pytest.raises(si.GitUnavailableError) as exc:
        si.bundles_from_git("https://git.example.com/org/repo")
    assert exc.value.status_code == 503
    assert exc.value.code == "registry.git_unavailable"
    assert exc.value.detail["host"] == "git.example.com"
    assert "install" in exc.value.detail
    assert "hint" in exc.value.detail["install"]


# ---------- capabilities + install helpers ----------

def test_capabilities_reports_git_missing(monkeypatch):
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (False, None))
    caps = si.git_capabilities()
    assert caps["available"] is False
    assert caps["version"] is None
    assert "github.com" in caps["fallback_hosts"]
    assert set(caps["install"]) == {"auto_installable", "package_manager", "hint"}


def test_install_git_not_auto_installable_does_not_execute(monkeypatch):
    monkeypatch.setattr(si, "_detect_package_manager", lambda: (None, None))

    def fail_if_called(*a, **k):
        raise AssertionError("subprocess.run must not run when not auto-installable")

    monkeypatch.setattr(si.subprocess, "run", fail_if_called)
    res = si.install_git()
    assert res["ok"] is False
    assert res["package_manager"] is None
    assert "hint" in res


def test_install_git_success_path(monkeypatch):
    monkeypatch.setattr(
        si, "_detect_package_manager",
        lambda: ("apt-get", ["apt-get", "install", "-y", "git"]),
    )
    monkeypatch.setattr(si, "_has_root_or_sudo", lambda: True)
    monkeypatch.setattr(si.os, "geteuid", lambda: 0)  # root → no sudo prefix
    calls: dict = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0, stdout="installed", stderr="")

    monkeypatch.setattr(si.subprocess, "run", fake_run)
    monkeypatch.setattr(si, "git_available", lambda refresh=False: (True, "2.44.0"))
    res = si.install_git()
    assert res == {"ok": True, "git_version": "2.44.0"}
    assert calls["cmd"] == ["apt-get", "install", "-y", "git"]


# ---------- router: capabilities endpoint, git-install, inspect JSON branch ----------

def test_capabilities_endpoint_git_present(client):
    res = client.get("/api/registry/skills/capabilities")
    assert res.status_code == 200
    git = res.json()["git"]
    assert git["available"] is True  # git IS installed in this dev env
    assert git["version"]
    assert "github.com" in git["fallback_hosts"]
    assert "install" in git


def test_git_install_endpoint_dispatches(client, monkeypatch):
    monkeypatch.setattr(
        registry_router.si, "install_git",
        lambda: {"ok": False, "hint": "sudo apt-get install -y git", "package_manager": "apt-get"},
    )
    res = client.post("/api/registry/skills/capabilities/git-install")
    assert res.status_code == 200
    assert res.json()["ok"] is False
    assert res.json()["hint"] == "sudo apt-get install -y git"


def test_inspect_git_source_json_branch(client, monkeypatch):
    def fake_bundles_from_git(url, ref=None, subdir=None, token=None):
        assert token == "sekret"  # token flows through but is never stored/logged
        b1 = si.bundle_from_inline(_md("alpha", description="A"))
        b2 = si.bundle_from_inline(_md("beta", description="B"))
        b1.source = si.SkillSource(kind="git", url=url, ref=ref, subdir="skills/alpha")
        b2.source = si.SkillSource(kind="git", url=url, ref=ref, subdir="skills/beta")
        return [b1, b2]

    monkeypatch.setattr(registry_router.si, "bundles_from_git", fake_bundles_from_git)
    res = client.post("/api/registry/skills/inspect", json={
        "source": {"kind": "git", "url": "https://github.com/org/skills",
                   "ref": "main", "token": "sekret"},
    })
    assert res.status_code == 200
    body = res.json()
    assert body["staging_id"]
    assert {s["name"] for s in body["skills"]} == {"alpha", "beta"}
    assert body["skills"][0]["source"]["kind"] == "git"


def test_inspect_unsupported_source_kind_400(client):
    # "url" is a supported kind now (P2); a genuinely unknown kind still 400s.
    res = client.post(
        "/api/registry/skills/inspect", json={"source": {"kind": "svn", "url": "x"}}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "registry.invalid_source"
