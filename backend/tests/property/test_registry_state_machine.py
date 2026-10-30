"""E16 — local registry: a Hypothesis state machine over records, actors and visibility.

Round 16, channel A (BUG-DISCOVERY-PLAN §E16 / §4). `RegistryMachine` drives the real
`/api/registry` routes on the real `LocalRegistryBackend` (SQLite ledger, S3 faked) with
four actors — the config administrator, an administrator account, two members — across
register / legacy NULL-creator rows / PUT (description, url, SKILL.md) / DELETE / reimport /
lifecycle `/action` / reads, toggling the workspace between `shared` and `isolated`. The
model is the contract: `docs/cn/interfaces/registry-backend.md` (state machine R4, version
bump, `created=False` on a same-name match) and `users-and-ownership.md` §7 (records are
shared for read) / §8 (content mutations are creator-or-admin, NULL creator → admin only,
403 before every type / status check; lifecycle stays the review workflow — any member submits,
only an administrator approves / publishes / rejects / disables, N17-obs248). The ledger scan
(`tests/property/ledger_invariants.py`) is a machine invariant. Skill files live under
`skills/<name>/` in the artifacts bucket (registry_console.register_skill_bundle); a
deleted record must leave none — the orphan check is N16-obs35.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from itertools import count
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import RegistryRecord
from app.services import aws_clients
from app.services.registry_backend import build_mcp_descriptors
from tests import test_visibility as tv
from tests.conftest import set_default_resources
from tests.memory_fakes import FakeS3
from tests.property.ledger_invariants import assert_ledger_invariants
from tests.test_visibility import _isolate, _member

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob  # noqa: F811 — pytest fixtures by name

_SEQ = count()
BUCKET = "bkt"
# registry-backend.md "State machine (ruling R4)": from → action → to (None = 409)
TRANSITIONS: dict[tuple[str, str], str | None] = {
    ("DRAFT", "submit"): "PENDING_APPROVAL",
    ("DRAFT", "approve"): None,
    ("DRAFT", "reject"): None,
    ("DRAFT", "disable"): None,
    ("PENDING_APPROVAL", "submit"): None,
    ("PENDING_APPROVAL", "approve"): "APPROVED",
    ("PENDING_APPROVAL", "reject"): "REJECTED",
    ("PENDING_APPROVAL", "disable"): None,
    ("APPROVED", "submit"): None,
    ("APPROVED", "approve"): None,
    ("APPROVED", "reject"): None,
    ("APPROVED", "disable"): "DEPRECATED",
    ("REJECTED", "submit"): "PENDING_APPROVAL",
    ("REJECTED", "approve"): "APPROVED",
    ("REJECTED", "reject"): None,
    ("REJECTED", "disable"): None,
    ("DEPRECATED", "submit"): None,
    ("DEPRECATED", "approve"): None,
    ("DEPRECATED", "reject"): None,
    ("DEPRECATED", "disable"): None,
}
TARGET_OF = {
    "submit": "PENDING_APPROVAL",
    "approve": "APPROVED",
    "publish": "APPROVED",
    "reject": "REJECTED",
    "disable": "DEPRECATED",
}
INITIAL_VERSION = {"MCP": "1.0.0-mcp", "AGENT_SKILLS": "1.0.0-skill"}  # registry contract table
# users-and-ownership.md §1: `created_by` is NULL for the config admin, `created_by_name` is the
# display name — the built-in administrator signs in as "operator" (tests/test_visibility.ADMIN)
USERNAMES = {"admin": tv.ADMIN["username"], "carol": "carol", "ada": "ada", "bob": "bob"}


def _bump(version: str) -> str:
    """registry-backend.md: content edits bump the minor component, keeping the suffix."""
    head, _, suffix = version.partition("-")
    major, minor, patch = head.split(".")
    return f"{major}.{int(minor) + 1}.0" + (f"-{suffix}" if suffix else "")


class SkillS3(FakeS3):
    """The artifacts bucket for skill bundles: `upload_file` / `delete_object` on top of the
    object store the memory fakes provide."""

    def upload_file(self, src: str, bucket: str, key: str) -> None:
        with open(src, "rb") as fh:
            self.put_object(Bucket=bucket, Key=key, Body=fh.read())

    def delete_object(self, *, Bucket: str, Key: str, **kw: Any) -> dict:  # noqa: N803
        self.objects.pop((Bucket, Key), None)
        return {}

    def delete_objects(self, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        for obj in Delete.get("Objects", []):
            self.delete_object(Bucket=Bucket, Key=obj["Key"])
        return {"Deleted": [{"Key": o["Key"]} for o in Delete.get("Objects", [])]}

    def keys_under(self, prefix: str) -> set[str]:
        return {k for (b, k) in self.objects if b == BUCKET and k.startswith(prefix)}


def _skill_md(name: str, body: str = "Draft reports.") -> str:
    return f"---\nname: {name}\ndescription: d\n---\n# {name}\n{body}\n"


class RegistryMachine(RuleBasedStateMachine):
    """One example = one fresh registry (rows wiped, bucket emptied, workspace shared)."""

    records = Bundle("records")

    def __init__(self, actors: dict[str, tuple[str | None, bool, Any]], s3: SkillS3) -> None:
        super().__init__()
        self.actors = actors  # name → (user id or None, is_admin, client)
        self.s3 = s3
        self.s3.objects.clear()
        _isolate(value=tv.visibility.SHARED)
        db = SessionLocal()
        try:
            db.query(RegistryRecord).delete()
            db.commit()
        finally:
            db.close()
        self.model: dict[str, dict[str, Any]] = {}  # record id → model row
        self.isolated = False

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _client(self, actor: str):
        return self.actors[actor][2]

    def _may_mutate(self, actor: str, rec: dict[str, Any]) -> bool:
        """§8: an administrator, or the member whose account created the row; NULL → admins."""
        user_id, is_admin, _ = self.actors[actor]
        return is_admin or (rec["creator"] is not None and rec["creator"] == user_id)

    def _live_names(self) -> set[tuple[str, str]]:
        """registry-backend.md: `upsert_record` matches on the same (name, type) — the unique
        index is `(workspace, name, record_version)` and versions are type-qualified, so an
        MCP server and a skill may share a name."""
        return {(r["name"], r["type"]) for r in self.model.values() if not r["deleted"]}

    def _forbidden(self, res, rec: dict[str, Any], action: str) -> None:
        assert res.status_code == 403, res.text
        body = res.json()
        assert body["code"] == "registry.forbidden", body
        assert body["detail"] == {
            "created_by": rec["creator"],
            "created_by_name": rec["creator_name"],
            "record_id": rec["id"],
            "action": action,
        }, body["detail"]

    def _gone(self, res) -> None:
        assert res.status_code == 404 and res.json()["code"] == "registry.record_not_found", (
            res.text
        )

    # ── rules ────────────────────────────────────────────────────────────────────
    @rule(
        target=records,
        actor=st.sampled_from(["admin", "carol", "ada", "bob"]),
        rtype=st.sampled_from(["MCP", "AGENT_SKILLS"]),
        slot=st.integers(min_value=0, max_value=4),
    )
    def register(self, actor, rtype, slot):
        name = f"rec-{slot}"
        body: dict[str, Any] = {"type": rtype, "name": name, "description": "d"}
        if rtype == "MCP":
            body["url"] = "https://mcp.example/mcp"
        else:
            body["skill_md"] = _skill_md(name)
        before = self.s3.keys_under(f"skills/{name}/")
        res = self._client(actor).post("/api/registry/records", json=body)
        if (name, rtype) in self._live_names():
            # registry-backend.md: a same-name match is never overwritten — the console's
            # registration refuses it (`registry.name_exists`)
            assert res.status_code == 409 and res.json()["code"] == "registry.name_exists", res.text
            assert self.s3.keys_under(f"skills/{name}/") == before, "a refused register wrote files"
            return self._noop()
        assert res.status_code == 201, res.text
        out = res.json()
        user_id, _is_admin, _ = self.actors[actor]
        rec = {
            "id": out["record_id"],
            "name": name,
            "type": rtype,
            "status": "DRAFT",
            "version": INITIAL_VERSION[rtype],
            "creator": user_id,
            "creator_name": USERNAMES[actor],
            "deleted": False,
        }
        assert out["status"] == "DRAFT" and out["version"] == rec["version"], out
        assert out["created_by"] == rec["creator"] and out["created_by_name"] == rec["creator_name"]
        if rtype == "AGENT_SKILLS":
            assert f"skills/{name}/SKILL.md" in self.s3.keys_under(f"skills/{name}/")
        self.model[rec["id"]] = rec
        return rec["id"]

    def _noop(self):
        # a rule with `target=` must return something; a sentinel the other rules skip
        return "none"

    @rule(
        target=records,
        slot=st.integers(min_value=0, max_value=4),
        status=st.sampled_from(["DRAFT", "APPROVED", "DEPRECATED"]),
    )
    def legacy_row(self, slot, status):
        """A pre-N15-obs7 row: no creator recorded (deploy-created, imported, platform)."""
        name = f"legacy-{slot}"
        if (name, "MCP") in self._live_names():
            return self._noop()
        db = SessionLocal()
        try:
            row = RegistryRecord(
                workspace_id=DEFAULT_WORKSPACE_ID,
                record_type="MCP",
                name=name,
                description="d",
                record_version="1.0.0-mcp",
                status=status,
                descriptors=build_mcp_descriptors(
                    target=name, description="d", gateway_url="https://gw.example/mcp", tools=None
                ),
            )
            if status != "DRAFT":
                row.decided_at = datetime.now(UTC)
                row.decided_by = "system"
            db.add(row)
            db.commit()
            rid = row.id
        finally:
            db.close()
        self.model[rid] = {
            "id": rid,
            "name": name,
            "type": "MCP",
            "status": status,
            "version": "1.0.0-mcp",
            "creator": None,
            "creator_name": None,
            "deleted": False,
        }
        return rid

    @rule(mode=st.sampled_from(["shared", "isolated"]))
    def toggle_visibility(self, mode):
        _isolate(value=tv.visibility.ISOLATED if mode == "isolated" else tv.visibility.SHARED)
        self.isolated = mode == "isolated"

    @rule(
        rid=records,
        actor=st.sampled_from(["admin", "carol", "ada", "bob"]),
        field=st.sampled_from(["description", "url", "skill_md"]),
    )
    def put(self, rid, actor, field):
        if rid == "none":
            return
        rec = self.model[rid]
        payload = (
            {"description": f"by {actor}"}
            if field == "description"
            else (
                {"url": "https://mcp.example/v2/mcp"}
                if field == "url"
                else {"skill_md": _skill_md(rec["name"], "Revised.")}
            )
        )
        action = "edit"
        res = self._client(actor).put(f"/api/registry/records/{rid}", json=payload)
        if rec["deleted"]:
            return self._gone(res)
        if not self._may_mutate(actor, rec):
            # §8: the gate runs before every type / status check — a forbidden caller learns
            # nothing (the same 403 for a DEPRECATED record or a mismatched field)
            return self._forbidden(res, rec, action)
        if (field == "url" and rec["type"] != "MCP") or (
            field == "skill_md" and rec["type"] != "AGENT_SKILLS"
        ):
            assert (
                res.status_code == 400 and res.json()["code"] == "registry.field_type_mismatch"
            ), res.text
            return
        if rec["status"] == "DEPRECATED":
            # registry-backend.md: DEPRECATED is terminal — "only delete remains"
            assert res.status_code == 400 and res.json()["code"] == "registry.not_editable", (
                res.text
            )
            return
        assert res.status_code == 200, res.text
        out = res.json()
        if field == "description":
            # "description-only edits keep status and version"
            assert out["status"] == rec["status"] and out["version"] == rec["version"], out
            assert out["description"] == f"by {actor}"
        else:
            # "new descriptors/record_version reset any non-terminal record to DRAFT"
            rec["version"] = _bump(rec["version"])
            rec["status"] = "DRAFT"
            assert out["status"] == "DRAFT" and out["version"] == rec["version"], out
            assert out["status_reason"] == "updated — awaiting review"
            if field == "skill_md":
                stored = self.s3.objects[(BUCKET, f"skills/{rec['name']}/SKILL.md")]["Body"]
                assert b"Revised." in stored
        assert out["created_by"] == rec["creator"]  # the creator never changes on edit

    @rule(
        rid=records,
        actor=st.sampled_from(["admin", "carol", "ada", "bob"]),
        action=st.sampled_from(["submit", "approve", "publish", "reject", "disable"]),
    )
    def lifecycle(self, rid, actor, action):
        if rid == "none":
            return
        rec = self.model[rid]
        res = self._client(actor).post(
            f"/api/registry/records/{rid}/action", json={"action": action}
        )
        # §8: lifecycle is the review workflow — every member may submit; the decision
        # (approve / publish / reject / disable) is an administrator's (N17-obs248), refused
        # before ANY backend read — so before the existence 404 and the status check
        _user_id, is_admin, _ = self.actors[actor]
        if action != "submit" and not is_admin:
            assert res.status_code == 403, res.text
            body = res.json()
            assert body["code"] == "registry.approval_admin_only", body
            assert body["detail"] == {"action": action}, body["detail"]
            return
        if rec["deleted"]:
            return self._gone(res)
        target = TRANSITIONS[(rec["status"], "approve" if action == "publish" else action)]
        if target is None:
            assert res.status_code == 409, res.text
            body = res.json()
            assert body["code"] == "registry.invalid_transition"
            assert body["detail"] == {
                "record_id": rid,
                "from_status": rec["status"],
                "to_status": TARGET_OF[action],
            }, body["detail"]
            return
        assert res.status_code == 200, res.text
        out = res.json()
        rec["status"] = target
        assert out["status"] == target, out
        assert out["version"] == rec["version"]  # a decision never changes the content version

    @rule(rid=records, actor=st.sampled_from(["admin", "carol", "ada", "bob"]))
    def reimport(self, rid, actor):
        if rid == "none":
            return
        rec = self.model[rid]
        res = self._client(actor).post(f"/api/registry/records/{rid}/reimport")
        if rec["deleted"]:
            return self._gone(res)
        if not self._may_mutate(actor, rec):
            return self._forbidden(res, rec, "reimport")
        # inline / MCP records have no retrievable origin: 400 `registry.not_reimportable`
        assert res.status_code == 400 and res.json()["code"] == "registry.not_reimportable", (
            res.text
        )

    @rule(rid=records, actor=st.sampled_from(["admin", "carol", "ada", "bob"]))
    def delete(self, rid, actor):
        if rid == "none":
            return
        rec = self.model[rid]
        res = self._client(actor).delete(f"/api/registry/records/{rid}")
        if rec["deleted"]:
            return self._gone(res)
        if not self._may_mutate(actor, rec):
            return self._forbidden(res, rec, "delete")
        body = res.json()
        assert res.status_code == 200 and (body["deleted"], body["record_id"]) == (True, rid), (
            res.text
        )
        rec["deleted"] = True
        self._gone(self._client(actor).get(f"/api/registry/records/{rid}"))

    @rule(rid=records, actor=st.sampled_from(["admin", "carol", "ada", "bob"]))
    def read(self, rid, actor):
        """§7: MCP / skill records stay SHARED for read in both modes, for every member."""
        if rid == "none":
            return
        rec = self.model[rid]
        res = self._client(actor).get(f"/api/registry/records/{rid}")
        if rec["deleted"]:
            return self._gone(res)
        assert res.status_code == 200, res.text
        out = res.json()
        assert (out["status"], out["version"], out["name"], out["type"]) == (
            rec["status"],
            rec["version"],
            rec["name"],
            rec["type"],
        )
        assert (out["created_by"], out["created_by_name"]) == (rec["creator"], rec["creator_name"])
        listed = {
            r["record_id"]
            for r in self._client(actor).get("/api/registry/records").json()["records"]
        }
        assert rid in listed

    # ── invariants ───────────────────────────────────────────────────────────────
    @invariant()
    def ledger_is_clean(self):
        assert_ledger_invariants(SessionLocal)

    @invariant()
    def api_and_model_agree(self):
        """Every live record is listed with the model's status for every actor; a deleted one
        is listed for none."""
        for actor in ("admin", "ada"):
            listed = {
                r["record_id"]: r
                for r in self._client(actor).get("/api/registry/records").json()["records"]
            }
            for rid, rec in self.model.items():
                if rec["deleted"]:
                    assert rid not in listed, rid
                else:
                    assert listed[rid]["status"] == rec["status"], (rid, listed[rid]["status"], rec)

    @invariant()
    def live_skill_records_have_their_files(self):
        for rec in self.model.values():
            if rec["type"] == "AGENT_SKILLS" and not rec["deleted"]:
                assert f"skills/{rec['name']}/SKILL.md" in self.s3.keys_under(
                    f"skills/{rec['name']}/"
                )


@pytest.fixture
def actors(app, admin, ada, bob, monkeypatch):
    carol_id, carol = _member(app, "carol", role="admin")
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    s3 = SkillS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: s3)
    set_default_resources({"artifacts_bucket": BUCKET, "registry_id": "local-default"})
    return {
        "admin": (None, True, admin),  # the row-less config admin
        "carol": (carol_id, True, carol),
        "ada": (ada_id, False, ada_client),
        "bob": (bob_id, False, bob_client),
    }, s3


def test_registry_state_machine(actors):
    table, s3 = actors
    run_state_machine_as_test(
        lambda: RegistryMachine(table, s3),
        settings=settings(
            max_examples=int(os.environ.get("LAUNCHPAD_REGISTRY_SM_EXAMPLES", "25")),
            stateful_step_count=30,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
        ),
    )


# ── orphan skill files (deterministic) ───────────────────────────────────────────────


def test_deleting_a_skill_record_leaves_no_files_under_its_prefix(actors):
    """N16-obs35 (r16-d), fixed in cn/r17-j: the delete sweeps `skills/<name>/` when no other
    record or live agent references the prefix."""
    table, s3 = actors
    _ada_id, ada_client = table["ada"][0], table["ada"][2]
    res = ada_client.post(
        "/api/registry/records",
        json={
            "type": "AGENT_SKILLS",
            "name": "expense-writer",
            "description": "d",
            "skill_md": _skill_md("expense-writer"),
        },
    )
    assert res.status_code == 201, res.text
    rid = res.json()["record_id"]
    assert s3.keys_under("skills/expense-writer/") == {"skills/expense-writer/SKILL.md"}
    assert ada_client.delete(f"/api/registry/records/{rid}").status_code == 200
    assert s3.keys_under("skills/expense-writer/") == set(), sorted(
        s3.keys_under("skills/expense-writer/")
    )


def test_a_failed_register_after_upload_cleans_its_objects(actors, monkeypatch):
    """registry_console.register_skill_bundle: "If the record write fails after the upload,
    the uploaded objects are deleted (no orphan prefix)"."""
    from app.services import registry_console

    table, s3 = actors
    ada_client = table["ada"][2]

    def boom(*a, **k):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(
        registry_console,
        "_backend",
        lambda: type("B", (), {"find_record": lambda self, *a, **k: None, "upsert_record": boom})(),
    )
    with pytest.raises(RuntimeError):
        ada_client.post(
            "/api/registry/records",
            json={
                "type": "AGENT_SKILLS",
                "name": "doomed",
                "description": "d",
                "skill_md": _skill_md("doomed"),
            },
        )
    assert s3.keys_under("skills/doomed/") == set()
    assert_ledger_invariants(SessionLocal)
