# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E22 Video library — roles × routes, publish / withdraw idempotence, chapter boundaries
(round 16, r16-g; `docs/cn/plans/BUG-DISCOVERY-PLAN.md` §E22: "navigation / redirect matrix
(role × old / new path × deep-link params)"). The page-side half of the matrix (redirects,
tabs, the forbidden card, the player's chapter seek) is
`frontend/src/v2/pages/videos/routing-matrix.test.tsx`; this file is the API half.

Contract sources — `docs/cn/acceptance/E22-video-library.md`:

* §1 Routes: `GET /api/videos` = MEMBER (published snapshots only); every `/manage*` route
  = ADMIN. The role table is `core/route_policy.py`, so the matrix is derived from it, not
  typed again (NC-E22-01: member 403 / anonymous 401).
* AC-E22-02/04/07/09: create → `draft`, `revision 1`, `published_content null`, member
  catalog unchanged; publish → `published_content == content`; edit after publish →
  `has_unpublished_changes true`, member catalog unchanged until the next publish; withdraw
  → `draft`, `published_at null`, the member catalog drops the id.
* AC-E22-05: the public shape `{id, consoleVersion, title, description, publishedAt,
  durationSeconds, language, posterUrl, sources, captions, chapters}` — no `revision` /
  `created_by` / draft fields.
* NC-E22-03/06: an invalid body is `422 validation.invalid_request` and **no draft is
  created**; "Chapter offsets must increase within the video duration" (`startSeconds ≥
  duration` or non-increasing rejected); 101 chapters rejected.
* NC-E22-02: a stale `expected_revision` is `409 videos.conflict`, the published snapshot
  never changes from a stale write.

Oracles: the route table (roles), a model of the row (idempotence), and the request body
itself (round trip through the managed and public projections).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.route_policy import ADMIN, ROUTE_POLICY
from tests.test_videos import CONTENT
from tests.test_visibility import PEER, _member, admin, app  # noqa: F401 — fixtures by name

PUBLIC_KEYS = {"id", "consoleVersion", "title", "description", "publishedAt", "durationSeconds",
               "language", "posterUrl", "sources", "captions", "chapters"}
MANAGED_KEYS = {"id", "content", "published_content", "status", "has_unpublished_changes",
                "revision", "created_by", "updated_by", "created_at", "updated_at",
                "published_at"}
VIDEO_ROUTES = sorted(k for k in ROUTE_POLICY if k[1].startswith("/api/videos"))


# ─── roles × routes, derived from the policy table ────────────────────────────


def _request(client: TestClient, method: str, path: str, video_id: str, revision: int):
    url = path.replace("{video_id}", video_id)
    if method == "DELETE":
        return client.delete(url, params={"expected_revision": revision})
    if method == "PUT":
        return client.put(url, json={**CONTENT, "expected_revision": revision})
    if method == "POST" and path.endswith(("/publish", "/unpublish")):
        return client.post(url, json={"expected_revision": revision})
    if method == "POST":
        return client.post(url, json=CONTENT)
    return client.get(url)


def test_every_video_route_answers_each_role_as_the_policy_table_says(app, admin):
    """NC-E22-01 as a full matrix: anonymous 401 everywhere; a member 403 on every ADMIN
    route and never on a MEMBER one; an administrator never 401 / 403. Reads and refused
    writes run first so the seeded row's revision is stable; the admin's writes run last."""
    _, member = _member(app, "viewer")
    anon = TestClient(app, client=PEER)
    row = admin.post("/api/videos/manage", json=CONTENT).json()
    vid, rev = row["id"], row["revision"]
    for method, path in VIDEO_ROUTES:
        role = ROUTE_POLICY[(method, path)]
        res = anon.request(method, path.replace("{video_id}", vid))
        assert res.status_code == 401 and res.json()["code"] == "auth.required", (method, path)
        res = _request(member, method, path, vid, rev)
        if role == ADMIN:
            assert res.status_code == 403, (method, path, res.text)
            assert res.json()["code"] == "auth.forbidden"
        else:
            assert res.status_code == 200, (method, path, res.text)
    # the refused writes changed nothing
    assert admin.get(f"/api/videos/manage/{vid}").json()["revision"] == rev
    # administrator: every route reachable (writes in lifecycle order, revision carried)
    for method, path in [("GET", "/api/videos/manage"), ("GET", "/api/videos/manage/{video_id}"),
                         ("PUT", "/api/videos/manage/{video_id}"),
                         ("POST", "/api/videos/manage/{video_id}/publish"),
                         ("POST", "/api/videos/manage/{video_id}/unpublish"),
                         ("DELETE", "/api/videos/manage/{video_id}")]:
        res = _request(admin, method, path, vid, rev)
        assert res.status_code == 200, (method, path, res.text)
        rev = res.json().get("revision", rev)
    created = _request(admin, "POST", "/api/videos/manage", vid, rev)
    assert created.status_code == 201
    assert set(dict.fromkeys(m for m, _ in VIDEO_ROUTES)) == {"GET", "POST", "PUT", "DELETE"}


# ─── publish / unpublish / republish idempotence ─────────────────────────────


def _snapshot(row: dict[str, Any]) -> tuple:
    """What a reader sees of the row, minus the write bookkeeping."""
    return (row["status"], row["content"], row["published_content"], row["has_unpublished_changes"])


def _public_ids(client) -> list[str]:
    return [v["id"] for v in client.get("/api/videos").json()["videos"]]


ACTIONS = st.lists(st.sampled_from(["publish", "unpublish", "save", "publish", "publish"]),
                   min_size=1, max_size=10)


@settings(max_examples=40, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(actions=ACTIONS, sort_orders=st.lists(st.integers(0, 1_000_000), min_size=10, max_size=10))
def test_publish_withdraw_republish_sequences_keep_the_projections_consistent(
    client, actions, sort_orders,
):
    """Model: `content` (draft), `published` (snapshot or None). publish copies the draft;
    a second publish of an unchanged draft is a no-op for every reader (idempotent);
    unpublish twice stays withdrawn; the member catalog lists the id iff published, with
    the snapshot's fields."""
    row = client.post("/api/videos/manage", json=CONTENT).json()
    vid = row["id"]
    model = {"content": row["content"], "published": None}
    try:
        for step, action in enumerate(actions):
            rev = row["revision"]
            if action == "save":
                body = {**CONTENT, "sort_order": sort_orders[step], "expected_revision": rev}
                res = client.put(f"/api/videos/manage/{vid}", json=body)
            else:
                res = client.post(f"/api/videos/manage/{vid}/{action}",
                                  json={"expected_revision": rev})
            assert res.status_code == 200, (action, res.text)
            before = dict(row)
            row = res.json()
            assert set(row) == MANAGED_KEYS
            assert row["revision"] == rev + 1
            if action == "save":
                model["content"] = row["content"]
                assert row["content"]["sort_order"] == sort_orders[step]
                assert row["published_content"] == before["published_content"]  # AC-E22-07
            elif action == "publish":
                model["published"] = dict(model["content"])
                assert row["published_content"] == row["content"]
                assert row["published_at"] is not None
                if before["published_content"] == before["content"]:
                    # republishing an unchanged draft: idempotent for every reader
                    assert _snapshot(row) == _snapshot(before)
            else:
                model["published"] = None
                assert row["published_content"] is None and row["published_at"] is None
                assert row["content"] == before["content"]  # the draft is kept
            # the row against the model
            assert row["content"] == model["content"]
            assert row["published_content"] == model["published"]
            assert row["status"] == ("published" if model["published"] else "draft")
            assert row["has_unpublished_changes"] is (model["content"] != model["published"])
            # the member catalog against the model
            public = client.get("/api/videos").json()
            if model["published"] is None:
                assert vid not in [v["id"] for v in public["videos"]]
            else:
                video = next(v for v in public["videos"] if v["id"] == vid)
                assert set(video) == PUBLIC_KEYS
                snap = model["published"]
                assert video["sources"][0]["url"] == snap["cdn_url"]
                assert video["durationSeconds"] == snap["duration_seconds"]
                assert video["chapters"] == snap["chapters"]
                assert video["title"] == snap["title"]
                assert video["publishedAt"] == row["published_at"]
    finally:
        client.delete(f"/api/videos/manage/{vid}", params={"expected_revision": row["revision"]})


def test_a_stale_publish_cannot_replace_a_newer_snapshot(client):
    """NC-E22-02: the published snapshot never changes from a stale write."""
    row = client.post("/api/videos/manage", json=CONTENT).json()
    vid = row["id"]
    row = client.post(f"/api/videos/manage/{vid}/publish", json={"expected_revision": 1}).json()
    row = client.put(f"/api/videos/manage/{vid}",
                     json={**CONTENT, "sort_order": 7, "expected_revision": 2}).json()
    snapshot = row["published_content"]
    for stale in (1, 2, 4):
        for verb in ("publish", "unpublish"):
            res = client.post(f"/api/videos/manage/{vid}/{verb}", json={"expected_revision": stale})
            assert res.status_code == 409 and res.json()["code"] == "videos.conflict"
        res = client.put(f"/api/videos/manage/{vid}",
                         json={**CONTENT, "sort_order": 99, "expected_revision": stale})
        assert res.status_code == 409
        res = client.delete(f"/api/videos/manage/{vid}", params={"expected_revision": stale})
        assert res.status_code == 409
    after = client.get(f"/api/videos/manage/{vid}").json()
    assert after["published_content"] == snapshot and after["revision"] == 3
    assert after["content"]["sort_order"] == 7


# ─── chapter boundaries (the request body is the oracle) ─────────────────────


def _chapter(start: float, i: int = 0) -> dict[str, Any]:
    return {"startSeconds": start, "title": {"zh-CN": f"第 {i} 章"}}


@pytest.mark.parametrize(
    ("duration", "starts", "accepted"),
    [
        (540, [0], True),  # the first second
        (540, [0, 539.999], True),  # just inside the end
        (540, [540], False),  # == duration: NC-E22-06 (`startSeconds ≥ duration`)
        (540, [541], False),  # past the end
        (540, [0, 0], False),  # not strictly increasing
        (540, [10, 5], False),  # decreasing
        (540, [-1], False),  # before the start (`ge=0`)
        (0, [0], False),  # a chapter needs a duration to sit inside
        (0, [], True),  # no chapters, no duration: the AC-E22-02 default
        (86400, [86399], True),  # the longest allowed video
        (86401, [], False),  # duration bound
    ],
)
def test_chapter_offsets_must_increase_within_the_duration(client, duration, starts, accepted):
    before = client.get("/api/videos/manage").json()["videos"]
    body = {**CONTENT, "duration_seconds": duration,
            "chapters": [_chapter(s, i) for i, s in enumerate(starts)]}
    res = client.post("/api/videos/manage", json=body)
    if accepted:
        assert res.status_code == 201, res.text
        row = res.json()
        assert [c["startSeconds"] for c in row["content"]["chapters"]] == starts
        client.delete(f"/api/videos/manage/{row['id']}", params={"expected_revision": 1})
    else:
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "validation.invalid_request"
        # NC-E22-03: no draft created
        assert client.get("/api/videos/manage").json()["videos"] == before


def test_one_hundred_chapters_fit_and_the_hundred_and_first_does_not(client):
    chapters = [_chapter(i, i) for i in range(100)]
    ok = client.post("/api/videos/manage",
                     json={**CONTENT, "duration_seconds": 1000, "chapters": chapters})
    assert ok.status_code == 201, ok.text
    client.delete(f"/api/videos/manage/{ok.json()['id']}", params={"expected_revision": 1})
    too_many = client.post("/api/videos/manage", json={
        **CONTENT, "duration_seconds": 1000, "chapters": chapters + [_chapter(100, 100)]})
    assert too_many.status_code == 422


@settings(max_examples=60, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    duration=st.floats(min_value=1, max_value=86400, allow_nan=False, allow_infinity=False),
    offsets=st.lists(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False),
                     max_size=8),
)
def test_strictly_increasing_chapters_inside_the_duration_round_trip(client, duration, offsets):
    """Any strictly increasing offsets in [0, duration) are accepted and come back verbatim
    through the managed row AND the public catalog; nudging the last one onto the duration
    is refused."""
    starts = sorted({round(o * duration * 0.999, 3) for o in offsets})
    starts = [s for s in starts if s < duration]
    body = {**CONTENT, "duration_seconds": duration,
            "chapters": [_chapter(s, i) for i, s in enumerate(starts)]}
    res = client.post("/api/videos/manage", json=body)
    assert res.status_code == 201, (starts, duration, res.text)
    row = res.json()
    try:
        assert [c["startSeconds"] for c in row["content"]["chapters"]] == starts
        published = client.post(f"/api/videos/manage/{row['id']}/publish",
                                json={"expected_revision": 1}).json()
        video = next(v for v in client.get("/api/videos").json()["videos"] if v["id"] == row["id"])
        assert [c["startSeconds"] for c in video["chapters"]] == starts
        assert video["durationSeconds"] == duration
        if starts:
            bad = {**body, "chapters": body["chapters"][:-1] + [_chapter(duration, 99)]}
            res = client.put(f"/api/videos/manage/{row['id']}",
                             json={**bad, "expected_revision": published["revision"]})
            assert res.status_code == 422
            assert client.get(f"/api/videos/manage/{row['id']}").json()["revision"] == (
                published["revision"])
        row = client.get(f"/api/videos/manage/{row['id']}").json()
    finally:
        client.delete(f"/api/videos/manage/{row['id']}",
                      params={"expected_revision": row["revision"]})
