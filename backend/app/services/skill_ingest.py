"""Skill ingestion pipeline — acquire skill bundles from diverse sources and
converge them into one ``SkillBundle`` abstraction the registry console uploads
to S3.

AWS Agent Registry stores skill definition and SKILL.md data in the
``agentSkillsDefinition`` descriptor; Launchpad's Registry adapter exposes the
stable ``skillDefinition`` / ``skillMd`` inline contract to this layer. The
multi-file bundle lives in ``s3://{artifacts_bucket}/skills/{name}/`` and the
deploy-time packager (``deployer/zip_runtime.py``) downloads the whole prefix.
This module is the producer side: every source resolves to a staging directory
and metadata, validated once, before ``registry_console.register_skill_bundle``
uploads the files and creates the record.

The acquirer seam (``bundle_from_*``) is designed so later sources — git shallow
clone, url fetch — plug in as new functions returning the same ``SkillBundle``;
only ``inline`` and ``zip`` are implemented here.
"""

import io
import ipaddress
import logging
import os
import re
import shutil
import socket
import stat
import subprocess
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlparse, urlunparse

import httpx
import yaml

from app.core.errors import AppError

_logger = logging.getLogger("launchpad.registry")

# Shared caps — the single source of truth for skill limits. The deploy-time
# consumer (deployer/zip_runtime.py) imports SKILL_BUNDLE_MAX_BYTES so producer
# and consumer never drift.
SKILL_MD_MAX_BYTES = 102_400  # Registry descriptor data cap
SKILL_BUNDLE_MAX_BYTES = 50 * 1024 * 1024  # per-skill S3 bundle cap
SKILL_FILE_COUNT_MAX = 200
# AWS parses the skillMd data server-side and rejects longer descriptions
# with a post-upload ValidationException (hit live 2026-07-12 on claude-api).
SKILL_DESCRIPTION_MAX_CHARS = 1024
SKILL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

_DEFAULT_VERSION = "0.1.0"
_CHUNK = 1024 * 1024
_EXCERPT_CHARS = 4000

# Repo-scale caps for the git-missing archive fallback: the downloaded archive
# is a WHOLE repository, not one skill bundle, so the per-bundle caps don't
# apply at extraction time (validate_bundle enforces them per skill afterwards,
# exactly like the git-clone path). Zip-bomb protection stays, just repo-sized.
_REPO_FILE_COUNT_MAX = 20_000
_REPO_MAX_BYTES = 500 * 1024 * 1024


class SkillValidationError(AppError):
    """A bundle failed validation (bad content, unsafe archive, oversize …).

    Subclasses AppError so it flows through the standard error envelope as a
    422; callers doing per-item import catch it to report failures individually.
    """

    def __init__(self, message: str, detail: Any = None):
        super().__init__("registry.skill_invalid", message, detail=detail, status_code=422)


class GitUnavailableError(AppError):
    """Raised when a git import needs the ``git`` CLI but it is not installed and
    the host has no archive-download fallback. Carries the install hint so the
    frontend can offer an auto-install action or show the manual command.
    """

    def __init__(self, host: str):
        install = git_install_info()
        super().__init__(
            "registry.git_unavailable",
            f"git is not installed on the server and '{host}' is not a known "
            f"archive-download host — install git to import from it ({install['hint']})",
            detail={"host": host, "install": install},
            status_code=503,
        )


# Hosts whose HTTPS archive endpoint lets us import without the git CLI. Keep in
# sync with _archive_request below and the capabilities.fallback_hosts field.
GIT_ARCHIVE_HOSTS = ("github.com", "gitlab.com", "gitee.com", "bitbucket.org")

# Restricting to https keeps token handling and SSRF surface simple. Tests widen
# this to include file:// so a local repo fixture can exercise the clone path.
_ALLOWED_GIT_SCHEMES = ("https://",)

_COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")

_GIT_CLONE_TIMEOUT_S = 60
_GIT_INSTALL_TIMEOUT_S = 120
_STDERR_TAIL_CHARS = 2000


@dataclass
class SkillSource:
    """Provenance stamped into ``skillDefinition.inlineContent.source``."""

    kind: Literal["inline", "zip", "git", "url"]
    url: str | None = None
    ref: str | None = None
    subdir: str | None = None
    imported_at: str = ""  # ISO8601 UTC, stamped at registration time
    # Full commit SHA the files actually came from. ``ref`` is what the operator
    # asked for and may be a branch, which moves; this is what they got. Skills
    # become deployable code, so without it there is no way to say which revision
    # a deployed agent is carrying. None on records imported before this existed.
    commit: str | None = None


@dataclass
class SkillBundle:
    """A skill staged on local disk plus the metadata the registry needs.

    ``root`` holds ``SKILL.md`` at its top level (after any unwrapping) and any
    supporting files. The owning ``TemporaryDirectory`` is kept alive via
    ``_tmp`` so the staging directory survives between the inspect and import
    requests; ``close()`` releases it.
    """

    root: Path
    name: str
    description: str
    version: str
    files: list[str]  # POSIX-relative paths under root, sorted
    skill_md: str
    source: SkillSource
    _tmp: TemporaryDirectory | None = field(default=None, repr=False, compare=False)

    def close(self) -> None:
        if self._tmp is not None:
            self._tmp.cleanup()
            self._tmp = None


def parse_frontmatter(markdown: str) -> dict[str, Any]:
    """Parse a leading ``---`` YAML frontmatter block; ``{}`` when absent/broken.

    Shared with registry_console (which re-exports it for backward compat).
    """
    if not markdown.startswith("---"):
        return {}
    try:
        _, frontmatter, _ = markdown.split("---", 2)
    except ValueError:
        return {}
    try:
        loaded = yaml.safe_load(frontmatter)
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


# ---------- acquirers (return a bundle whose files are already on disk) ----------

def bundle_from_inline(skill_md: str) -> SkillBundle:
    """A single-file bundle from raw SKILL.md text (the legacy paste path)."""
    tmp = TemporaryDirectory(prefix="skill-inline-")
    root = Path(tmp.name)
    (root / "SKILL.md").write_text(skill_md, encoding="utf-8")
    return _bundle_from_dir(root, SkillSource(kind="inline"), tmp)


def bundle_from_zip(data: bytes) -> SkillBundle:
    """Extract a zip bundle safely, unwrap a single top-level dir, and describe
    it. Raises SkillValidationError on any archive-safety violation (bad zip,
    absolute/traversal paths, symlinks, zip bomb, file-count overflow)."""
    tmp = TemporaryDirectory(prefix="skill-zip-")
    try:
        _extract_zip_safely(data, Path(tmp.name))
        root = _unwrap_single_top_dir(Path(tmp.name))
        return _bundle_from_dir(root, SkillSource(kind="zip"), tmp)
    except BaseException:
        tmp.cleanup()
        raise


def bundles_from_git(
    url: str,
    ref: str | None = None,
    subdir: str | None = None,
    token: str | None = None,
) -> list[SkillBundle]:
    """Acquire one or more skill bundles from an https git repository.

    When the ``git`` CLI is present, shallow-clones the repo (``--depth 1``,
    optional ``--branch ref``, ``GIT_TERMINAL_PROMPT=0``, 60s timeout); an
    optional token is injected into the clone URL as
    ``https://x-access-token:{token}@…`` and redacted from every error/log.
    When git is missing, falls back to downloading the host's HTTPS archive zip
    for the known hosts in :data:`GIT_ARCHIVE_HOSTS` (token via Authorization
    header); any other host raises :class:`GitUnavailableError`.

    The materialized tree is scanned (after applying ``subdir``) for every
    ``SKILL.md``; each hit becomes a bundle rooted at that file's directory, so a
    monorepo yields multiple bundles. All bundles share the one staging dir —
    only the first owns it, so ``close()`` on it tears down the whole tree once.
    """
    if not url or not str(url).startswith(_ALLOWED_GIT_SCHEMES):
        raise SkillValidationError("git import requires an https:// repository URL")
    # SSRF parity with the url/archive paths; file:// (tests-only scheme) has no
    # host to judge, so the guard applies to real https URLs only.
    if str(url).startswith("https://"):
        _assert_public_url(url)

    tmp = TemporaryDirectory(prefix="skill-git-")
    try:
        available, _ = git_available()
        if available:
            commit = _git_clone(url, ref, token, Path(tmp.name))
            base = Path(tmp.name)
        else:
            base = _archive_fallback(url, ref, token, Path(tmp.name))
            commit = ref  # guaranteed to be a SHA by _archive_fallback
        return _scan_git_bundles(base, tmp, url, ref, subdir, commit)
    except BaseException:
        tmp.cleanup()
        raise


def bundle_from_url(url: str) -> SkillBundle:
    """Acquire a single skill bundle from a direct https URL.

    The body is downloaded with a 60s timeout and the per-skill 50MB cap (this
    is one skill bundle, not a repo). A zip response — detected by content-type,
    a ``.zip`` path, or the ``PK\\x03\\x04`` magic — goes through the same safe
    extractor as an upload; anything else is treated as raw ``SKILL.md`` text
    (utf-8; an undecodable body is rejected). The stored source is stamped
    ``kind="url"`` regardless of which branch produced the files.
    """
    if not url or not url.startswith("https://"):
        raise SkillValidationError("url import requires an https:// URL")
    data, content_type = _fetch_url(url, max_bytes=SKILL_BUNDLE_MAX_BYTES)
    if _looks_like_zip(url, content_type, data):
        bundle = bundle_from_zip(data)  # per-skill caps apply (it IS one bundle)
    else:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SkillValidationError(
                "URL body is neither a zip archive nor valid UTF-8 SKILL.md text"
            ) from exc
        bundle = bundle_from_inline(text)
    bundle.source = SkillSource(kind="url", url=url)
    return bundle


def bundle_from_source(source: dict[str, Any]) -> SkillBundle:
    """Reconstruct a single owned bundle from a stored ``skillDefinition.source``
    dict (reimport path). ``url`` sources fetch afresh; ``git`` sources re-clone
    and, because ``source.subdir`` pins the recorded skill's directory in a
    monorepo, resolve to exactly one bundle (the one rooted at that subdir)."""
    kind = source.get("kind")
    url = source.get("url") or ""
    if kind == "url":
        return bundle_from_url(url)
    if kind == "git":
        subdir = source.get("subdir") or None
        bundles = bundles_from_git(url=url, ref=source.get("ref") or None, subdir=subdir)
        return _single_bundle(bundles, subdir)
    raise SkillValidationError(f"cannot reimport a '{kind}' source")


def _single_bundle(bundles: list[SkillBundle], subdir: str | None) -> SkillBundle:
    """Pick the one bundle a reimport expects. Zero → error (defensive; the
    scanner normally raises first). Several → the one rooted at the recorded
    subdir wins; staging-dir ownership is moved onto it so ``close()`` still
    tears the whole shared tree down exactly once."""
    if not bundles:
        raise SkillValidationError("no skill found at the recorded source")
    if len(bundles) == 1:
        return bundles[0]
    chosen = next((b for b in bundles if (b.source.subdir or None) == subdir), None)
    if chosen is None:
        for b in bundles:
            b.close()
        raise SkillValidationError(
            "recorded source resolves to multiple skills — cannot reimport unambiguously"
        )
    for b in bundles:  # transfer staging ownership onto the chosen bundle
        if b is not chosen and b._tmp is not None:
            chosen._tmp, b._tmp = b._tmp, None
    return chosen


def _looks_like_zip(url: str, content_type: str, data: bytes) -> bool:
    ct = (content_type or "").lower()
    if "application/zip" in ct or "application/x-zip" in ct:
        return True
    if urlparse(url).path.lower().endswith(".zip"):
        return True
    return data[:4] == b"PK\x03\x04"


# ---------- validation ----------

def bundle_errors(bundle: SkillBundle) -> list[str]:
    """Content-level validation errors (empty list = valid). Used by inspect to
    report per-skill status; ``validate_bundle`` raises on the same set."""
    errors: list[str] = []
    skill_md_path = bundle.root / "SKILL.md"
    if not skill_md_path.is_file():
        errors.append("missing SKILL.md at bundle root")
    else:
        md_bytes = skill_md_path.stat().st_size
        if md_bytes > SKILL_MD_MAX_BYTES:
            errors.append(
                f"SKILL.md is {md_bytes} bytes — exceeds the {SKILL_MD_MAX_BYTES} byte limit"
            )
    if len(bundle.description) > SKILL_DESCRIPTION_MAX_CHARS:
        errors.append(
            f"frontmatter description is {len(bundle.description)} chars — AWS rejects "
            f"skillMd descriptions over {SKILL_DESCRIPTION_MAX_CHARS} chars"
        )
    if len(bundle.files) > SKILL_FILE_COUNT_MAX:
        errors.append(f"{len(bundle.files)} files — exceeds the {SKILL_FILE_COUNT_MAX} file limit")
    total = _dir_size(bundle.root)
    if total > SKILL_BUNDLE_MAX_BYTES:
        errors.append(f"bundle is {total} bytes — exceeds the {SKILL_BUNDLE_MAX_BYTES} byte limit")
    return errors


def validate_bundle(bundle: SkillBundle) -> None:
    """Raise SkillValidationError if the bundle is not importable."""
    errors = bundle_errors(bundle)
    if errors:
        raise SkillValidationError("; ".join(errors), detail=errors)


# ---------- internals ----------

def _bundle_from_dir(
    root: Path, source: SkillSource, tmp: TemporaryDirectory | None
) -> SkillBundle:
    files = sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )
    skill_md_path = root / "SKILL.md"
    skill_md = (
        skill_md_path.read_text(encoding="utf-8", errors="replace")
        if skill_md_path.is_file()
        else ""
    )
    meta = parse_frontmatter(skill_md)
    return SkillBundle(
        root=root,
        name=str(meta.get("name", "") or "").strip(),
        description=str(meta.get("description", "") or "").strip(),
        version=str(meta.get("version", "") or "").strip() or _DEFAULT_VERSION,
        files=files,
        skill_md=skill_md,
        source=source,
        _tmp=tmp,
    )


def _unwrap_single_top_dir(root: Path) -> Path:
    """Descend into a lone wrapper directory (e.g. GitHub archive ``repo-ref/``)
    when the root has no SKILL.md but its single top-level entry is a directory
    that does."""
    if (root / "SKILL.md").is_file():
        return root
    entries = list(root.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / "SKILL.md").is_file():
        return entries[0]
    return root


def _extract_zip_safely(
    data: bytes,
    dest: Path,
    *,
    max_files: int | None = None,
    max_bytes: int | None = None,
) -> None:
    """Extract every file entry into ``dest`` while enforcing archive safety:
    reject symlinks, absolute paths, and ``..`` traversal; cap file count; and
    abort mid-stream once actually-decompressed bytes exceed ``max_bytes``
    (defends against a zip bomb that lies about its declared sizes). Defaults
    are the per-skill bundle caps; the repo archive fallback passes repo-scale
    caps since per-skill limits are enforced later by validate_bundle."""
    if max_files is None:
        max_files = SKILL_FILE_COUNT_MAX
    if max_bytes is None:
        max_bytes = SKILL_BUNDLE_MAX_BYTES
    dest_root = dest.resolve()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise SkillValidationError("uploaded file is not a valid zip archive") from exc

    total = 0
    count = 0
    with archive as zf:
        for info in zf.infolist():
            name = info.filename
            if _is_symlink(info):
                raise SkillValidationError(f"zip entry '{name}' is a symlink — refused")
            _reject_unsafe_name(name)
            if info.is_dir():
                continue
            target = (dest / name).resolve()
            if not _is_within(target, dest_root):
                raise SkillValidationError(f"zip entry '{name}' escapes the bundle root")
            count += 1
            if count > max_files:
                raise SkillValidationError(f"zip has more than {max_files} files")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                while True:
                    chunk = src.read(_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise SkillValidationError(
                            f"bundle exceeds the {max_bytes} byte uncompressed limit"
                        )
                    out.write(chunk)


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _reject_unsafe_name(name: str) -> None:
    norm = name.replace("\\", "/")
    if norm.startswith("/"):
        raise SkillValidationError(f"zip entry '{name}' uses an absolute path")
    if len(norm) >= 2 and norm[1] == ":":  # Windows drive letter (C:/...)
        raise SkillValidationError(f"zip entry '{name}' uses an absolute path")
    if ".." in PurePosixPath(norm).parts:
        raise SkillValidationError(f"zip entry '{name}' escapes the bundle root")


def _is_within(target: Path, root: Path) -> bool:
    return target == root or root in target.parents


def _dir_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file())


# ---------- git acquirer internals ----------

def _git_clone(url: str, ref: str | None, token: str | None, dst: Path) -> str | None:
    """Shallow-clone into ``dst`` (already an empty dir) and drop ``.git``.

    Returns the resolved commit SHA. It has to be read here: ``.git`` is removed at
    the end of this function, and that is the only place the SHA exists.

    Never prompts (``GIT_TERMINAL_PROMPT=0``); a non-zero exit or timeout raises
    SkillValidationError with the token redacted from the message."""
    clone_url = _inject_token(url, token)
    cmd = ["git", "clone", "--depth", "1"]
    if ref:
        cmd += ["--branch", ref]
    # '--' ends git option parsing so a crafted url can never be read as a git
    # option (url is https-validated already; ref is consumed as --branch's
    # value regardless of leading dashes — this is belt-and-suspenders).
    cmd += ["--", clone_url, str(dst)]
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    try:
        # URL/ref remain single argv values and '--' terminates git option parsing.
        proc = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd, capture_output=True, text=True, timeout=_GIT_CLONE_TIMEOUT_S, env=env
        )
    except subprocess.TimeoutExpired:
        raise SkillValidationError(
            f"git clone timed out after {_GIT_CLONE_TIMEOUT_S}s"
        ) from None
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise SkillValidationError(f"git clone failed: {_redact(detail, token)}")
    commit = _git_head_sha(dst)
    shutil.rmtree(dst / ".git", ignore_errors=True)
    return commit


def _git_head_sha(repo: Path) -> str | None:
    """`git rev-parse HEAD` in a freshly cloned tree.

    Best-effort: a missing SHA degrades the record to ref-only provenance rather
    than failing an otherwise good import. Callers that need immutability check the
    result.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and _COMMIT_SHA_RE.match(sha) else None


def _archive_fallback(url: str, ref: str | None, token: str | None, dst: Path) -> Path:
    """git-missing path: download the host archive zip and extract into ``dst``.
    Returns the tree root (the archive's single wrapper dir stripped). Raises
    GitUnavailableError for hosts without a known archive endpoint."""
    host, owner, repo = _parse_repo(url)
    if host not in GIT_ARCHIVE_HOSTS:
        raise GitUnavailableError(host)
    # A skill becomes deployable code, so its source has to be immutable. This
    # endpoint serves `…/zip/<ref>` and the only place a SHA could surface is the
    # wrapper directory name, which is `repo-<branch>` for a branch — so it can
    # only be immutable when the caller already named a commit. Checked after the
    # host check so an unsupported host still reports the more fundamental problem.
    if not (ref and _COMMIT_SHA_RE.match(ref)):
        raise SkillValidationError(
            "a git import must resolve to an immutable commit. Without the git CLI "
            "this host can only download an archive of a named ref "
            f"({ref or 'the default branch'}), which can move. Install git on the "
            "server (POST /api/registry/skills/capabilities/git-install), or pass a "
            "full 40-character commit SHA as the ref."
        )
    archive_url, headers = _archive_request(host, owner, repo, ref, token)
    # The archive is a WHOLE repo, so cap the download at repo scale too — using
    # the per-skill 50MB cap here would reject large monorepos before extraction
    # even though extraction allows repo scale (mirrors the per-skill/repo split).
    try:
        data = _download_archive(archive_url, headers, max_bytes=_REPO_MAX_BYTES)
    except SkillValidationError as exc:
        # HEAD.zip resolves the default branch reliably only on github; elsewhere
        # a missing ref is the likely cause of a download failure — say so.
        if ref is None and host != "github.com":
            raise SkillValidationError(
                f"{exc.message} — for {host} without the git CLI, specify a "
                "branch or tag (ref)"
            ) from exc
        raise
    _extract_zip_safely(data, dst, max_files=_REPO_FILE_COUNT_MAX, max_bytes=_REPO_MAX_BYTES)
    return _strip_wrapper_dir(dst)


def _scan_git_bundles(
    base: Path,
    tmp: TemporaryDirectory,
    url: str,
    ref: str | None,
    subdir: str | None,
    commit: str | None = None,
) -> list[SkillBundle]:
    """Find every SKILL.md under ``base`` (after applying ``subdir``) and build a
    bundle per hit. Only the first bundle owns ``tmp`` so the shared staging dir
    is cleaned up exactly once. Zero hits → SkillValidationError."""
    base_root = base.resolve()
    scan_root = base_root
    if subdir:
        scan_root = (base / subdir).resolve()
        if not _is_within(scan_root, base_root) or not scan_root.is_dir():
            raise SkillValidationError(f"subdir '{subdir}' not found in the repository")

    hits = sorted(p for p in scan_root.rglob("SKILL.md") if p.is_file())
    if not hits:
        where = f" under '{subdir}'" if subdir else ""
        raise SkillValidationError(f"no SKILL.md found in the repository{where}")

    redacted_url = _redact_url(url)
    bundles: list[SkillBundle] = []
    for i, md_path in enumerate(hits):
        root = md_path.parent
        rel = root.relative_to(base_root).as_posix()
        source = SkillSource(
            kind="git",
            url=redacted_url,
            ref=ref,
            subdir=None if rel == "." else rel,
            commit=commit,
        )
        bundles.append(_bundle_from_dir(root, source, tmp if i == 0 else None))
    return bundles


def _strip_wrapper_dir(base: Path) -> Path:
    """A host archive wraps everything in a single ``repo-ref/`` dir; descend into
    it so subdir/scan see repo contents at top level."""
    entries = list(base.iterdir())
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return base


def _parse_repo(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2:
        raise SkillValidationError("cannot parse owner/repo from the git URL")
    owner, repo = parts[0], parts[1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    return host, owner, repo


def _archive_request(
    host: str, owner: str, repo: str, ref: str | None, token: str | None
) -> tuple[str, dict[str, str]]:
    """Build the archive zip URL + headers per host. ``HEAD`` resolves to the
    default branch on every supported host."""
    r = ref or "HEAD"
    if host == "github.com":
        url = f"https://github.com/{owner}/{repo}/archive/{r}.zip"
    elif host in ("gitlab.com", "gitee.com"):
        url = f"https://{host}/{owner}/{repo}/-/archive/{r}/{repo}-{r}.zip"
    elif host == "bitbucket.org":
        url = f"https://bitbucket.org/{owner}/{repo}/get/{r}.zip"
    else:  # unreachable: _archive_fallback filters unknown hosts first
        raise GitUnavailableError(host)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return url, headers


_BLOCKED_URL_HOSTS = ("localhost", "metadata.google.internal")


def _assert_public_url(url: str) -> None:
    """SSRF guard for the url acquirer + archive fallback: refuse non-web schemes
    and hosts that resolve to a non-public address (loopback, RFC1918 private,
    link-local incl. the 169.254.169.254 cloud-metadata endpoint, reserved,
    multicast). The host is *resolved* and the ACTUAL IPs judged so decimal/hex/
    IPv6 encodings and DNS names pointing at private ranges can't slip through.
    Mirrors ``routers/tools._validate_demo_url``. Applied on the initial request
    and every redirect hop (see :class:`_GuardingTransport`); not a complete
    defense (a DNS-rebinding TOCTOU window remains) — proportionate for a single
    -tenant lab tool."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise SkillValidationError(f"refusing to fetch a non-web URL: {url}")
    if host in _BLOCKED_URL_HOSTS or host.endswith(".internal"):
        raise SkillValidationError(f"refusing to fetch from an internal host: {host}")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    except (OSError, ValueError):
        addresses = []  # unresolvable — let the download itself fail
    if any(
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
        for ip in addresses
    ):
        raise SkillValidationError(
            f"refusing to fetch from a non-public address (host '{host}')"
        )


class _GuardingTransport(httpx.BaseTransport):
    """Wraps the default transport so :func:`_assert_public_url` runs on the
    initial request AND every redirect hop — httpx routes each hop through the
    transport, so this closes the redirect-based SSRF that a one-shot pre-check
    would miss, while keeping ``follow_redirects=True`` (the github→codeload
    archive redirect still works)."""

    def __init__(self, inner: httpx.BaseTransport) -> None:
        self._inner = inner

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        _assert_public_url(str(request.url))
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


def _stream_download(
    url: str, headers: dict[str, str], max_bytes: int
) -> tuple[bytes, str]:
    """Stream a GET with a 60s timeout, aborting once ``max_bytes`` bytes have
    arrived (streamed cap, not post-hoc). Every request hop is SSRF-guarded via
    the guarding transport. Returns (body, content-type)."""
    buf = bytearray()
    content_type = ""
    try:
        transport = _GuardingTransport(httpx.HTTPTransport())
        with httpx.Client(
            timeout=60.0, follow_redirects=True, transport=transport
        ) as client, client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "")
            for chunk in resp.iter_bytes(_CHUNK):
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise SkillValidationError(
                        f"download exceeds the {max_bytes} byte limit"
                    )
    except httpx.HTTPError as exc:
        raise SkillValidationError(f"failed to download: {exc}") from exc
    return bytes(buf), content_type


def _download_archive(
    url: str, headers: dict[str, str], max_bytes: int = SKILL_BUNDLE_MAX_BYTES
) -> bytes:
    """Download a repo archive zip; content-type is irrelevant here (always a
    zip), so only the bytes are returned."""
    data, _ = _stream_download(url, headers, max_bytes)
    return data


def _fetch_url(url: str, max_bytes: int = SKILL_BUNDLE_MAX_BYTES) -> tuple[bytes, str]:
    """Downloader seam for the url acquirer — returns (body, content-type) so
    ``bundle_from_url`` can tell a zip archive from a raw SKILL.md."""
    return _stream_download(url, {}, max_bytes)


def _inject_token(url: str, token: str | None) -> str:
    if not token:
        return url
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=f"x-access-token:{token}@{host}"))


_USERINFO_RE = re.compile(r"://[^/\s@]+@")


def _redact(text: str, token: str | None) -> str:
    """Mask the injected token AND any ``scheme://user:pass@`` credentials that a
    user may have pasted into the URL field, so neither ends up in an error line
    (git usually masks the password itself, but not every version/host does)."""
    if not text:
        return text
    if token:
        text = text.replace(token, "***")
    return _USERINFO_RE.sub("://***@", text)


def _redact_url(url: str) -> str:
    """Strip any embedded userinfo (defensive — the token is passed separately,
    not in the URL) so no credential is stamped into the stored source."""
    parsed = urlparse(url)
    if parsed.username or parsed.password:
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        parsed = parsed._replace(netloc=host)
    return urlunparse(parsed)


# ---------- git environment: probe, capabilities, install ----------

# Cached git probe; None until first probed. install_git() invalidates it after a
# successful install so the next capabilities read reflects the new binary.
_git_probe: dict[str, Any] | None = None

_PACKAGE_MANAGERS: tuple[tuple[str, list[str]], ...] = (
    ("apt-get", ["apt-get", "install", "-y", "git"]),
    ("dnf", ["dnf", "install", "-y", "git"]),
    ("yum", ["yum", "install", "-y", "git"]),
    ("apk", ["apk", "add", "git"]),
    ("brew", ["brew", "install", "git"]),
)


def git_available(refresh: bool = False) -> tuple[bool, str | None]:
    """(available, version) for the ``git`` CLI, memoized. ``version`` is the
    numeric part of ``git --version`` (e.g. ``"2.43.0"``) or None."""
    global _git_probe
    if _git_probe is None or refresh:
        path = shutil.which("git")
        version: str | None = None
        if path:
            try:
                # shutil.which supplies the executable; no command shell is involved.
                out = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                    [path, "--version"], capture_output=True, text=True, timeout=10
                )
                raw = (out.stdout or "").strip()
                version = raw.replace("git version ", "").strip() or None
            except (OSError, subprocess.SubprocessError):
                version = None
        _git_probe = {"available": bool(path), "version": version}
    return _git_probe["available"], _git_probe["version"]


def invalidate_git_probe() -> None:
    global _git_probe
    _git_probe = None


def _detect_package_manager() -> tuple[str | None, list[str] | None]:
    for name, args in _PACKAGE_MANAGERS:
        if shutil.which(name):
            return name, args
    return None, None


def _has_root_or_sudo() -> bool:
    if getattr(os, "geteuid", lambda: 1)() == 0:
        return True
    if not shutil.which("sudo"):
        return False
    try:
        return subprocess.run(
            ["sudo", "-n", "true"], capture_output=True, timeout=10
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _install_hint(pm: str | None, needs_root: bool) -> str:
    if pm is None:
        return (
            "no supported package manager (apt-get/dnf/yum/apk/brew) found — "
            "install git manually: https://git-scm.com/downloads"
        )
    args = dict(_PACKAGE_MANAGERS)[pm]
    cmd = " ".join(args)
    return f"sudo {cmd}" if needs_root else cmd


def git_install_info() -> dict[str, Any]:
    """The ``install`` sub-object shared by capabilities and GitUnavailableError.
    ``auto_installable`` is true only when a package manager is present AND we
    have the privilege to use it (root, passwordless sudo, or brew which needs
    neither)."""
    pm, _ = _detect_package_manager()
    needs_root = pm not in (None, "brew")
    has_priv = _has_root_or_sudo() if needs_root else pm is not None
    return {
        "auto_installable": pm is not None and has_priv,
        "package_manager": pm,
        "hint": _install_hint(pm, needs_root),
    }


def git_capabilities() -> dict[str, Any]:
    available, version = git_available()
    return {
        "available": available,
        "version": version,
        "fallback_hosts": list(GIT_ARCHIVE_HOSTS),
        "install": git_install_info(),
    }


def install_git() -> dict[str, Any]:
    """Explicit, user-triggered best-effort install via the detected package
    manager (non-interactive, ``sudo -n`` when not root, 120s timeout). Never
    attempts when not auto_installable — returns the manual hint instead. On
    success invalidates the probe cache and returns the new version."""
    info = git_install_info()
    pm = info["package_manager"]
    if not info["auto_installable"]:
        _logger.info("git-install requested but not auto-installable (pm=%s)", pm)
        return {"ok": False, "hint": info["hint"], "package_manager": pm}

    args = dict(_PACKAGE_MANAGERS)[pm]
    is_root = getattr(os, "geteuid", lambda: 1)() == 0
    cmd = args if (pm == "brew" or is_root) else ["sudo", "-n", *args]
    _logger.info("git-install invoked: %s", " ".join(cmd))
    try:
        # cmd comes from the fixed package-manager table; no shell is involved.
        proc = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
            cmd, capture_output=True, text=True, timeout=_GIT_INSTALL_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"install timed out after {_GIT_INSTALL_TIMEOUT_S}s",
                "hint": info["hint"]}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)[-_STDERR_TAIL_CHARS:], "hint": info["hint"]}

    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-_STDERR_TAIL_CHARS:]
        _logger.warning("git-install failed (rc=%s)", proc.returncode)
        return {"ok": False, "error": tail, "hint": info["hint"]}

    available, version = git_available(refresh=True)
    if not available:
        tail = (proc.stdout or "").strip()[-_STDERR_TAIL_CHARS:]
        return {"ok": False, "error": tail or "git still not found after install",
                "hint": info["hint"]}
    _logger.info("git-install succeeded: git %s", version)
    return {"ok": True, "git_version": version}
