"""Which commit this backend / console was deployed from (N13-obs10, E01-obs1).

``GET /api/health`` carries ``build: {sha, short, built_at, source}`` so an operator — or a
demo video's ``recorded_version`` check — can read the deployed build sha off the console
instead of ssh-ing to the host. The information comes, in order, from:

1. ``LAUNCHPAD_BUILD_SHA`` (+ optional ``LAUNCHPAD_BUILD_AT``) in the environment — a
   container / CI override;
2. ``data/build-info.json`` ``{"sha", "built_at", …}`` — written by ``scripts/host-update.sh``
   step 7 on every update (the commit the host was fast-forwarded to, and when);
3. ``frontend/dist/.launchpad-build`` — the dist stamp every build writes (N11-r3-bug8: the
   commit ``dist/`` was built from); ``built_at`` is then the stamp's mtime;
4. nothing: every key is null (a dev checkout that never built the console).

``dist_sha`` (N17-obs466) is a different fact: the commit the SERVED console bundle was built
from — the dist stamp, else ``dist_built_from`` in ``data/build-info.json``, else null (never
the env / HEAD sha). An open tab compares it with the sha its bundle was built with to offer
a reload; ``sha`` is not that, since host-update keeps ``dist/`` when ``frontend/`` did not
change and ``sha`` then moves without the bundle.

Pure file / env reads — no git subprocess on a health probe, no caching (the files are a
few bytes and the console probes every 30 s).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.config import DATA_DIR, REPO_ROOT

BUILD_INFO_FILE = DATA_DIR / "build-info.json"
DIST_STAMP = REPO_ROOT / "frontend" / "dist" / ".launchpad-build"
SHORT_LEN = 8
_SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


def _clean_sha(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return text if _SHA_RE.match(text) else None


def _iso(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _mtime_iso(path: Path) -> str | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat(timespec="seconds")
    except OSError:
        return None


def build_info(*, env: Mapping[str, str] | None = None, info_file: Path | None = None,
               dist_stamp: Path | None = None) -> dict[str, Any]:
    """``{sha, short, built_at, source, dist_sha}`` — ``source`` names which of the four origins
    answered (``env`` | ``file`` | ``dist_stamp`` | None); every key is null when none did.
    The paths default to the module's ``BUILD_INFO_FILE`` / ``DIST_STAMP`` at call time (a
    test points them at a scratch dir)."""
    environ = os.environ if env is None else env
    info_file = BUILD_INFO_FILE if info_file is None else info_file
    dist_stamp = DIST_STAMP if dist_stamp is None else dist_stamp
    try:
        payload = json.loads(info_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        payload = {}
    try:
        stamp_sha = _clean_sha(dist_stamp.read_text(encoding="utf-8"))
    except OSError:
        stamp_sha = None
    dist_sha = stamp_sha or _clean_sha(payload.get("dist_built_from"))
    sha = _clean_sha(environ.get("LAUNCHPAD_BUILD_SHA"))
    if sha:
        return _out(sha, _iso(environ.get("LAUNCHPAD_BUILD_AT")), "env", dist_sha)
    sha = _clean_sha(payload.get("sha"))
    if sha:
        return _out(sha, _iso(payload.get("built_at")), "file", dist_sha)
    if stamp_sha:
        return _out(stamp_sha, _mtime_iso(dist_stamp), "dist_stamp", dist_sha)
    return _out(None, None, None, dist_sha)


def _out(sha: str | None, built_at: str | None, source: str | None,
         dist_sha: str | None) -> dict[str, Any]:
    return {"sha": sha, "short": sha[:SHORT_LEN] if sha else None, "built_at": built_at,
            "source": source, "dist_sha": dist_sha}
