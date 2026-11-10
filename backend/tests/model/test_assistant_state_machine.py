# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_assistant
"""E02 — architect assistant: proposal → edit → approve / reject → clear, as a model
(round 16 channel A, Hypothesis ``RuleBasedStateMachine``; hermetic — the architect's
data plane is the scripted ``FakeArchitectRuntime``, the deploy starter is recorded, the
purge's AWS seams are the ``cloud`` fakes of ``test_assistant_purge_dependencies``).

Every invariant names the contract clause it checks; none copies a threshold from code:

* **Validator-decidable** — architecture.md §Architect assistant, *Proposal submission,
  validation and revisions*: "A proposal is checked when it is recorded against everything
  the member will later meet" and *Validation reports every failing stage at once*: a
  revision is ``draft`` (valid, bindings pinned) or ``invalid`` with operator-readable
  errors — never an exception, never a 500. The oracle is **differential**: the stored row
  must equal what the pure validator (``proposal.validate``) says about the same input, and
  **idempotent**: re-validating a stored ``draft`` content reproduces it with no errors.
* **New revision, never a mutation** — same section: "A member edit is a NEW revision (never
  a mutation of an approved one)"; "a later revision is a change"; the previous draft /
  invalid revision becomes ``superseded`` (``test_member_edit_is_a_new_revision_and_the_
  old_one_is_stale`` pins the statuses). So at most ONE revision is non-final and it is the
  newest; revision numbers are contiguous from 1.
* **Approve only a valid, current revision** — "Names an exact revision + content hash; on
  success one regular agent/deployment/job exists … Repeated and concurrent calls return
  the same recorded outcome (200)"; a superseded / invalid / rejected revision is
  ``409 assistant.proposal_not_approvable``, a wrong hash or a non-current revision
  ``409 assistant.proposal_stale``; the conversation "ends in an inert, reviewable proposal
  for ONE new managed-Harness business agent" and "no longer offers another creation
  approval in that conversation. Historical approvals and their idempotent outcomes remain
  intact" — so one conversation owns at most one Agent.
* **Resources locked after approval** — "Preparation saves/imports and resource-changing
  proposal edits return ``409 assistant.resources_locked``".
* **Round-trip** — editing any subset of the 22 contract keys leaves the unedited keys
  byte-identical (the N15-obs13 escape: a member edit lost ``evaluation_plan``); the
  ``proposal_repaired`` note is recorded **exactly** when the server carried the seed
  (bugs-round-15.md N15-obs13; ``service.carry_evaluation_plan``: absent → carried + note,
  ``null`` → explicit removal, object → stored as submitted).
* **CLEAR** — assistant-turns.md §10: a transcript-only conversation is deleted inline
  (``200 deleted: true``); with an Agent it is a durable job (``202 {job_id}``) whose ledger
  step removes "operations, plans, proposals, messages, turn events, conversation"; a
  live deploy job is a ``blocker`` (``409 assistant.conversation_busy``); the canary is read
  back and left ``cleaned``; a repeated DELETE while the job is live returns it; "resume_
  pending_jobs re-adopts a running row after a restart"; nothing is left to resume
  afterwards.
* **Interrupted turn** — assistant-turns.md §2 *Startup sweep*: a claim found at boot is
  finalized from the event log — partial text, the ``turn_interrupted`` row, a block that
  "had streamed completely, validated like a completed reply", a terminal
  ``error {"message": "interrupted"}`` event — "Never resumed blind".

Findings are strict ``xfail`` rows (``N16-obs40..``) with their shrunk repro in the
docstring; the invariant is never weakened to pass.
"""

from __future__ import annotations

import copy
import json
import os
import re
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import HealthCheck, assume, given, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app.assistant import proposal as contract
from app.assistant import purge, service
from app.core.db import SessionLocal
from app.deployer import pipeline
from app.main import create_app
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantMessage,
    AssistantProposal,
    AssistantTurnEvent,
)
from app.models.ledger import Agent, Job
from app.optimization.models import RuntimeCanary
from app.schemas.agent import allowed_model_ids
from tests.model.worlds import reset_assistant_world
from tests.property.ledger_invariants import scan_ledger
from tests.test_assistant import (  # noqa: F401 — autouse fixtures + helpers by name
    BASE,
    FISHBONE,
    VALID_PROPOSAL,
    _approve,
    _block,
    _catalog,
    _latest,
    _mark_ready,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)
from tests.test_assistant_purge_dependencies import (  # noqa: F401 — fixture by name
    _canary,
    cloud,
    wait_job,
)

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
SCALE = 10 if SOAK else 1
CONV_URL = f"{BASE}/conversations"

# the 22 keys of the proposal contract (``ProposalContent`` — the allowlist)
CONTRACT_KEYS = (
    "version", "name", "model_id", "model_source", "system_prompt", "tools",
    "tool_functions", "native_tools", "builtin_tools", "skills", "knowledge_bases", "memory",
    "max_iterations", "timeout_seconds", "summary", "requirements_baseline", "assumptions",
    "manual_tasks", "golden_tests", "evaluator_recommendations", "evaluation_plan", "fishbone",
)
RESOURCE_KEYS = ("tools", "skills", "knowledge_bases")
FINAL_STATUSES = ("superseded", "rejected", "approved")
REPAIRED = re.compile(r"^proposal_repaired\b")
EMPTY_SEED = {"evaluators": [], "scenarios": [], "recommendation_keys": {},
              "blocked_golden_tests": []}


def _canon(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------------------
# strategies — one per contract key, valid AND invalid shapes
# ---------------------------------------------------------------------------

_lines = st.lists(st.text(alphabet="abcdefghij 字节", min_size=1, max_size=12), max_size=3)
_bad_lines = st.one_of(st.just([""]), st.just("not-a-list"), st.just(["x" * 1001]))


def _key_strategy(key: str) -> st.SearchStrategy:
    valid, invalid = {
        "version": (st.just(1), st.sampled_from([0, 2, "1"])),
        "name": (st.from_regex(r"^[a-z][a-z0-9-]{2,20}$", fullmatch=True),
                 st.sampled_from(["HR", "launchpad-x", "a", "x" * 60, 7])),
        "model_id": (st.sampled_from(sorted(allowed_model_ids())),
                     st.sampled_from(["gpt-9", "", "bad id with spaces"])),
        "model_source": (st.just("bedrock"), st.sampled_from(["openai", "", None])),
        "system_prompt": (st.text(min_size=1, max_size=200), st.sampled_from(["", None, 7])),
        "tools": (st.lists(st.sampled_from(["gateway:hr-tools", "mcp:deepwiki"]),
                           unique=True, max_size=2),
                  st.sampled_from([["gateway:nope"], ["mcp:dup"], "gateway:hr-tools"])),
        "tool_functions": (st.just(None),
                           st.sampled_from([{"gateway:hr-tools": ["hr-tools___get_employee"]},
                                            {"mcp:deepwiki": []}, "x"])),
        "native_tools": (st.lists(st.sampled_from(["shell", "file_operations"]),
                                  unique=True, max_size=2),
                         st.sampled_from([["browser"], "shell"])),
        "builtin_tools": (st.sampled_from([[], ["code-interpreter"]]),
                          st.sampled_from([["browser"], ["code-interpreter"] * 2])),
        "skills": (st.sampled_from([[], ["meeting-summarizer"]]),
                   st.sampled_from([["ghost-skill"], "meeting-summarizer"])),
        "knowledge_bases": (st.sampled_from([[], ["KB123ABC"]]),
                            st.sampled_from([["KBNOPE"], 3])),
        "memory": (st.sampled_from(["disabled", "workspace"]),
                   st.sampled_from(["short_term", "", None])),
        "max_iterations": (st.integers(min_value=1, max_value=100),
                           st.sampled_from([0, 101, "12", None])),
        "timeout_seconds": (st.integers(min_value=10, max_value=3600),
                            st.sampled_from([9, 3601, "240"])),
        "summary": (st.text(max_size=120), st.sampled_from([7, "x" * 4001])),
        "requirements_baseline": (_lines, _bad_lines),
        "assumptions": (_lines, _bad_lines),
        "manual_tasks": (_lines, _bad_lines),
        "golden_tests": (
            st.sampled_from([[], VALID_PROPOSAL["golden_tests"]]),
            st.sampled_from([[{"id": "", "input": "x"}], [{"input": "no id"}], "GT"]),
        ),
        "evaluator_recommendations": (
            st.sampled_from([[], ["Builtin.Helpfulness"],
                             [{"text": "be helpful", "evaluator_id": "Builtin.Helpfulness",
                               "blocking": True, "threshold": 0.7}]]),
            st.sampled_from([[{"text": "", "evaluator_id": ""}], [{"threshold": 2.0}], 5]),
        ),
        "evaluation_plan": (st.sampled_from([None, {}, EMPTY_SEED]),
                            st.sampled_from([{"evaluators": "x"}, {"unknown": 1}, 1])),
        "fishbone": (st.sampled_from([None, FISHBONE]),
                     st.sampled_from([{"version": 1}, {"customer": "x"}, "fb"])),
    }[key]
    return st.one_of(valid, valid, invalid)  # 2:1 valid, so most edits still parse


def _edits_strategy(keys=CONTRACT_KEYS) -> st.SearchStrategy[dict]:
    """A random non-empty subset of the contract keys, each with a random value."""
    return st.lists(st.sampled_from(keys), min_size=1, max_size=6, unique=True).flatmap(
        lambda ks: st.fixed_dictionaries({k: _key_strategy(k) for k in ks})
    )


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------


def _rows(cid: str) -> dict[str, int]:
    db = SessionLocal()
    try:
        return {
            "conversation": db.query(AssistantConversation).filter_by(id=cid).count(),
            "messages": db.query(AssistantMessage).filter_by(conversation_id=cid).count(),
            "proposals": db.query(AssistantProposal).filter_by(conversation_id=cid).count(),
            "events": db.query(AssistantTurnEvent).filter_by(conversation_id=cid).count(),
            "plans": db.query(AssistantEvaluationPlan).filter_by(conversation_id=cid).count(),
        }
    finally:
        db.close()


def _conversation(cid: str) -> AssistantConversation | None:
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        if row is not None:
            db.expunge(row)
        return row
    finally:
        db.close()


def _agents_of(cid: str) -> list[Agent]:
    db = SessionLocal()
    try:
        ids = [p.agent_id for p in db.query(AssistantProposal)
               .filter(AssistantProposal.conversation_id == cid,
                       AssistantProposal.agent_id.isnot(None)).all()]
        rows = [db.get(Agent, i) for i in dict.fromkeys(ids)]
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _messages(cid: str) -> list[tuple[int, str, str | None]]:
    db = SessionLocal()
    try:
        return [(m.turn, m.role, m.name) for m in db.query(AssistantMessage)
                .filter_by(conversation_id=cid).order_by(AssistantMessage.id).all()]
    finally:
        db.close()


def _events(cid: str, turn: int) -> list[tuple[int, str]]:
    db = SessionLocal()
    try:
        return [(e.seq, e.event) for e in db.query(AssistantTurnEvent)
                .filter_by(conversation_id=cid, turn=turn).order_by(AssistantTurnEvent.seq)]
    finally:
        db.close()


def _finish_deploy(agent_id: str, job_id: str | None) -> None:
    """The recorded deploy starter never ran: settle the job and the Agent the way a
    finished pipeline leaves them (the purge then reads the Agent through the fakes)."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status = "active"
        agent.resource_id = agent.resource_id or f"rt{agent.id[:10]}"
        agent.arn = agent.arn or (
            f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{agent.resource_id}"
        )
        if job_id:
            job = db.get(Job, job_id)
            if job is not None:
                job.status = "succeeded"
        db.commit()
    finally:
        db.close()


def _seed_dead_turn(cid: str, streamed: str) -> int:
    """What a dead process leaves mid-turn (assistant-turns.md §2): the claim, the user row,
    the event log of what had streamed — no terminal row, no terminal event."""
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        turn = (row.turns or 0) + 1
        row.turns, row.active_turn, row.active_turn_token = turn, turn, "dead-process-token"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(seconds=120)
        db.add(AssistantMessage(workspace_id=row.workspace_id, conversation_id=cid, turn=turn,
                                role="user", text="go", author="river",
                                runtime_session_id="s" * 40))
        events = [{"event": "meta", "data": {"turn": turn}}]
        events += [{"event": "delta", "data": {"text": streamed[i:i + 40]}}
                   for i in range(0, len(streamed), 40)]
        for seq, e in enumerate(events, start=1):
            db.add(AssistantTurnEvent(workspace_id=row.workspace_id, conversation_id=cid,
                                      turn=turn, seq=seq, event=e["event"], data=e["data"]))
        db.commit()
        return turn
    finally:
        db.close()


def _validator_view(raw) -> tuple[str, dict, list[str]]:
    """What the pure contract says about ``raw`` — the differential oracle."""
    content, display, errors = contract.validate(raw, _catalog())
    if contract.serialized_bytes(display) > contract.PROPOSAL_MAX_BYTES:
        return "invalid", {"_rejected": "normalized proposal exceeds the size limit"}, errors
    return ("draft" if content is not None and not errors else "invalid"), display, errors


def _assert_matches_validator(revision: dict, raw, *, where: str) -> None:
    status, display, errors = _validator_view(raw)
    note(f"{where}: validator says {status} {errors[:2]}")
    assert revision["status"] == status, (where, revision["status"], status, errors,
                                          revision["validation_errors"])
    assert _canon(revision["content"]) == _canon(display), where
    assert revision["validation_errors"] == errors, (where, revision["validation_errors"], errors)


def _assert_decidable(revision: dict, *, where: str) -> None:
    """Validator idempotence on what the row stores (metamorphic): a ``draft`` re-validates
    clean to the same bytes; an ``invalid`` row keeps reporting errors."""
    content = revision["content"]
    if not isinstance(content, dict) or "_rejected" in content:
        # a marker revision is born ``invalid`` and may be superseded or rejected later
        assert revision["status"] in ("invalid", "superseded", "rejected"), where
        return
    parsed, display, errors = contract.validate(content, _catalog())
    if revision["status"] == "draft":
        assert parsed is not None and errors == [], (where, errors)
        assert _canon(display) == _canon(content), where
    elif revision["status"] == "invalid":
        assert errors, (where, "an invalid revision re-validates clean")
    # superseded / rejected / approved rows are historical: only decidable, never raising


# ---------------------------------------------------------------------------
# the machine
# ---------------------------------------------------------------------------


class AssistantMachine(RuleBasedStateMachine):
    """One conversation, driven through the real routes."""

    def __init__(self, client, harness):
        super().__init__()
        reset_assistant_world()
        self.client = client
        self.harness = harness
        self.cid = _open(client)
        self.turns = 0
        self.approved: int | None = None  # the approved revision
        self.agent_id: str | None = None
        self.job_id: str | None = None
        self.deployed = False
        self.canary_id: str | None = None
        self.cleared = False
        self.clear_job: str | None = None
        self.blocked_clears = 0

    # -- views ---------------------------------------------------------------
    def detail(self) -> dict:
        return _latest(self.client, self.cid)

    def proposals(self) -> list[dict]:
        return sorted(self.detail()["proposals"], key=lambda p: p["revision"])

    def latest(self) -> dict | None:
        rows = self.proposals()
        return rows[-1] if rows else None

    def patch_base(self) -> dict | None:
        """The newest revision with real content (``service.patch_base``'s rule)."""
        for p in reversed(self.proposals()):
            if isinstance(p["content"], dict) and "_rejected" not in p["content"]:
                return p
        return None

    def approved_resources(self) -> dict[str, set[str]] | None:
        if self.approved is None:
            return None
        row = next(p for p in self.proposals() if p["revision"] == self.approved)
        return {k: set(row["content"].get(k) or []) for k in RESOURCE_KEYS}

    def expect_new_revision(self, before: list[dict], where: str) -> dict:
        after = self.proposals()
        assert len(after) == len(before) + 1, (where, [p["revision"] for p in after])
        new = after[-1]
        assert new["revision"] == (before[-1]["revision"] if before else 0) + 1, where
        assert new["status"] in ("draft", "invalid"), (where, new["status"])
        assert bool(new["validation_errors"]) == (new["status"] == "invalid"), where
        return new

    # -- rules: turns ----------------------------------------------------------
    @precondition(lambda self: not self.cleared)
    @rule(kind=st.sampled_from(["prose", "valid", "invalid_shape", "invalid_ref", "patch"]),
          name=st.from_regex(r"^[a-z][a-z0-9-]{2,12}$", fullmatch=True),
          prompt=st.text(min_size=1, max_size=60),
          iterations=st.integers(min_value=1, max_value=100),
          memory=st.sampled_from(["disabled", "workspace"]),
          op=st.sampled_from(["replace_prompt", "replace_name", "append_assumption",
                              "test_wrong", "stale_base"]))
    def user_turn(self, kind, name, prompt, iterations, memory, op):
        before = self.proposals()
        base = self.patch_base()
        raw = None
        if kind == "prose":
            self.harness.reply("Tell me more about the audience.")
        elif kind == "valid":
            raw = {**VALID_PROPOSAL, "name": name, "system_prompt": prompt,
                   "max_iterations": iterations, "memory": memory}
            self.harness.reply(_block(raw))
        elif kind == "invalid_shape":
            raw = {**VALID_PROPOSAL, "version": 2, "name": name}
            self.harness.reply(_block(raw))
        elif kind == "invalid_ref":
            raw = {**VALID_PROPOSAL, "name": name, "tools": ["gateway:nope"]}
            self.harness.reply(_block(raw))
        else:  # a change against the stored base
            if base is None:
                return  # "there is no stored proposal to change yet" — nothing to model
            ops = {
                "replace_prompt": [{"op": "replace", "path": "/system_prompt", "value": prompt}],
                "replace_name": [{"op": "replace", "path": "/name", "value": name}],
                "append_assumption": [{"op": "add", "path": "/assumptions/-", "value": prompt}],
                "test_wrong": [{"op": "test", "path": "/name", "value": "never-this-name"}],
                "stale_base": [{"op": "replace", "path": "/name", "value": name}],
            }[op]
            base_rev = base["revision"] - 1 if op == "stale_base" else base["revision"]
            change = {"base_revision": base_rev, "operations": ops}
            self.harness.reply(f"```{contract.PATCH_FENCE}\n{json.dumps(change)}\n```")
            raw, perrors = contract.apply_patch(base["content"], change,
                                                base_revision=base["revision"])
            if raw is None:
                raw = ("unusable", perrors)
        events = _turn(self.client, self.cid, f"turn {self.turns + 1}: {prompt}")
        self.turns += 1
        kinds = [k for k, _ in events]
        assert kinds[-1] == "done", kinds
        if kind == "prose":
            assert "proposal" not in kinds and self.proposals() == before
            return
        new = self.expect_new_revision(before, f"model turn {kind}/{op}")
        assert new["source"] == "model"
        if isinstance(raw, tuple):  # a change that does not apply: an invalid marker revision
            assert new["status"] == "invalid" and "_rejected" in new["content"], new
            assert new["validation_errors"] == raw[1], (new["validation_errors"], raw[1])
            assert self.patch_base()["revision"] == base["revision"], "the base moved"
            return
        prep = (_conversation(self.cid).preparation or {})
        approved = self.approved_resources()
        effective = dict(raw)
        if approved is not None:
            # effective_raw: a model proposal after approval silently keeps the approved lists
            row = next(p for p in self.proposals() if p["revision"] == self.approved)
            effective.update({k: list(row["content"].get(k) or []) for k in RESOURCE_KEYS})
        elif prep.get("selection_set") and contract.parse_content(raw)[0] is not None:
            # explicit member selections are authoritative over a well-shaped model block
            effective = {**contract.content_dump(contract.parse_content(raw)[0]),
                         "skills": prep.get("skills", []),
                         "knowledge_bases": prep.get("knowledge_bases", [])}
            if prep.get("tools_selection_set"):
                effective["tools"] = prep.get("tools", [])
        _assert_matches_validator(new, effective, where=f"model turn {kind}/{op}")

    # -- rules: member edit ----------------------------------------------------
    @precondition(lambda self: not self.cleared)
    @rule(edits=_edits_strategy(), seed_mode=st.sampled_from(["keep", "absent", "null"]))
    def member_edit(self, edits, seed_mode):
        base = self.patch_base()
        if base is None:
            return
        before = self.proposals()
        submission = {**copy.deepcopy(base["content"]), **edits}
        if seed_mode == "absent":
            submission.pop("evaluation_plan", None)
        elif seed_mode == "null":
            submission["evaluation_plan"] = None
        too_large = contract.serialized_bytes(submission) > contract.PROPOSAL_MAX_BYTES
        approved = self.approved_resources()
        locked = approved is not None and any(
            not isinstance(submission.get(k, []), list)
            or not all(isinstance(v, str) for v in submission.get(k, []))
            or set(submission.get(k, [])) != approved[k]
            for k in RESOURCE_KEYS
        )
        res = self.client.put(f"{CONV_URL}/{self.cid}/proposal", json={"content": submission})
        note(f"member edit {sorted(edits)} seed={seed_mode} → {res.status_code}")
        if too_large:
            assert res.status_code == 413 and res.json()["code"] == "assistant.proposal_too_large"
            assert self.proposals() == before
            return
        if locked:
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "assistant.resources_locked", res.text
            assert self.proposals() == before
            return
        assert res.status_code == 200, res.text
        new = self.expect_new_revision(before, "member edit")
        assert new["source"] == "member"
        # the N15-obs13 carry: absent → carried + note; null → removed, no note; object → as is
        latest_seed = base["content"].get("evaluation_plan") if base is before[-1] else None
        prior = before[-1]["content"] if isinstance(before[-1]["content"], dict) else {}
        latest_seed = prior.get("evaluation_plan")
        carried = seed_mode == "absent" and latest_seed is not None
        expected_raw = ({**submission, "evaluation_plan": copy.deepcopy(latest_seed)}
                        if carried else submission)
        repaired = [n for n in new["notes"] if REPAIRED.match(n)]
        assert bool(repaired) == carried, (new["notes"], seed_mode, latest_seed)
        _assert_matches_validator(new, expected_raw, where="member edit")
        # ROUND-TRIP: every key the member did not touch is byte-identical — against a base
        # that IS canonical. N17-obs152 (TEST, r17-fixA): `patch_base` may be an `invalid`
        # revision, which stores the raw block as it came (no canonical form exists for
        # content that does not parse); the first edit that makes it valid stores the
        # contract's display, and that re-serialises the untouched keys (a minimal golden test
        # gains its defaulted fields). The differential oracle above already pins that display
        # byte for byte, so the byte-identical rule applies when the base itself re-validates
        # clean to its own bytes (`_assert_decidable`'s draft rule) — the product's real
        # semantics, not a weakening: for a draft base nothing changed.
        base_canonical = _validator_view(base["content"])[0] == "draft"
        if base_canonical and isinstance(new["content"], dict) \
                and "_rejected" not in new["content"]:
            for key in CONTRACT_KEYS:
                if key in edits or key == "evaluation_plan":
                    continue
                if key in base["content"]:
                    assert _canon(new["content"].get(key)) == _canon(base["content"][key]), key
            if seed_mode == "null":
                assert new["content"].get("evaluation_plan") is None
            elif seed_mode == "keep" and "evaluation_plan" not in edits \
                    and "evaluation_plan" in base["content"]:
                assert _canon(new["content"]["evaluation_plan"]) == _canon(
                    base["content"]["evaluation_plan"])

    # -- rules: approve / reject -------------------------------------------------
    @precondition(lambda self: not self.cleared)
    @rule(target_kind=st.sampled_from(["latest", "latest", "older", "wrong_hash", "unknown"]))
    def approve(self, target_kind):
        rows = self.proposals()
        if not rows:
            return
        latest = rows[-1]
        agents_before = {a.id for a in _agents_of(self.cid)}
        if (self.approved is not None and latest["status"] == "draft"
                and target_kind == "latest"):
            # N16-obs40 (fixed in cn/r16-l): ONE Agent per conversation — a later valid draft
            # is refused with 409 `already_approved` naming the approved revision, no row
            body = {"revision": latest["revision"], "content_hash": latest["content_hash"]}
            res = self.client.post(f"{CONV_URL}/{self.cid}/proposal/approve", json=body)
            note(f"approve second creation rev {body['revision']} → {res.status_code}")
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "assistant.already_approved", res.text
            assert res.json()["detail"]["agent_id"] == self.agent_id
            assert res.json()["detail"]["revision"] == self.approved
            assert {a.id for a in _agents_of(self.cid)} == agents_before
            fresh = next(p for p in self.proposals() if p["revision"] == latest["revision"])
            assert fresh["status"] == "draft"  # the refusal is not a transition
            return
        if target_kind == "older" and len(rows) > 1:
            target = rows[-2]
            body = {"revision": target["revision"], "content_hash": target["content_hash"]}
        elif target_kind == "wrong_hash":
            target = latest
            body = {"revision": latest["revision"], "content_hash": "0" * 64}
        elif target_kind == "unknown":
            target = None
            body = {"revision": latest["revision"] + 7, "content_hash": latest["content_hash"]}
        else:
            target = latest
            body = {"revision": latest["revision"], "content_hash": latest["content_hash"]}
        res = self.client.post(f"{CONV_URL}/{self.cid}/proposal/approve", json=body)
        note(f"approve {target_kind} rev {body['revision']} ({target and target['status']}) "
             f"→ {res.status_code} {res.json().get('code')}")
        assert res.status_code != 500, res.text
        if target is None or body["content_hash"] != target["content_hash"]:
            assert res.status_code == 409 and res.json()["code"] == "assistant.proposal_stale"
        elif target["status"] == "approved":
            assert res.status_code == 200 and res.json()["job_id"] == self.job_id
            assert res.json()["agent"]["id"] == self.agent_id
        elif target["status"] != "draft":
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "assistant.proposal_not_approvable", res.text
        elif target is not latest:
            assert res.status_code == 409 and res.json()["code"] == "assistant.proposal_stale"
        else:
            assert res.status_code == 202, res.text
            body = res.json()
            assert body["started"] is True and body["job_id"] and body["agent"]["id"]
            self.approved = target["revision"]
            self.agent_id = body["agent"]["id"]
            self.job_id = body["job_id"]
            self.deployed = False
            fresh = next(p for p in self.proposals() if p["revision"] == target["revision"])
            assert fresh["status"] == "approved"
        agents_after = {a.id for a in _agents_of(self.cid)}
        assert agents_after >= agents_before

    @precondition(lambda self: not self.cleared)
    @rule(which=st.sampled_from(["latest", "latest", "older"]))
    def reject(self, which):
        rows = self.proposals()
        if not rows:
            return
        target = rows[-2] if which == "older" and len(rows) > 1 else rows[-1]
        res = self.client.post(f"{CONV_URL}/{self.cid}/proposal/reject",
                               json={"revision": target["revision"]})
        note(f"reject {which} rev {target['revision']} ({target['status']}) → {res.status_code}")
        if target is not rows[-1]:
            assert res.status_code == 409 and res.json()["code"] == "assistant.proposal_stale"
        elif target["status"] == "approved":
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "assistant.proposal_already_approved"
        else:
            assert res.status_code == 200, res.text
            assert res.json()["proposal"]["status"] == "rejected"
            assert res.json()["proposal"]["revision"] == target["revision"]

    # -- rules: the deployed Agent and its canary --------------------------------
    @precondition(lambda self: self.approved is not None and not self.deployed
                  and not self.cleared)
    @rule()
    def deploy_completes(self):
        _finish_deploy(self.agent_id, self.job_id)
        self.deployed = True

    @precondition(lambda self: self.deployed and self.canary_id is None and not self.cleared)
    @rule()
    def start_canary(self):
        db = SessionLocal()
        try:
            agent = db.get(Agent, self.agent_id)
            row = _canary(db, agent)
            db.commit()
            self.canary_id = row.id
        finally:
            db.close()

    # -- rules: CLEAR ------------------------------------------------------------------
    @precondition(lambda self: not self.cleared)
    @rule()
    def clear(self):
        res = self.client.delete(f"{CONV_URL}/{self.cid}")
        note(f"clear → {res.status_code} {res.json().get('code') or res.json().get('status')}")
        if self.approved is None:
            # transcript-only: deleted inline (assistant-turns.md §10)
            assert res.status_code == 200 and res.json()["deleted"] is True, res.text
            self.cleared = True
            self.assert_purged()
            return
        if not self.deployed:
            # the live deploy job is a blocker: nothing is deleted
            assert res.status_code == 409, res.text
            assert res.json()["code"] == "assistant.conversation_busy", res.text
            assert [b["kind"] for b in res.json()["detail"]["blockers"]] == ["job"]
            assert _rows(self.cid)["conversation"] == 1
            self.blocked_clears += 1
            return
        assert res.status_code == 202, res.text
        self.clear_job = res.json()["job_id"]
        job = wait_job(self.clear_job)
        assert job.status == "succeeded", (job.status, job.error, job.payload.get("blocker"))
        self.cleared = True
        self.assert_purged()

    def assert_purged(self):
        counts = _rows(self.cid)
        assert all(v == 0 for v in counts.values()), counts
        assert self.client.get(f"{CONV_URL}/{self.cid}").status_code == 404
        # a repeated DELETE of a gone conversation is the ordinary not-found, not a job
        again = self.client.delete(f"{CONV_URL}/{self.cid}")
        assert again.status_code == 404, again.text
        if self.agent_id:
            db = SessionLocal()
            try:
                agent = db.get(Agent, self.agent_id)
                assert agent.status == "deleted", agent.status
                if self.canary_id:
                    canary = db.get(RuntimeCanary, self.canary_id)
                    assert canary.status == "cleaned" and canary.running_action is None
                live = [j for j in db.query(Job).filter(Job.type == purge.JOB_TYPE).all()
                        if (j.payload or {}).get("conversation_id") == self.cid
                        and j.status in purge.LIVE_JOB_STATUSES]
                assert live == []
            finally:
                db.close()

    # -- rules: restart / dead process ---------------------------------------------
    @rule()
    def restart(self):
        snapshot = None if self.cleared else self.proposals()
        create_app(resume_jobs=True)
        _mark_ready()  # init_db mirrors the (smaller) test yaml onto the workspace row
        if self.clear_job:
            wait_job(self.clear_job)
        # the recorded deploy starter leaves an approved-but-undeployed job ``queued``, so
        # the resume keeps offering exactly that one; nothing else is ever left to resume
        pending = [] if (self.approved is None or self.deployed) else [self.job_id]
        assert pipeline.resume_pending_jobs() == pending
        if not self.cleared:
            assert self.proposals() == snapshot, "a restart changed a proposal"
            assert _conversation(self.cid).active_turn is None
        else:
            self.assert_purged()

    @precondition(lambda self: not self.cleared)
    @rule(shape=st.sampled_from(["prose", "complete_valid", "complete_invalid", "half"]),
          name=st.from_regex(r"^[a-z][a-z0-9-]{2,12}$", fullmatch=True))
    def interrupted_turn(self, shape, name):
        """The process died mid-turn; the next startup sweep finalizes it from the log."""
        before = self.proposals()
        prose = "Design notes for the helpdesk agent. "
        raw = {**VALID_PROPOSAL, "name": name}
        if shape == "complete_invalid":
            raw["tools"] = ["gateway:nope"]
        block = _block(raw)
        streamed = {
            "prose": prose,
            "complete_valid": prose + block,
            "complete_invalid": prose + block,
            "half": prose + block[: len(block) // 2],
        }[shape]
        turn = _seed_dead_turn(self.cid, streamed)
        self.turns = turn
        assert service.clear_stale_turn_claims() == 1
        assert _conversation(self.cid).active_turn is None
        rows = [(t, role, nm) for t, role, nm in _messages(self.cid) if t == turn]
        assert (turn, "error", service.TURN_INTERRUPTED_NAME) in rows, rows
        assert rows.count((turn, "error", service.TURN_INTERRUPTED_NAME)) == 1
        events = _events(self.cid, turn)
        assert events[-1][1] == "error"
        assert [s for s, _ in events] == list(range(1, len(events) + 1))
        after = self.proposals()
        if shape.startswith("complete"):
            new = self.expect_new_revision(before, f"interrupted {shape}")
            approved = self.approved_resources()
            effective = dict(raw)
            if approved is not None:
                row = next(p for p in after if p["revision"] == self.approved)
                effective.update({k: list(row["content"].get(k) or []) for k in RESOURCE_KEYS})
                _assert_matches_validator(new, effective, where=f"interrupted {shape}")
            elif not (_conversation(self.cid).preparation or {}).get("selection_set"):
                _assert_matches_validator(new, effective, where=f"interrupted {shape}")
        else:
            assert after == before, "a cut fence yielded a revision"
        # idempotent: a second sweep finds nothing
        assert service.clear_stale_turn_claims() == 0
        assert rows == [(t, role, nm) for t, role, nm in _messages(self.cid) if t == turn]

    # -- invariants ------------------------------------------------------------------
    @invariant()
    def revisions_are_contiguous_and_one_is_open(self):
        if self.cleared:
            return
        rows = self.proposals()
        assert [p["revision"] for p in rows] == list(range(1, len(rows) + 1))
        open_rows = [p for p in rows if p["status"] not in FINAL_STATUSES]
        assert len(open_rows) <= 1, [(p["revision"], p["status"]) for p in rows]
        if open_rows:
            assert open_rows[0] is rows[-1]
        for p in rows:
            assert p["status"] in ("draft", "invalid", *FINAL_STATUSES), p["status"]
            _assert_decidable(p, where=f"revision {p['revision']}")

    @invariant()
    def one_agent_per_conversation(self):
        """architecture.md: a conversation "ends in an inert, reviewable proposal for ONE new
        managed-Harness business agent" and after approval "no longer offers another
        creation approval in that conversation"."""
        if self.cleared:
            return
        agents = _agents_of(self.cid)
        assert len(agents) <= 1, [(a.id, a.name, a.status) for a in agents]

    @invariant()
    def no_claim_between_steps(self):
        if not self.cleared:
            assert _conversation(self.cid).active_turn is None

    @invariant()
    def ledger_has_no_orphans(self):
        """The shared scan, restricted to the rows this machine owns: another test's
        detached turn driver may land rows in this worker's ledger after its own table
        wipe (cross-test leakage the scan would otherwise blame on this walk)."""
        own = {self.cid, self.agent_id, self.job_id, self.canary_id, self.clear_job} - {None}
        found = [v for v in scan_ledger(SessionLocal) if any(i in v for i in own)]
        assert found == []


def _run(client, harness, *, examples: int, steps: int) -> None:
    run_state_machine_as_test(
        lambda: AssistantMachine(client, harness),
        settings=settings(max_examples=examples, stateful_step_count=steps, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]),
    )


# `cloud` before `ready`: its create_app() re-mirrors the yaml resources onto the
# workspace row, which would drop the gateway_id `ready` sets (r16-a's creation check)
def test_assistant_state_machine(client, cloud, ready, harness):
    _run(client, harness, examples=3 * SCALE, steps=24 * SCALE)


@settings(max_examples=1, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.none())  # one deterministic example: the rules call `note`, legal only under Hypothesis
def test_N17_obs152_member_edit_of_an_invalid_base_stores_the_canonical_display(
    client, cloud, ready, harness, _none,
):
    """N17-obs152 (TEST — the deterministic replay of the gate's Hypothesis example on
    cn/r17-n, 2026-10-20). The model turn `invalid_shape` leaves an `invalid` revision whose
    content is the RAW block (`version: 2`, golden tests in the minimal shape); a member edit
    that fixes only `version` makes it a draft, and the product stores the contract's display —
    the golden tests come back in their canonical, fully-defaulted shape. The ROUND-TRIP oracle
    used to demand byte identity with the raw base; the differential oracle (stored content ==
    validator display of the submission) is the product's rule and holds here. The step list is
    the shrunk example (`/tmp/r17n-verify.log` 251–311); the restarts are kept as found."""
    state = AssistantMachine(client, harness)
    try:
        state.member_edit(edits={"requirements_baseline": [], "version": 1, "name": "HR"},
                          seed_mode="keep")
        state.member_edit(edits={"name": "a00", "golden_tests": [], "assumptions": [],
                                 "tools": []}, seed_mode="keep")
        state.user_turn(iterations=11, kind="invalid_shape", memory="disabled", name="hksut30",
                        op="replace_prompt", prompt="0")
        base = state.patch_base()
        assert base is not None and base["status"] == "invalid", base
        assert base["content"]["version"] == 2
        raw_gts = base["content"]["golden_tests"]
        assert raw_gts == VALID_PROPOSAL["golden_tests"]  # the raw minimal shape, stored as is
        for _ in range(4):
            state.restart()
        before = state.proposals()
        state.member_edit(edits={"version": 1}, seed_mode="keep")  # the fixed oracle holds
        new = state.proposals()[-1]
        assert len(state.proposals()) == len(before) + 1 and new["status"] == "draft"
        _, display, errors = _validator_view({**base["content"], "version": 1})
        assert errors == [] and _canon(new["content"]) == _canon(display)
        canonical_gts = new["content"]["golden_tests"]
        assert [g["id"] for g in canonical_gts] == [g["id"] for g in raw_gts]
        assert _canon(canonical_gts) != _canon(raw_gts)  # re-serialised: the defaulted fields
        assert set(canonical_gts[0]) > set(raw_gts[0])
        _assert_decidable(new, where="obs152 replay")
    finally:
        state.teardown()


def test_a_second_creation_approval_does_not_mint_a_second_agent(client, ready, harness):
    """N16-obs40 (fixed in cn/r16-l): approve a valid draft, let the model propose a valid
    revision under another name, approve it → 409 ``assistant.already_approved`` naming the
    approved revision and its Agent; exactly ONE Agent row and ONE deploy job exist, the new
    draft stays a draft, and the historical approval's idempotent retry still answers 200
    (architecture.md: the conversation proposes ONE new agent and after approval "no longer
    offers another creation approval"; "Historical approvals and their idempotent outcomes
    remain intact")."""
    cid = _open(client)
    harness.reply(_block(VALID_PROPOSAL))
    first = next(d for k, d in _turn(client, cid, "propose") if k == "proposal")
    approved = _approve(client, cid, first)
    assert approved.status_code == 202
    harness.reply(_block({**VALID_PROPOSAL, "name": "hr-helpdesk-two"}))
    second = next(d for k, d in _turn(client, cid, "again") if k == "proposal")
    assert second["status"] == "draft", second["validation_errors"]
    res = _approve(client, cid, second)
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "assistant.already_approved"
    assert body["detail"]["agent_id"] == approved.json()["agent"]["id"]
    assert body["detail"]["revision"] == first["revision"]
    assert body["detail"]["job_id"] == approved.json()["job_id"]
    agents = _agents_of(cid)
    assert [(a.name, a.status) for a in agents] == [(VALID_PROPOSAL["name"], "deploying")]
    db = SessionLocal()
    try:
        jobs = db.query(Job).filter(Job.type == "deploy_agent").all()
        assert [j.id for j in jobs] == [approved.json()["job_id"]]
    finally:
        db.close()
    statuses = [p["status"] for p in _latest(client, cid)["proposals"]]
    assert statuses == ["approved", "draft"]
    # the historical approval stays idempotent
    again = _approve(client, cid, first)
    assert again.status_code == 200 and again.json()["started"] is False


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
# `cloud` before `ready`: its create_app() re-mirrors the yaml resources onto the
# workspace row, which would drop the gateway_id `ready` sets (r16-a's creation check)
def test_soak_assistant_state_machine(client, cloud, ready, harness):
    _run(client, harness, examples=20, steps=80)


# ---------------------------------------------------------------------------
# properties outside the machine
# ---------------------------------------------------------------------------


@settings(max_examples=25 * SCALE, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(edits=_edits_strategy(), seed_mode=st.sampled_from(["keep", "absent", "null"]),
       seeded=st.booleans())
def test_member_edit_round_trips_every_unedited_key(client, ready, harness, edits, seed_mode,
                                                    seeded):
    """ROUND-TRIP (the N15-obs13 escape class): a member edit of ANY subset of the 22 keys
    leaves every other key byte-identical, stores exactly what the pure validator says, and
    carries the previous seed with a ``proposal_repaired`` note exactly when the editor
    omitted ``evaluation_plan`` while the previous revision had one."""
    cid = _open(client)
    base_raw = {**VALID_PROPOSAL, **({"evaluation_plan": EMPTY_SEED} if seeded else {})}
    harness.reply(_block(base_raw))
    events = _turn(client, cid, "propose")
    base = next(d for k, d in events if k == "proposal")
    assert base["status"] == "draft", base["validation_errors"]
    submission = {**copy.deepcopy(base["content"]), **edits}
    if seed_mode == "absent":
        submission.pop("evaluation_plan", None)
    elif seed_mode == "null":
        submission["evaluation_plan"] = None
    assume(contract.serialized_bytes(submission) <= contract.PROPOSAL_MAX_BYTES)
    res = client.put(f"{CONV_URL}/{cid}/proposal", json={"content": submission})
    assert res.status_code == 200, res.text
    new = res.json()["proposal"]
    assert new["revision"] == 2 and new["source"] == "member"
    carried = seed_mode == "absent" and base["content"].get("evaluation_plan") is not None
    expected = ({**submission, "evaluation_plan": base["content"]["evaluation_plan"]}
                if carried else submission)
    assert bool([n for n in new["notes"] if REPAIRED.match(n)]) == carried, new["notes"]
    _assert_matches_validator(new, expected, where="round trip")
    if isinstance(new["content"], dict) and "_rejected" not in new["content"]:
        for key in CONTRACT_KEYS:
            if key in edits or key == "evaluation_plan" or key not in base["content"]:
                continue
            assert _canon(new["content"][key]) == _canon(base["content"][key]), key
    statuses = [p["status"] for p in _latest(client, cid)["proposals"]]
    assert statuses == ["superseded", new["status"]]


_WORDS = ["answers", "HR", "policy", "questions", "for", "employees", "with", "the", "KB"]


@settings(max_examples=12 * SCALE, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(words=st.lists(st.sampled_from(_WORDS), min_size=2, max_size=8),
       spacing=st.sampled_from([" ", "  ", "\n", " , "]),
       casing=st.sampled_from([str, str.upper, str.lower, str.title]),
       suffix=st.sampled_from(["", ".", "!", "，请尽快"]))
def test_paraphrased_requirements_map_to_the_same_proposal_shape_class(
    client, ready, harness, words, spacing, casing, suffix,
):
    """METAMORPHIC (stubbed model): two requirement texts that differ only in wording
    (spacing, case, punctuation, a CJK tail) reach the architect with an identical
    protocol preamble and produce revisions of the same shape class — same normalized
    content, bindings, status and hash — because "A proposal is checked when it is
    recorded against everything the member will later meet" (the content, never the
    prose that asked for it)."""
    a_text = " ".join(words) + suffix
    b_text = casing(spacing.join(words)) + suffix
    assume(a_text != b_text)
    shapes = []
    payloads = []
    for text in (a_text, b_text):
        cid = _open(client)
        harness.reply(_block(VALID_PROPOSAL))
        events = _turn(client, cid, text)
        proposal = next(d for k, d in events if k == "proposal")
        shapes.append((proposal["status"], proposal["content_hash"], _canon(proposal["content"]),
                       _canon(proposal["bindings"]), proposal["validation_errors"]))
        payloads.append(json.loads(harness.calls[-1]["payload"]))
    assert shapes[0] == shapes[1]
    # the request differs only in the member's own text
    msgs_a, msgs_b = payloads[0]["messages"], payloads[1]["messages"]
    assert len(msgs_a) == len(msgs_b) == 1
    text_a, text_b = msgs_a[0]["content"][0]["text"], msgs_b[0]["content"][0]["text"]
    # N16-obs94: `replace` strips EVERY occurrence — a requirement such as "for the" also appears in
    # the protocol boilerplate, so the two sides would strip different amounts of fixed text. Only
    # compare when each requirement occurs exactly once (the oracle, not the builder, was wrong).
    assume(text_a.count(a_text) == 1 and text_b.count(b_text) == 1)
    assert text_a.replace(a_text, "") == text_b.replace(b_text, "")
    assert a_text in text_a and b_text in text_b


# `cloud` before `ready`: its create_app() re-mirrors the yaml resources onto the
# workspace row, which would drop the gateway_id `ready` sets (r16-a's creation check)
def test_clear_resumes_after_a_restart_and_leaves_nothing_to_resume(client, cloud, ready, harness):
    """assistant-turns.md §10: ``resume_pending_jobs`` re-adopts a ``running`` purge row
    after a restart; the second start finds nothing; a repeated DELETE is 404."""
    cid = _open(client)
    harness.reply(_block(VALID_PROPOSAL))
    proposal = next(d for k, d in _turn(client, cid, "propose") if k == "proposal")
    res = _approve(client, cid, proposal)
    assert res.status_code == 202, res.text
    _finish_deploy(res.json()["agent"]["id"], res.json()["job_id"])
    db = SessionLocal()
    try:
        row = db.get(AssistantConversation, cid)
        job, done = purge.request_purge(db, row, can=lambda _p: True)
        assert done is None
        job = db.get(Job, job.id)
        job.status = "running"  # the process died right after adopting the job
        db.commit()
        job_id = job.id
    finally:
        db.close()
    create_app(resume_jobs=True)
    job = wait_job(job_id)
    assert job.status == "succeeded", (job.status, job.error)
    assert all(v == 0 for v in _rows(cid).values())
    assert pipeline.resume_pending_jobs() == []
    assert client.delete(f"{CONV_URL}/{cid}").status_code == 404
    assert _agents_of(cid) == []
