"""E20 Announcements — the draft / publish / withdraw lifecycle as a state machine with an
injected clock, plus racing writers (round 16, r16-g; `docs/cn/plans/BUG-DISCOVERY-PLAN.md`
§E20: "the boundaries of publish / withdraw / expire").

Contract sources — `docs/cn/acceptance/E20-announcements.md`:

* §1: "Draft and published snapshot are two projections of one row; every write carries
  `expected_revision` and a mismatch is `409 announcements.conflict`"; `GET
  /api/announcements` = published snapshots only, ordered by `published_at` desc.
* AC-E20-01/02/04/05: create → `draft`, `revision 1`, `published_content null`; publish →
  `published`, `published_content == content`, `published_at` set; edit after publish →
  `has_unpublished_changes true` and the member board keeps the OLD text; unpublish →
  `draft`, `published_at null`, the draft kept.
* AC-E20-03: the public shape is exactly `{id, title, body, link_url, link_label,
  published_at}` — no `revision`, no `created_by`.
* NC-E20-02: a stale `expected_revision` on PUT / publish / unpublish / DELETE is 409 and
  "the newer content is never replaced or published".
* NC-E20-08: unknown id → `404 announcements.not_found`.

"Expire": the contract (and `models/announcement.py`) has no expiry — a notice leaves the
board only through `unpublish` / `DELETE`. The machine therefore treats *withdraw* as the
only exit and pins that neither projection grows an expiry field, so a future expiry
lands here as a deliberate contract change rather than silently.

Oracle: a model of the rows (revision, draft content, published snapshot, publish time)
maintained by the machine; after every step the admin list and the member board are
recomputed from the model and compared. The clock is injected (`announcements.datetime`),
so `published_at` ordering — including ties — is deterministic.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import Bundle, RuleBasedStateMachine, consumes, invariant, rule

from app.core.db import engine
from app.core.display_time import display_iso
from app.models.announcement import Announcement
from app.services import announcements as service
from tests.test_announcements import CONTENT

PUBLIC_KEYS = {"id", "title", "body", "link_url", "link_label", "published_at"}
ADMIN_KEYS = {"id", "content", "published_content", "status", "has_unpublished_changes",
              "revision", "created_by", "updated_by", "created_at", "updated_at", "published_at"}
T0 = datetime(2026, 10, 19, 9, 0, tzinfo=UTC)


class Clock:
    """`announcements.datetime` stand-in: `now()` is whatever the test set."""

    def __init__(self) -> None:
        self.current = T0

    def now(self, tz=None) -> datetime:
        return self.current if tz is None else self.current.astimezone(tz)

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def clock(monkeypatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(service, "datetime", fake)
    return fake


def _iso(value: datetime | None) -> str | None:
    return display_iso(value) if value else None  # N17-obs463: the wire is +08:00


CONTENTS = st.fixed_dictionaries({
    "title": st.text(min_size=1, max_size=40).map(str.strip).filter(bool),
    "body": st.text(min_size=1, max_size=120).map(str.strip).filter(bool),
}).map(lambda c: {**c, "link_url": "/v2/agents", "link_label": "Open Agents"})


class AnnouncementMachine(RuleBasedStateMachine):
    client: Any
    clock: Clock

    def __init__(self) -> None:
        super().__init__()
        self.rows: dict[str, dict[str, Any]] = {}
        # one ledger per example: the suite's table wipe runs per test, not per example
        with engine.begin() as conn:
            conn.execute(Announcement.__table__.delete())
        self.clock.current = T0

    ids = Bundle("ids")

    # ─── actions ─────────────────────────────────────────────────────────────

    @rule(target=ids, content=CONTENTS)
    def create(self, content):
        res = self.client.post("/api/announcements", json=content)
        assert res.status_code == 201, res.text
        row = res.json()
        assert set(row) == ADMIN_KEYS
        assert (row["status"], row["revision"], row["published_content"]) == ("draft", 1, None)
        assert row["has_unpublished_changes"] is True
        self.rows[row["id"]] = {"revision": 1, "content": content, "published": None,
                                "published_at": None}
        return row["id"]

    @rule(aid=ids, content=CONTENTS, stale=st.booleans())
    def save(self, aid, content, stale):
        self._write(aid, "PUT", f"/api/announcements/{aid}", stale,
                    body=lambda rev: {**content, "expected_revision": rev},
                    apply=lambda m: m.update(content=content))

    @rule(aid=ids, stale=st.booleans())
    def publish(self, aid, stale):
        def apply(m):
            m["published"] = dict(m["content"])
            m["published_at"] = self.clock.current
        self._write(aid, "POST", f"/api/announcements/{aid}/publish", stale,
                    body=lambda rev: {"expected_revision": rev}, apply=apply)

    @rule(aid=ids, stale=st.booleans())
    def unpublish(self, aid, stale):
        def apply(m):
            m["published"] = None
            m["published_at"] = None
        self._write(aid, "POST", f"/api/announcements/{aid}/unpublish", stale,
                    body=lambda rev: {"expected_revision": rev}, apply=apply)

    @rule(aid=consumes(ids), stale=st.booleans())
    def delete(self, aid, stale):
        model = self.rows[aid]
        rev = model["revision"] + (1 if stale else 0)
        res = self.client.delete(f"/api/announcements/{aid}", params={"expected_revision": rev})
        if stale:
            assert res.status_code == 409 and res.json()["code"] == "announcements.conflict"
            # the row survives a stale delete untouched
            kept = self.client.get(f"/api/announcements/{aid}").json()
            assert kept["revision"] == model["revision"]
            # a consumed id leaves the bundle either way; keep the model honest by deleting
            # it for real now (correct revision), which is itself one more write to check
            res = self.client.delete(f"/api/announcements/{aid}",
                                     params={"expected_revision": model["revision"]})
        assert res.status_code == 200 and res.json() == {"deleted": True}, res.text
        del self.rows[aid]
        gone = self.client.get(f"/api/announcements/{aid}")
        assert gone.status_code == 404 and gone.json()["code"] == "announcements.not_found"

    @rule(seconds=st.integers(0, 3600))
    def tick(self, seconds):
        self.clock.advance(seconds)

    def _write(self, aid, method, url, stale, *, body, apply):
        model = self.rows[aid]
        # a stale tab: the revision it last read is behind (or, racing, ahead of) the row's
        rev = model["revision"] + (1 if stale else 0)
        res = self.client.request(method, url, json=body(rev))
        if stale:
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "announcements.conflict"
            return
        assert res.status_code == 200, res.text
        row = res.json()
        apply(model)
        model["revision"] += 1
        assert row["revision"] == model["revision"]
        assert row["content"] == model["content"]
        assert row["published_content"] == model["published"]
        assert row["published_at"] == _iso(model["published_at"])
        assert row["status"] == ("published" if model["published"] is not None else "draft")
        assert row["has_unpublished_changes"] is (model["content"] != model["published"])
        assert row["updated_at"] == _iso(self.clock.current)

    # ─── invariants: both projections recomputed from the model ───────────────

    @invariant()
    def the_member_board_is_exactly_the_published_snapshots(self):
        want = sorted(
            ((m["published_at"], aid) for aid, m in self.rows.items() if m["published"]),
            key=lambda t: (t[0], t[1]), reverse=True,
        )
        res = self.client.get("/api/announcements", params={"limit": 100})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == len(want)
        assert [item["id"] for item in body["announcements"]] == [aid for _, aid in want]
        for item in body["announcements"]:
            model = self.rows[item["id"]]
            assert set(item) == PUBLIC_KEYS, item  # AC-E20-03: no revision / created_by
            assert {k: item[k] for k in ("title", "body", "link_url", "link_label")} == (
                model["published"])
            assert item["published_at"] == _iso(model["published_at"])

    @invariant()
    def the_admin_list_mirrors_every_row(self):
        res = self.client.get("/api/announcements/manage", params={"limit": 100})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == len(self.rows)
        for row in body["announcements"]:
            assert set(row) == ADMIN_KEYS
            model = self.rows[row["id"]]
            assert row["revision"] == model["revision"]
            assert row["content"] == model["content"]
            assert row["published_content"] == model["published"]
            # no expiry dimension exists in either projection (see the module docstring)
            assert not {k for k in row if "expir" in k}


def test_announcement_lifecycle_state_machine(client, clock):
    class Machine(AnnouncementMachine):
        pass

    Machine.client = client
    Machine.clock = clock
    Machine.TestCase.settings = settings(
        max_examples=25, stateful_step_count=20, deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
    )
    case = Machine.TestCase()
    case.runTest()


# ─── ties and ordering under the injected clock ───────────────────────────────


def test_same_second_publishes_order_by_id_desc_then_newer_first(client, clock):
    """§1: ordered by `published_at` desc; the service breaks ties by id desc."""
    first = client.post("/api/announcements", json={**CONTENT, "title": "A"}).json()
    second = client.post("/api/announcements", json={**CONTENT, "title": "B"}).json()
    for row in (first, second):
        assert client.post(f"/api/announcements/{row['id']}/publish",
                           json={"expected_revision": 1}).status_code == 200
    board = client.get("/api/announcements").json()["announcements"]
    assert [b["id"] for b in board] == sorted([first["id"], second["id"]], reverse=True)
    assert {b["published_at"] for b in board} == {display_iso(T0)}
    # a later republish of the older one moves it to the top
    clock.advance(60)
    assert client.post(f"/api/announcements/{first['id']}/publish",
                       json={"expected_revision": 2}).status_code == 200
    board = client.get("/api/announcements").json()["announcements"]
    assert [b["id"] for b in board] == [first["id"], second["id"]]
    assert board[0]["published_at"] == display_iso(T0 + timedelta(seconds=60))


def test_an_edit_after_publish_keeps_the_old_text_on_the_board_until_republished(client, clock):
    """AC-E20-04/05."""
    row = client.post("/api/announcements", json=CONTENT).json()
    aid = row["id"]
    client.post(f"/api/announcements/{aid}/publish", json={"expected_revision": 1})
    edited = client.put(f"/api/announcements/{aid}",
                        json={**CONTENT, "title": "newer", "expected_revision": 2}).json()
    assert edited["has_unpublished_changes"] is True and edited["status"] == "published"
    assert client.get("/api/announcements").json()["announcements"][0]["title"] == CONTENT["title"]
    client.post(f"/api/announcements/{aid}/publish", json={"expected_revision": 3})
    assert client.get("/api/announcements").json()["announcements"][0]["title"] == "newer"
    withdrawn = client.post(f"/api/announcements/{aid}/unpublish",
                            json={"expected_revision": 4}).json()
    assert (withdrawn["status"], withdrawn["published_at"]) == ("draft", None)
    assert withdrawn["content"]["title"] == "newer"  # the draft is kept
    assert client.get("/api/announcements").json() == {"announcements": [], "total": 0}


# ─── concurrency: N writers, one expected_revision ────────────────────────────


@pytest.mark.parametrize("action", ["publish", "save", "unpublish", "delete"])
def test_racing_writers_with_one_revision_get_exactly_one_success(client, action):
    """NC-E20-02 under real concurrency: eight threads submit the same `expected_revision`
    at once; exactly one 200, the rest 409 `announcements.conflict`, never a 5xx, and the
    row advanced by exactly one revision (or is gone, for delete)."""
    row = client.post("/api/announcements", json=CONTENT).json()
    aid, rev = row["id"], row["revision"]
    if action == "unpublish":
        row = client.post(f"/api/announcements/{aid}/publish",
                          json={"expected_revision": rev}).json()
        rev = row["revision"]
    barrier = threading.Barrier(8)

    def attempt(i: int):
        barrier.wait(timeout=10)
        if action == "publish":
            return client.post(f"/api/announcements/{aid}/publish", json={"expected_revision": rev})
        if action == "unpublish":
            return client.post(f"/api/announcements/{aid}/unpublish",
                               json={"expected_revision": rev})
        if action == "save":
            return client.put(f"/api/announcements/{aid}",
                              json={**CONTENT, "title": f"writer {i}", "expected_revision": rev})
        return client.delete(f"/api/announcements/{aid}", params={"expected_revision": rev})

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(attempt, range(8)))
    statuses = sorted(r.status_code for r in responses)
    # a loser of the delete race may find the row already gone: 404 is as honest as 409
    losers = {409, 404} if action == "delete" else {409}
    assert statuses[0] == 200 and set(statuses[1:]) <= losers, [
        (r.status_code, r.text[:80]) for r in responses]
    assert statuses.count(200) == 1
    assert all(r.json()["code"] in ("announcements.conflict", "announcements.not_found")
               for r in responses if r.status_code != 200)
    after = client.get(f"/api/announcements/{aid}")
    if action == "delete":
        assert after.status_code == 404
    else:
        assert after.json()["revision"] == rev + 1
        if action == "save":
            winner = next(r for r in responses if r.status_code == 200).json()
            assert after.json()["content"] == winner["content"]
