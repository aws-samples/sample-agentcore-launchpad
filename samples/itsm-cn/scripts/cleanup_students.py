"""Per-student cleanup after a class: remove what the kit (and the learner) created, keep
the pre-provisioned base.

    python3 cleanup_students.py --students lab02,lab03            # dry run (default)
    python3 cleanup_students.py --students lab02,lab03 --apply
    python3 cleanup_students.py --all-but lab01 --apply [--keep-staff] [--trim-config]

For every requested student the script reads the manifest row ``students[labNN]`` and the
live console (as the configured administrator) and removes, in dependency order:

1. **Evaluation runs (active).** Runs on the student's Agents or datasets that are still
   ``queued`` / ``invoking`` / ``waiting`` / ``evaluating`` are stopped first: the Agent
   delete refuses ``409 agent.in_use`` while one is active.
2. **Agents.** The kit's console / eval Agents named on the manifest row, the ``agents.py``
   employee Agent recorded under ``agents[labNN]``, every live Agent named
   ``<student>-itdesk-cn`` / ``<student>-itdesk-console-cn`` / ``<student>-itdesk-eval-cn`` /
   ``<student>-sim-cn``, and every Agent whose creator is the student or (unless the staff
   account is kept) their staff account. ``DELETE /api/agents/{id}`` tears the Runtime,
   role, logs, artifacts, registry record AND the Agent's long-term memory records down;
   the script polls ``GET /api/agents/{id}`` until it answers 404. A ``409
   agent.deploy_in_progress`` / ``agent.in_use`` is retried for a while, then reported.
3. **Evaluation runs (finished) and datasets.** ``failed`` / ``stopped`` runs of those Agents
   and datasets are deleted; ``completed`` runs are evaluation history the API keeps (``409
   run.not_deletable``) and are listed as kept. The two personal datasets go with
   ``DELETE /api/eval/datasets/{id}?force=true`` (administrator override of ``dataset.in_use``).
4. **Personal knowledge base.** ``DELETE /api/knowledge-bases/{kb_id}`` removes the ledger
   row, the ``kb/<kb_id>/`` S3 prefix and the OpenSearch index (``knowledge_selfhosted
   .delete_kb`` + ``kb_cleanup.STEPS``; a pending step is a retriable ``kb_cleanup`` job
   the response names). Never forced: a KB still mounted by an Agent that is not the
   student's is reported, not stripped.
5. **Memory records.** Long-term records of the actors ``<agent_id>__*`` of the deleted
   Agents are swept through ``GET /api/memory/actors`` → ``GET /api/memory/records`` →
   ``DELETE /api/memory/records/{id}?actor_id=`` (administrator); a workspace without the
   memory feature, or an actor with no namespace, is skipped. Chat sessions have no delete
   route; they become unreachable with their Agent (``404 agent.not_found``).
6. **Accounts.** ``DELETE /api/users/{id}`` for the student and — unless ``--keep-staff`` is
   given or the manifest marks the staff row ``shared`` / ``referenced`` — the staff
   account. The SSM credential parameters are kept (prepare_console.py / the kit reuse
   them). ``staff_accounts`` / ``staff_publish_targets`` of the lab config are NOT edited
   unless ``--trim-config`` is given (atomic rewrite with a ``.bak``); either way the
   organizer re-runs ``refresh_service.py`` and ``prepare_policy.py`` afterwards.
7. **Ledger.** ``students[labNN]``, ``users[labNN]``, ``users[<staff>]`` (when deleted) and
   ``agents[labNN]`` leave the manifest under the manifest lock; the card
   ``<state_dir>/cards/labNN.md`` is deleted; ``evidence/cleanup-labNN.json`` records what
   happened.

Never deleted, whatever a manifest row says: the shared knowledge base
``anypharma-it-helpdesk-cn``, the lab Gateway, the policy engine and its policies, the
Lambda / synthetic service, the Skills, the reference Agent ``itdesk-console-cn`` (and
``agents[<first staff>]``), system presets, the organizer accounts (``admin``,
``workshop-admin``, ``itdesk01``, the configured administrator, the first staff account) and
anything not recorded for — or named after — the requested student. A requested student
missing from the manifest is refused (exit 2). A half-cleaned student (resources already
gone) finishes cleanly: every 404 is "already gone".
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from student_kit_lib import SHARED_KB, STUDENTS, resource_names, staff_for

ORGANIZER_ACCOUNTS = ("admin", "workshop-admin", "itdesk01")
REFERENCE_AGENT = "itdesk-console-cn"
ACTIVE_RUN_STATUSES = ("queued", "invoking", "waiting", "evaluating")
DELETABLE_RUN_STATUSES = ("failed", "stopped")
RETRY_CODES = ("agent.deploy_in_progress", "agent.in_use", "aws.conflict")
GONE_POLL_SECONDS = 3
GONE_POLL_ROUNDS = 100
CONFLICT_RETRY_SECONDS = 10
CONFLICT_RETRY_ROUNDS = 30
PAGE = 200


def agent_name_patterns(student: str) -> tuple[str, ...]:
    """Every per-student Agent name the manual, the kit or the simulator may have created."""
    names = resource_names(student)
    return (names["employee_agent"], names["console_agent"], names["eval_agent"],
            f"{student}-sim-cn")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    who = p.add_mutually_exclusive_group(required=True)
    who.add_argument("--students", help="comma-separated learners to clean, e.g. lab02,lab03")
    who.add_argument("--all-but", metavar="STUDENTS",
                     help="clean every learner in the manifest except these (comma-separated)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="read the live state and print the plan; mutate nothing (default)")
    mode.add_argument("--apply", action="store_true", help="perform the deletions")
    p.add_argument("--keep-staff", action="store_true",
                   help="keep the student's service-desk account and the Agents it created"
                        " (the kit's console Agent still goes)")
    p.add_argument("--trim-config", action="store_true",
                   help="also remove the deleted staff accounts from staff_accounts /"
                        " staff_publish_targets of LAUNCHPAD_LAB_CONFIG (atomic, .bak kept)")
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    args.students = _names(p, args.students)
    args.all_but = _names(p, args.all_but)
    if args.trim_config and args.keep_staff:
        p.error("--trim-config removes deleted staff accounts; it needs the staff deleted")
    return args


def _names(p, value):
    if value is None:
        return None
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names:
        p.error("expected at least one learner name")
    bad = [n for n in names if n not in STUDENTS]
    if bad:
        p.error(f"not a learner name (lab01-lab10): {', '.join(bad)}")
    return names


class Refused(Exception):
    """A request the script will not act on (exit 2)."""


# ----- HTTP ------------------------------------------------------------------------------
def request(lab, c, method, path, body=None, **kw):
    """One console call → ``(status, json)``; unlike ``lab.api`` a 4xx is data here (a 404
    is "already gone", a 409 names what still references the resource)."""
    r = c.request(method, lab.BASE + path, json=body, timeout=180, **kw)
    try:
        out = r.json()
    except ValueError:
        out = {"raw": (getattr(r, "text", "") or "")[:600]}
    return r.status_code, out


def paged(lab, c, path, key, *, params=""):
    """Every item of an offset-paginated list (``?limit=&offset=``)."""
    items, offset = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        status, out = request(lab, c, "GET", f"{path}{sep}limit={PAGE}&offset={offset}{params}")
        if status != 200:
            raise RuntimeError(f"GET {path}: HTTP {status}: {str(out)[:300]}")
        page = out.get(key) or []
        items.extend(page)
        total = out.get("total")
        offset += len(page)
        if not page or total is None or offset >= total:
            return items


# ----- the plan of one student ----------------------------------------------------------
class Protected:
    """Ids and names the script never deletes, from the config and the manifest."""

    def __init__(self, lab, manifest):
        staff = list(lab.CONFIG.get("staff_accounts") or [])
        self.users = set(ORGANIZER_ACCOUNTS) | {lab.CONFIG.get("admin_username") or "admin"}
        if staff:
            self.users.add(staff[0])
        self.agent_names = {REFERENCE_AGENT}
        self.agent_ids: set[str] = set()
        reference = (manifest.get("agents") or {}).get(staff[0] if staff else "") or {}
        if reference.get("id"):
            self.agent_ids.add(reference["id"])
        if reference.get("name"):
            self.agent_names.add(reference["name"])
        self.kb_names = {SHARED_KB}
        self.kb_ids = {(manifest.get("kb") or {}).get("kb_id")} - {None}
        self.dataset_ids = {
            (row or {}).get("id") for row in (manifest.get("datasets") or {}).values()
        } - {None}
        self.dataset_names = set(manifest.get("datasets") or {})

    def agent(self, item: dict) -> str | None:
        if item.get("system"):
            return "system preset"
        if item.get("id") in self.agent_ids or item.get("name") in self.agent_names:
            return "reference Agent"
        return None

    def kb(self, kb_id, name) -> str | None:
        return "shared knowledge base" if kb_id in self.kb_ids or name in self.kb_names else None

    def dataset(self, ds_id, name) -> str | None:
        if ds_id in self.dataset_ids or name in self.dataset_names:
            return "shared dataset"
        return None

    def user(self, username) -> str | None:
        return "organizer account" if username in self.users else None


class StudentCleanup:
    """Discover (read-only) and, with ``--apply``, remove one student's resources."""

    def __init__(self, lab, admin, manifest, student, *, keep_staff, apply, log=print):
        self.lab, self.c, self.m, self.student = lab, admin, manifest, student
        self.apply, self.log = apply, log
        self.row = (manifest.get("students") or {})[student]
        self.staff = (self.row.get("staff") or {}).get("username") or staff_for(student)
        staff_row = self.row.get("staff") or {}
        self.staff_shared = (
            staff_row.get("ownership") in ("shared", "referenced") or staff_row.get("shared")
        )
        self.keep_staff = keep_staff or bool(self.staff_shared)
        self.protected = Protected(lab, manifest)
        self.names = resource_names(student, self.staff)
        self.evidence: dict[str, Any] = {
            "student": student, "staff": self.staff, "keep_staff": self.keep_staff,
            "mode": "apply" if apply else "dry-run", "steps": [],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.failures: list[str] = []
        self.skipped: list[str] = []

    # ----- helpers --------------------------------------------------------------------
    def note(self, step, action, **detail):
        self.evidence["steps"].append({"step": step, "action": action, **detail})
        tag = "WOULD " if not self.apply and action.startswith("delete") else ""
        what = " ".join(f"{k}={v}" for k, v in detail.items() if k != "response")
        self.log(f"  [{step}] {tag}{action} {what}".rstrip())

    def protected_skip(self, step, reason, **detail):
        self.skipped.append(f"{step}: {reason} {detail}")
        self.evidence["steps"].append({"step": step, "action": "PROTECTED", "reason": reason,
                                       **detail})
        what = " ".join(f"{k}={v}" for k, v in detail.items())
        self.log(f"  [{step}] PROTECTED ({reason}) {what}")

    def fail(self, step, message, **detail):
        self.failures.append(f"{step}: {message}")
        self.evidence["steps"].append({"step": step, "action": "FAILED", "message": message,
                                       **detail})
        self.log(f"  [{step}] FAILED {message} {detail if detail else ''}".rstrip())

    def get(self, path, key=None):
        status, out = request(self.lab, self.c, "GET", path)
        if status == 404:
            return None
        if status != 200:
            raise RuntimeError(f"GET {path}: HTTP {status}: {str(out)[:300]}")
        return out if key is None else out.get(key)

    def delete(self, step, path, label, **detail):
        """DELETE with the idempotency rule: 404 = already gone, success = deleted,
        anything else = a failure of this student (its manifest row stays)."""
        if not self.apply:
            self.note(step, "delete", target=label, call=f"DELETE {path}", **detail)
            return True
        status, out = request(self.lab, self.c, "DELETE", path)
        if status == 404:
            self.note(step, "already gone", target=label, call=f"DELETE {path}")
            return True
        if 200 <= status < 300:
            self.note(step, "deleted", target=label, call=f"DELETE {path}", response=out)
            return True
        self.fail(step, f"DELETE {path} → HTTP {status} {out.get('code', '')}", target=label,
                  detail=str(out)[:400])
        return False

    # ----- discovery ------------------------------------------------------------------
    def discover(self):
        self.live_agents = self.target_agents()
        self.agent_ids = [a["id"] for a in self.live_agents]
        self.datasets = self.target_datasets()
        self.runs = self.target_runs()

    def target_agents(self) -> list[dict]:
        live = self.get("/api/agents", "agents") or []
        creators = {self.student} if self.keep_staff else {self.student, self.staff}
        wanted_ids = set()
        for kind in ("console", "eval"):
            rid = ((self.row.get("agents") or {}).get(kind) or {}).get("id")
            if rid:
                wanted_ids.add(rid)
        employee = (self.m.get("agents") or {}).get(self.student) or {}
        if employee.get("id"):
            wanted_ids.add(employee["id"])
        patterns = set(agent_name_patterns(self.student))
        out = []
        for item in live:
            if item.get("status") == "deleted":
                continue
            hit = (item.get("id") in wanted_ids or item.get("name") in patterns
                   or (item.get("created_by_name") in creators))
            if not hit:
                continue
            reason = self.protected.agent(item)
            if reason:
                self.protected_skip("agents", reason, id=item.get("id"), name=item.get("name"))
                continue
            out.append(item)
        seen = {a["id"] for a in out}
        # manifest ids the live list no longer shows are already gone (idempotency)
        for rid in sorted(wanted_ids - seen):
            if rid in self.protected.agent_ids:
                continue
            self.note("agents", "already gone", id=rid)
        return out

    def target_datasets(self) -> list[dict]:
        recorded = {d.get("id"): name for name, d in (self.row.get("datasets") or {}).items()}
        names = {self.names["exam_dataset"], self.names["held_out_dataset"]}
        live = self.get("/api/eval/datasets", "datasets") or []
        out = []
        for d in live:
            if not (d.get("id") in recorded or d.get("name") in names
                    or d.get("created_by_name") == self.student):
                continue
            reason = self.protected.dataset(d.get("id"), d.get("name"))
            if reason:
                self.protected_skip("datasets", reason, id=d.get("id"), name=d.get("name"))
                continue
            out.append(d)
        seen = {d["id"] for d in out}
        for ds_id in sorted(set(recorded) - seen - {None}):
            self.note("datasets", "already gone", id=ds_id, name=recorded[ds_id])
        return out

    def target_runs(self) -> list[dict]:
        runs: dict[str, dict] = {}
        for agent_id in self.agent_ids:
            for run in paged(self.lab, self.c, "/api/eval/runs", "runs",
                             params=f"&agent_id={agent_id}"):
                runs[run["id"]] = run
        for d in self.datasets:
            for run in paged(self.lab, self.c, "/api/eval/runs", "runs",
                             params=f"&dataset_id={d['id']}"):
                runs[run["id"]] = run
        return list(runs.values())

    # ----- the steps -------------------------------------------------------------------
    def stop_active_runs(self):
        for run in self.runs:
            if run.get("status") not in ACTIVE_RUN_STATUSES:
                continue
            path = f"/api/eval/runs/{run['id']}/stop"
            if not self.apply:
                self.note("runs", "stop", id=run["id"], status=run.get("status"),
                          call=f"POST {path}")
                continue
            status, out = request(self.lab, self.c, "POST", path, {})
            if status in (202, 200, 404) or out.get("code") == "run.not_active":
                self.note("runs", "stop requested", id=run["id"], http=status)
            else:
                self.fail("runs", f"POST {path} → HTTP {status} {out.get('code', '')}")

    def delete_agents(self):
        for item in self.live_agents:
            aid, name = item["id"], item.get("name")
            path = f"/api/agents/{aid}"
            if not self.apply:
                self.note("agents", "delete", id=aid, name=name,
                          created_by=item.get("created_by_name"), call=f"DELETE {path}")
                continue
            if self.delete_agent_with_retry(aid, name):
                self.wait_gone(aid, name)

    def delete_agent_with_retry(self, aid, name) -> bool:
        path = f"/api/agents/{aid}"
        for attempt in range(CONFLICT_RETRY_ROUNDS):
            status, out = request(self.lab, self.c, "DELETE", path)
            if status == 404:
                self.note("agents", "already gone", id=aid, name=name)
                return True
            if 200 <= status < 300:
                self.note("agents", "deleted", id=aid, name=name,
                          removed=out.get("removed"), failed=out.get("failed"), response=out)
                return True
            if status == 409 and out.get("code") in RETRY_CODES:
                if attempt + 1 < CONFLICT_RETRY_ROUNDS:
                    self.log(f"  [agents] {name}: {out.get('code')}; retrying in "
                             f"{CONFLICT_RETRY_SECONDS}s")
                    time.sleep(CONFLICT_RETRY_SECONDS)
                    continue
            self.fail("agents", f"DELETE {path} → HTTP {status} {out.get('code', '')}",
                      id=aid, name=name, detail=str(out.get("detail"))[:400])
            return False
        return False

    def wait_gone(self, aid, name):
        for _ in range(GONE_POLL_ROUNDS):
            status, _out = request(self.lab, self.c, "GET", f"/api/agents/{aid}")
            if status == 404:
                self.note("agents", "gone", id=aid, name=name)
                return
            time.sleep(GONE_POLL_SECONDS)
        self.fail("agents", f"{name} ({aid}) still answers after the delete", id=aid)

    def delete_runs_and_datasets(self):
        for run in self.runs:
            status = run.get("status")
            if status in DELETABLE_RUN_STATUSES:
                self.delete("runs", f"/api/eval/runs/{run['id']}", run["id"], status=status)
            elif status in ACTIVE_RUN_STATUSES:
                self.note("runs", "kept (stop requested; delete it once stopped)",
                          id=run["id"], status=status)
            else:
                self.note("runs", "kept (completed runs are evaluation history)",
                          id=run["id"], status=status)
        for d in self.datasets:
            self.delete("datasets", f"/api/eval/datasets/{d['id']}?force=true", d.get("name"),
                        id=d["id"])

    def delete_kb(self):
        kb = self.row.get("kb") or {}
        kb_id, name = kb.get("kb_id"), kb.get("name") or self.names["kb"]
        if not kb_id:
            self.note("kb", "none recorded")
            return
        reason = self.protected.kb(kb_id, name)
        if reason:
            self.protected_skip("kb", reason, kb_id=kb_id, name=name)
            return
        self.delete("kb", f"/api/knowledge-bases/{kb_id}", name, kb_id=kb_id,
                    removes="ledger row + S3 prefix kb/<kb_id>/ + index")

    def sweep_memory(self):
        """Records of the deleted Agents' actors (the Agent teardown already purged the
        store its spec pinned; this catches actors of Agents deleted earlier). Skipped
        gracefully when the workspace has no memory feature."""
        if not self.agent_ids:
            self.note("memory", "no Agents; nothing to sweep")
            return
        actors, token = [], None
        while True:
            path = "/api/memory/actors?max_results=100" + (f"&next_token={token}" if token else "")
            status, out = request(self.lab, self.c, "GET", path)
            if status in (503, 404):
                self.note("memory", "skipped", reason=out.get("code") or f"HTTP {status}")
                return
            if status != 200:
                self.fail("memory", f"GET {path} → HTTP {status} {out.get('code', '')}")
                return
            actors.extend(out.get("items") or [])
            token = out.get("next_token")
            if not token:
                break
        prefixes = tuple(f"{aid}__" for aid in self.agent_ids)
        mine = [a for a in actors if str(a.get("actor_id", "")).startswith(prefixes)]
        if not mine:
            self.note("memory", "no actors of the deleted Agents")
            return
        for actor in mine:
            self.sweep_actor(actor["actor_id"])

    def sweep_actor(self, actor_id):
        token = None
        while True:
            path = (f"/api/memory/records?actor_id={actor_id}&max_results=100"
                    + (f"&next_token={token}" if token else ""))
            status, out = request(self.lab, self.c, "GET", path)
            if status in (400, 404, 503):
                self.note("memory", "skipped", actor_id=actor_id,
                          reason=out.get("code") or f"HTTP {status}")
                return
            if status != 200:
                self.fail("memory", f"GET {path} → HTTP {status} {out.get('code', '')}",
                          actor_id=actor_id)
                return
            for record in out.get("items") or []:
                rid = record.get("record_id") or record.get("id")
                if rid:
                    self.delete("memory", f"/api/memory/records/{rid}?actor_id={actor_id}",
                                rid, actor_id=actor_id)
            token = out.get("next_token")
            if not token:
                return

    def delete_users(self):
        users = {u["username"]: u for u in paged(self.lab, self.c,
                                                 "/api/users?status=all", "items")}
        targets = [self.student]
        if self.keep_staff:
            self.note("users", "kept", username=self.staff,
                      reason="--keep-staff" if not self.staff_shared else "manifest: shared")
        else:
            targets.append(self.staff)
        for username in targets:
            reason = self.protected.user(username)
            if reason:
                self.protected_skip("users", reason, username=username)
                continue
            user = users.get(username)
            recorded = ((self.m.get("users") or {}).get(username) or {}).get("id")
            if username == self.staff:
                recorded = (self.row.get("staff") or {}).get("id") or recorded
            uid = (user or {}).get("id") or recorded
            if not uid:
                self.note("users", "already gone", username=username)
                continue
            self.delete("users", f"/api/users/{uid}", username, id=uid)

    # ----- ledger ---------------------------------------------------------------------
    def deleted_staff(self) -> bool:
        return not self.keep_staff and not self.protected.user(self.staff)

    def finish_ledger(self):
        if not self.apply:
            self.note("manifest", "remove row", keys=self.manifest_keys())
            self.note("card", "delete", path=f"cards/{self.student}.md")
            return
        if self.failures:
            self.note("manifest", "kept (failures above; re-run after fixing)",
                      keys=self.manifest_keys())
            return
        remove_manifest_rows(self.lab, self.student, self.staff if self.deleted_staff() else None)
        self.note("manifest", "row removed", keys=self.manifest_keys())
        card = self.lab.ROOT / (self.row.get("card_path") or f"cards/{self.student}.md")
        if card.is_file():
            card.unlink()
            self.note("card", "deleted", path=str(card))
        else:
            self.note("card", "already gone", path=str(card))

    def manifest_keys(self) -> list[str]:
        keys = [f"students.{self.student}", f"users.{self.student}", f"agents.{self.student}"]
        if self.deleted_staff():
            keys.append(f"users.{self.staff}")
        return keys

    def run(self) -> bool:
        self.log(f"\n== {self.student} (staff {self.staff}"
                 f"{', kept' if self.keep_staff else ''}) ==")
        self.discover()
        self.stop_active_runs()
        self.delete_agents()
        self.delete_runs_and_datasets()
        self.delete_kb()
        self.sweep_memory()
        self.delete_users()
        self.finish_ledger()
        self.evidence["failures"] = self.failures
        self.evidence["protected_skipped"] = self.skipped
        self.evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.lab.evidence(f"cleanup-{self.student}.json", self.evidence)
        return not self.failures


def remove_manifest_rows(lab, student, staff):
    """Drop the student's rows under the manifest lock; other rows are untouched."""
    with (lab.ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = lab.load()
        (merged.get("students") or {}).pop(student, None)
        (merged.get("users") or {}).pop(student, None)
        (merged.get("agents") or {}).pop(student, None)
        if staff:
            (merged.get("users") or {}).pop(staff, None)
        lab.save(merged)


# ----- the lab config -------------------------------------------------------------------
def trim_config(path: Path, staff: list[str]) -> list[str]:
    """Remove ``staff`` from ``staff_accounts`` / ``staff_publish_targets`` of the config
    JSON (atomic: ``<file>.bak`` keeps the previous text, a temp file is renamed over the
    original). Refuses to empty the list (``read_config`` requires one account)."""
    raw = json.loads(path.read_text())
    accounts = list(raw.get("staff_accounts") or ["itdesk01"])
    removed = [s for s in accounts if s in staff]
    kept = [s for s in accounts if s not in staff]
    if not kept:
        raise Refused("--trim-config would leave staff_accounts empty; keep one account")
    if not removed:
        return []
    raw["staff_accounts"] = kept
    targets = dict(raw.get("staff_publish_targets") or {})
    for s in removed:
        targets.pop(s, None)
    if "staff_publish_targets" in raw or targets:
        raw["staff_publish_targets"] = targets
    path.with_suffix(path.suffix + ".bak").write_text(path.read_text())
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    return removed


FOLLOW_UP = (
    "uv run --project samples/itsm-cn samples/itsm-cn/scripts/refresh_service.py",
    "uv run --project samples/itsm-cn samples/itsm-cn/scripts/prepare_policy.py",
)


def select_students(manifest, args) -> tuple[list[str], list[str]]:
    recorded = list((manifest.get("students") or {}).keys())
    if args.students:
        missing = [s for s in args.students if s not in recorded]
        if missing:
            raise Refused(f"not in the manifest: {', '.join(missing)} (recorded: "
                          f"{', '.join(recorded) or 'none'})")
        return [s for s in STUDENTS if s in args.students], []
    keep = list(args.all_but)
    return [s for s in STUDENTS if s in recorded and s not in keep], keep


def describe_kept(manifest, keep) -> list[str]:
    """``--all-but`` prints what it keeps: each learner with its staff account."""
    rows = manifest.get("students") or {}
    out = []
    for s in keep:
        if s not in rows:
            out.append(f"{s} (not in manifest)")
            continue
        staff = ((rows[s] or {}).get("staff") or {}).get("username") or staff_for(s)
        out.append(f"{s} (staff {staff})")
    return out


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        import lab
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    manifest = lab.load()
    try:
        students, keep = select_students(manifest, args)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    mode = "APPLY" if args.apply else "DRY RUN (nothing is deleted; add --apply)"
    print(f"{mode} — cleanup of {', '.join(students) or 'nobody'}")
    if args.all_but:
        print("KEEP learners:", ", ".join(describe_kept(manifest, keep)))
    protected = Protected(lab, manifest)
    print("KEEP always: shared KB", SHARED_KB, "· Gateway · policy engine + policies · Lambda /"
          " service · Skills · reference Agent", ", ".join(sorted(protected.agent_names)),
          "· accounts", ", ".join(sorted(protected.users)))
    if not students:
        print("No learner to clean.")
        return 0
    admin = lab.session()
    ok, deleted_staff = True, []
    for student in students:
        job = StudentCleanup(lab, admin, manifest, student, keep_staff=args.keep_staff,
                             apply=args.apply)
        if not job.run():
            ok = False
        elif job.deleted_staff():
            deleted_staff.append(job.staff)
    configured = [s for s in deleted_staff if s in (lab.CONFIG.get("staff_accounts") or [])]
    if configured:
        if args.trim_config and args.apply:
            path = Path(os.environ["LAUNCHPAD_LAB_CONFIG"]).resolve()
            try:
                removed = trim_config(path, configured)
            except Refused as exc:
                print(f"REFUSED: {exc}", file=sys.stderr)
                return 2
            print(f"\nConfig trimmed: removed {', '.join(removed)} from staff_accounts /"
                  f" staff_publish_targets of {path} (previous text in {path.name}.bak)")
        else:
            verb = "would still list" if not args.apply else "still lists"
            print(f"\nREMINDER: the lab config {verb} {', '.join(configured)} in staff_accounts /"
                  " staff_publish_targets; remove them (or re-run with --trim-config), then:")
        print("Then run, in this order:")
        for cmd in FOLLOW_UP:
            print("  " + cmd)
    if not ok:
        print("\nSome students had failures; their manifest rows were kept. Fix and re-run.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
