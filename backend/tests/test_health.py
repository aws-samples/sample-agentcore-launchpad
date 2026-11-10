"""`GET /api/health` — the liveness probe the console, nginx / NLB and host-update poll,
plus (N13-obs10, E01-obs1) the deployed build: `build: {sha, short, built_at, source}` read
from `LAUNCHPAD_BUILD_SHA`, `data/build-info.json` (host-update step 7) or the dist stamp
`frontend/dist/.launchpad-build` — every key null when none exists, so the shape is stable."""

import json

import pytest

from app.services import build_info as bi

SHA = "a096762d7f3e4c21b9d0e5f6a7b8c9d0e1f2a3b4"
BUILD_KEYS = {"sha", "short", "built_at", "source", "dist_sha"}


def test_health_returns_ok(client):
    res = client.get("/api/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["version"]
    assert body["region"]
    assert set(body["build"]) == BUILD_KEYS  # additive, always present


@pytest.fixture
def stamps(tmp_path, monkeypatch):
    """Point the three origins at a scratch dir (none exists yet) and clear the env."""
    monkeypatch.delenv("LAUNCHPAD_BUILD_SHA", raising=False)
    monkeypatch.delenv("LAUNCHPAD_BUILD_AT", raising=False)
    info = tmp_path / "data" / "build-info.json"
    stamp = tmp_path / "dist" / ".launchpad-build"
    monkeypatch.setattr(bi, "BUILD_INFO_FILE", info)
    monkeypatch.setattr(bi, "DIST_STAMP", stamp)
    return info, stamp


def _build(client) -> dict:
    return client.get("/api/health").json()["build"]


def test_without_any_stamp_every_build_key_is_null(client, stamps):
    assert _build(client) == {"sha": None, "short": None, "built_at": None, "source": None,
                              "dist_sha": None}


def test_the_build_info_file_written_by_host_update_is_served(client, stamps):
    info, _stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"sha": SHA, "built_at": "2026-10-17T18:40:00Z",
                                "branch": "cn/integration", "dist_built_from": SHA}))
    assert _build(client) == {"sha": SHA, "short": SHA[:8],
                              "built_at": "2026-10-18T02:40:00+08:00",  # N17-obs463
                              "source": "file", "dist_sha": SHA}  # the shas untouched


def test_a_dist_stamp_alone_answers_with_its_mtime(client, stamps):
    _info, stamp = stamps
    stamp.parent.mkdir(parents=True)
    stamp.write_text(SHA + "\n")  # host-update writes the sha + newline
    build = _build(client)
    assert build["sha"] == SHA and build["short"] == SHA[:8] and build["source"] == "dist_stamp"
    assert build["built_at"] and build["built_at"].endswith("+08:00")  # N17-obs463


def test_the_env_override_wins_over_the_files(client, stamps, monkeypatch):
    info, stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"sha": "b" * 40, "built_at": "x"}))
    monkeypatch.setenv("LAUNCHPAD_BUILD_SHA", SHA.upper())  # normalised to lower-case
    monkeypatch.setenv("LAUNCHPAD_BUILD_AT", "2026-10-17T19:00:00Z")
    assert _build(client) == {"sha": SHA, "short": SHA[:8],
                              "built_at": "2026-10-18T03:00:00+08:00",  # N17-obs463
                              "source": "env", "dist_sha": None}


@pytest.mark.parametrize("content", ["", "not json", "{}", json.dumps({"sha": "not-a-sha"}),
                                     json.dumps(["a"]), json.dumps({"sha": "abc"})])
def test_a_malformed_or_sha_less_file_falls_through_to_the_dist_stamp(client, stamps, content):
    info, stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(content)
    assert _build(client)["source"] is None  # nothing else exists either
    stamp.parent.mkdir(parents=True)
    stamp.write_text("deadbeef")  # the shortest sha the stamp reader accepts
    assert _build(client)["sha"] == "deadbeef" and _build(client)["source"] == "dist_stamp"


def test_a_garbage_env_sha_is_ignored_not_served(client, stamps, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_BUILD_SHA", "$(whoami)")
    assert _build(client)["sha"] is None


# N17-obs466: `dist_sha` is the commit the SERVED console bundle was built from — what an open
# tab compares its own build sha with to offer a reload. It is NOT `sha` (the commit the host
# runs): host-update keeps dist/ when frontend/ did not change, so after a backend-only update
# `sha` moves and the bundle does not — comparing with `sha` would nag forever and a reload
# would not clear it.
OLD = "c" * 40


def test_dist_sha_is_the_commit_the_kept_dist_was_built_from(client, stamps):
    info, stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"sha": SHA, "built_at": "x", "dist_built_from": OLD}))
    stamp.parent.mkdir(parents=True)
    stamp.write_text(OLD + "\n")
    build = _build(client)
    assert (build["sha"], build["dist_sha"]) == (SHA, OLD)


def test_the_dist_stamp_wins_over_the_recorded_dist_built_from(client, stamps):
    # the stamp is what dist/ on disk was built from (rewritten by every build); the file
    # is the last update's record of it
    info, stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"sha": SHA, "dist_built_from": OLD}))
    stamp.parent.mkdir(parents=True)
    stamp.write_text(SHA + "\n")
    assert _build(client)["dist_sha"] == SHA


def test_without_a_stamp_dist_sha_falls_back_to_dist_built_from(client, stamps):
    info, _stamp = stamps
    info.parent.mkdir(parents=True)
    info.write_text(json.dumps({"sha": SHA, "dist_built_from": OLD}))
    assert _build(client)["dist_sha"] == OLD
    info.write_text(json.dumps({"sha": SHA, "dist_built_from": "$(id)"}))
    assert _build(client)["dist_sha"] is None  # a garbage value is never served


def test_the_env_override_does_not_name_the_served_bundle(client, stamps, monkeypatch):
    _info, stamp = stamps
    monkeypatch.setenv("LAUNCHPAD_BUILD_SHA", SHA)
    assert _build(client)["dist_sha"] is None
    stamp.parent.mkdir(parents=True)
    stamp.write_text(OLD)
    assert _build(client)["dist_sha"] == OLD
