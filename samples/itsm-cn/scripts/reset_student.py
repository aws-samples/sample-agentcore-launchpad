"""Post-class reset of a KEPT student pair: undo what the manual's exercises produced, keep
every pre-provisioned resource (instructor manual 「八、课后清理」 §8.2).

    python3 reset_student.py --student lab01                 # dry run (default): plan only
    python3 reset_student.py --student lab01 --apply
    python3 reset_student.py --apply                         # every student in the manifest
    python3 reset_student.py --student lab01 --apply --skip-tickets --extend-days 14
    python3 reset_student.py --student lab01 --apply --employee-agent delete

Each student has their own environment with one account pair (employee ``labNN`` + service
desk ``itdeskNN``); after class the environment is kept and only the exercises' residue is
undone so the SAME pair starts clean next time. For the student, in this order:

0. **Active evaluation runs** on the kit Agents are stopped (a redeploy refuses
   ``409 agent.in_use`` while one runs).
1. **Employee Agent** ``labNN-itdesk-cn``. ``--employee-agent keep-baseline`` (default):
   when it exists it is REDEPLOYED to the stage-1 baseline — the lab config's ``model_id``, the
   kit's stage-1 prompt (``step1`` of ``lab-assets/prompts.json``), no tool / Skill / KB /
   policy, long-term memory off — which rebuilds the Runtime so the old chat sessions and
   long-term records go with it; the script waits until it is ``active`` (≤ 15 min). When the
   manifest row marks the Agent as student-created (``agents.employee.prebuilt: false``) or
   ``--employee-agent delete`` is given, it is deleted instead (the student re-creates it in
   scenario one). An absent Agent is left absent.
2. **Evaluation Agent** ``labNN-itdesk-eval-cn`` is redeployed to the kit's baseline ``spec``
   recorded on the manifest row (model = the lab config's ``model_id``) and waited for; then
   every recommendation of its runs is read and an ACCEPTED one is reported (it cannot be
   un-accepted; the redeploy above already restored the kit prompt).
2b. **Service-desk Agent** ``labNN-itdesk-console-cn`` is redeployed to its kit ``spec`` ONLY
   when the model it runs differs from the configured one (N17-obs557: the model switch
   reaches all three Agents); otherwise it is left alone and its sessions are only reported.

The model: ``lab.CONFIG["model_id"]`` is the single source of truth (N17-obs557). When the
manifest copy differs, ``model_id: <old> -> <new> (from config)`` is printed and, with
``--apply``, the manifest field is updated before anything is redeployed; the model must be
in the console's catalog (``GET /api/capabilities``, tool-capable, provider configured) or the
run is REFUSED (exit 2) before any mutation. ``--model <id>`` deploys THIS run with another
model without touching the config or the manifest (the card keeps the configured model).
3. **Leftover Agents** named ``labNN-*-cn`` outside the kit set (``labNN-sim-cn`` …) or created
   by the pair are deleted (polled until 404).
4. **Memory records** of the actors ``<agent_id>__labNN`` of the employee and evaluation
   Agents, every strategy, are deleted one by one (skipped when the workspace has no memory
   feature).
5. **Personal knowledge base**: every object ``KB\\d{7}.md`` under the data source's S3 prefix
   (``kb/<kb_id>/<ds_id>/``) is deleted with boto3, a FULL rebuild sync is started and polled
   to completion, and the remaining document count is printed (expect the 5 base files).
6. **Tickets**: the synthetic service is reset like ``reset_lab.py --apply`` (ALL tickets of
   the environment — correct for a single-pair environment; ``--skip-tickets`` skips it).
7. **Policy**: every lab rule (including the requester rule ``itsm_requester_is_self``)
   back to ``active`` — the manual presumes an enforced gateway from 2.1 on (N17-obs548).
8. **Accounts**: both users' ``expires_at`` set to now + ``--extend-days`` (default 30).
9. **Report**: completed evaluation runs that remain (permanent history, the API refuses to
   delete them), chat sessions still listed on the console Agent (not rebuilt), the evidence
   file ``<state_dir>/evidence/reset-<student>-<ts>.json``, and the resource card
   ``cards/labNN.md`` re-rendered from the manifest (``student_kit.py --cards-only``).

Never touched: the shared knowledge base, the Gateway, the policy engine itself, the tools
service code, the Skills, the reference Agent, the organizer accounts, other students, and the
kit's console Agent (its sessions are only reported). A requested student missing from the
manifest is refused (exit 2); a step that fails is reported and the run exits 1.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import types
from datetime import UTC, datetime, timedelta
from typing import Any

from cleanup_students import (
    ACTIVE_RUN_STATUSES,
    RETRY_CODES,
    Protected,
    Refused,
    paged,
    request,
)
from student_kit_lib import (
    STUDENTS,
    ModelNotUsable,
    require_model,
    resource_names,
    staff_for,
    stage1_agent_spec,
    sync_model_id,
)

ARTICLE_RE = re.compile(r"^KB\d{7}\.md$")
REQUESTER_RULE = "itsm_requester_is_self"
# N17-obs548: since manual 1.6 the student manual presumes the requester rule is ENFORCED
# throughout (2.1 check, 4.1 precondition, 4.1.2 deny card, 5.1.2 decisions); nothing is
# left in log_only after a reset.
LOG_ONLY_RULES: tuple[str, ...] = ()
ACTIVE_POLL_SECONDS = 5
ACTIVE_POLL_ROUNDS = 180  # 15 minutes
GONE_POLL_SECONDS = 3
GONE_POLL_ROUNDS = 100
CONFLICT_RETRY_SECONDS = 10
CONFLICT_RETRY_ROUNDS = 30
SYNC_POLL_SECONDS = 10
SYNC_POLL_ROUNDS = 90
RUNNING_JOB_STATUSES = {"STARTING", "IN_PROGRESS", "RUNNING"}
BASE_DOCUMENT_COUNT = 5
S3_DELETE_BATCH = 1000


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--student", choices=STUDENTS,
                   help="the learner to reset (default: every student in the manifest)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="read the live state and print the plan; mutate nothing (default)")
    mode.add_argument("--apply", action="store_true", help="perform the reset")
    p.add_argument("--skip-tickets", action="store_true",
                   help="do not reset the synthetic tickets (they are shared by the environment)")
    p.add_argument("--extend-days", type=int, default=30, metavar="N",
                   help="both accounts expire N days from now (default 30; 1–3650)")
    p.add_argument("--employee-agent", choices=("keep-baseline", "delete"),
                   default="keep-baseline",
                   help="redeploy the employee Agent to the stage-1 baseline (default) or"
                        " delete it so the student re-creates it in scenario one")
    p.add_argument("--model", metavar="MODEL_ID",
                   help="redeploy with this model for THIS run only; the lab config, the"
                        " manifest and the card keep their model_id (default: the config's)")
    args = p.parse_args(argv)
    if args.apply:
        args.dry_run = False
    if not 1 <= args.extend_days <= 3650:
        p.error("--extend-days must be between 1 and 3650")
    return args


def article_keys(keys, prefix: str) -> list[str]:
    """The published-article objects among ``keys``: directly under ``prefix`` and named
    ``KB<7 digits>.md``. The 5 base documents and anything nested stay."""
    out = []
    for key in keys:
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if ARTICLE_RE.match(rest):
            out.append(key)
    return out


def memory_actor(agent_id: str, username: str) -> str:
    """The platform folds the Agent id into the actor: ``<agent_id>__<human>``."""
    return f"{agent_id}__{username}"


def kb_location(row_kb: dict, manifest: dict) -> tuple[str | None, str]:
    """``(bucket, prefix)`` of the personal KB's data source: the row's own data source when
    the kit recorded it, else the shared KB's bucket with the platform layout."""
    kb_id, ds_id = row_kb.get("kb_id"), row_kb.get("ds_id")
    sources = row_kb.get("data_sources") or []
    source = next((s for s in sources if s.get("ds_id") == ds_id), sources[0] if sources else {})
    bucket = source.get("bucket")
    prefix = (source.get("prefix") or "").strip("/")
    if not bucket:
        shared = ((manifest.get("kb") or {}).get("data_sources") or [{}])[0]
        bucket = shared.get("bucket")
    if not prefix:
        prefix = f"kb/{kb_id}/{ds_id}"
    return bucket, prefix + "/"


class StudentReset:
    """Discover (read-only) and, with ``--apply``, reset one kept student pair."""

    def __init__(self, lab, admin, manifest, student, *, args, log=print, model_id=None):
        self.lab, self.c, self.m, self.student = lab, admin, manifest, student
        self.args, self.apply, self.log = args, args.apply, log
        # N17-obs557: the model every redeploy below uses — the config's (already synced into
        # ``manifest`` by main()) or the one-run ``--model`` override
        self.model_id = model_id or manifest["model_id"]
        self.row = (manifest.get("students") or {})[student]
        self.staff = (self.row.get("staff") or {}).get("username") or staff_for(student)
        self.names = resource_names(student, self.staff)
        self.protected = Protected(lab, manifest)
        self.ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.evidence: dict[str, Any] = {
            "student": student, "staff": self.staff,
            "mode": "apply" if self.apply else "dry-run",
            "employee_agent": args.employee_agent, "steps": [],
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self.failures: list[str] = []
        self.skipped: list[str] = []
        self.report: dict[str, Any] = {}

    # ----- helpers --------------------------------------------------------------------
    MUTATING = ("delete", "redeploy", "stop", "sync", "reset", "set", "extend", "patch",
                "write")

    def note(self, step, action, **detail):
        self.evidence["steps"].append({"step": step, "action": action, **detail})
        tag = "WOULD " if not self.apply and action.split(" ")[0] in self.MUTATING else ""
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

    # ----- discovery ------------------------------------------------------------------
    def discover(self):
        live = [a for a in (self.get("/api/agents", "agents") or [])
                if a.get("status") != "deleted"]
        self.live_by_id = {a["id"]: a for a in live}
        self.live_by_name = {a.get("name"): a for a in live}
        kit = self.row.get("agents") or {}
        self.kit_rows = {
            "employee": kit.get("employee") or (self.m.get("agents") or {}).get(self.student)
            or {},
            "console": kit.get("console") or {},
            "eval": kit.get("eval") or {},
        }
        self.kit_agents = {kind: self.find_agent(kind) for kind in self.kit_rows}
        self.kit_ids = {a["id"] for a in self.kit_agents.values() if a}
        self.kit_names = {self.names["employee_agent"], self.names["console_agent"],
                          self.names["eval_agent"]} | {
            r.get("name") for r in self.kit_rows.values() if r.get("name")}
        self.runs = self.target_runs()

    def find_agent(self, kind) -> dict | None:
        row = self.kit_rows[kind]
        name = row.get("name") or self.names[f"{kind}_agent"]
        hit = self.live_by_id.get(row.get("id")) or self.live_by_name.get(name)
        if hit and self.protected.agent(hit):
            self.protected_skip("agents", self.protected.agent(hit), kind=kind, id=hit["id"],
                                name=hit.get("name"))
            return None
        return hit

    def target_runs(self) -> list[dict]:
        runs: dict[str, dict] = {}
        for aid in sorted(self.kit_ids):
            for run in paged(self.lab, self.c, "/api/eval/runs", "runs", params=f"&agent_id={aid}"):
                runs[run["id"]] = run
        return list(runs.values())

    # ----- Agent primitives ----------------------------------------------------------
    def redeploy(self, step, agent, spec, label) -> bool:
        aid, path = agent["id"], f"/api/agents/{agent['id']}/redeploy"
        if not self.apply:
            self.note(step, "redeploy", target=label, id=aid, call=f"POST {path}",
                      model=spec.get("model_id"),
                      memory=json.dumps(spec.get("memory"), sort_keys=True),
                      tools=len(spec.get("tools") or []), skills=len(spec.get("skills") or []),
                      kbs=len(spec.get("knowledge_bases") or []))
            return True
        for attempt in range(CONFLICT_RETRY_ROUNDS):
            status, out = request(self.lab, self.c, "POST", path, spec)
            if status in (200, 202):
                self.note(step, "redeploy submitted", target=label, id=aid,
                          job_id=out.get("job_id"), response=out)
                return self.wait_active(step, aid, label)
            if status == 409 and out.get("code") in RETRY_CODES:
                if attempt + 1 < CONFLICT_RETRY_ROUNDS:
                    self.log(f"  [{step}] {label}: {out.get('code')}; retrying in "
                             f"{CONFLICT_RETRY_SECONDS}s")
                    time.sleep(CONFLICT_RETRY_SECONDS)
                    continue
            self.fail(step, f"POST {path} → HTTP {status} {out.get('code', '')}", target=label,
                      detail=str(out)[:400])
            return False
        return False

    def wait_active(self, step, aid, label) -> bool:
        start = time.monotonic()
        for _ in range(ACTIVE_POLL_ROUNDS):
            detail = self.get(f"/api/agents/{aid}")
            item = (detail or {}).get("agent", detail) or {}
            status = item.get("status")
            if status in ("active", "running"):
                self.note(step, "active", target=label, id=aid, version=item.get("version"),
                          seconds=round(time.monotonic() - start))
                self.report.setdefault("versions", {})[label] = item.get("version")
                return True
            if status in ("failed", "error") or detail is None:
                self.fail(step, f"{label} ended in status {status}", id=aid,
                          detail=str(item)[:400])
                return False
            time.sleep(ACTIVE_POLL_SECONDS)
        budget = ACTIVE_POLL_ROUNDS * ACTIVE_POLL_SECONDS
        self.fail(step, f"{label} ({aid}) not active after {budget}s", id=aid)
        return False

    def delete_agent(self, step, agent, label) -> bool:
        aid, path = agent["id"], f"/api/agents/{agent['id']}"
        if not self.apply:
            self.note(step, "delete", target=label, id=aid,
                      created_by=agent.get("created_by_name"), call=f"DELETE {path}")
            return True
        for attempt in range(CONFLICT_RETRY_ROUNDS):
            status, out = request(self.lab, self.c, "DELETE", path)
            if status == 404:
                self.note(step, "already gone", target=label, id=aid)
                return True
            if 200 <= status < 300:
                self.note(step, "deleted", target=label, id=aid, removed=out.get("removed"),
                          response=out)
                return self.wait_gone(step, aid, label)
            if status == 409 and out.get("code") in RETRY_CODES:
                if attempt + 1 < CONFLICT_RETRY_ROUNDS:
                    self.log(f"  [{step}] {label}: {out.get('code')}; retrying in "
                             f"{CONFLICT_RETRY_SECONDS}s")
                    time.sleep(CONFLICT_RETRY_SECONDS)
                    continue
            self.fail(step, f"DELETE {path} → HTTP {status} {out.get('code', '')}", target=label,
                      detail=str(out)[:400])
            return False
        return False

    def wait_gone(self, step, aid, label) -> bool:
        for _ in range(GONE_POLL_ROUNDS):
            status, _out = request(self.lab, self.c, "GET", f"/api/agents/{aid}")
            if status == 404:
                self.note(step, "gone", target=label, id=aid)
                return True
            time.sleep(GONE_POLL_SECONDS)
        self.fail(step, f"{label} ({aid}) still answers after the delete", id=aid)
        return False

    # ----- 0. active runs -------------------------------------------------------------
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
            if status in (200, 202, 404) or out.get("code") == "run.not_active":
                self.note("runs", "stop requested", id=run["id"], http=status)
            else:
                self.fail("runs", f"POST {path} → HTTP {status} {out.get('code', '')}")

    # ----- 1. employee Agent ----------------------------------------------------------
    def reset_employee_agent(self):
        label = self.names["employee_agent"]
        agent = self.kit_agents["employee"]
        prebuilt = self.kit_rows["employee"].get("prebuilt")
        delete = self.args.employee_agent == "delete" or prebuilt is False
        if agent is None:
            self.note("employee", "absent (the student creates it in scenario one)", name=label)
            return
        if delete:
            reason = ("manifest: prebuilt false" if prebuilt is False
                      else "--employee-agent delete")
            self.note("employee", "student-created; delete", name=label, reason=reason)
            self.delete_agent("employee", agent, label)
            return
        prompts = json.loads((self.lab.ROOT / "lab-assets/prompts.json").read_text())
        spec = stage1_agent_spec(agent.get("name") or label, model_id=self.model_id,
                                 prompts=prompts)
        self.note("employee", "baseline = stage 1", name=label, id=agent["id"],
                  prompt_chars=len(spec["system_prompt"]))
        self.redeploy("employee", agent, spec, label)

    # ----- 2. evaluation Agent --------------------------------------------------------
    def reset_eval_agent(self):
        label = self.names["eval_agent"]
        agent = self.kit_agents["eval"]
        if agent is None:
            self.note("eval", "missing; run student_kit.py to rebuild it", name=label)
            return
        spec = self.kit_rows["eval"].get("spec")
        if not spec:
            self.fail("eval", "manifest row has no kit spec; run student_kit.py", name=label)
            return
        spec = {**spec, "model_id": self.model_id, "name": agent.get("name") or label}
        if self.redeploy("eval", agent, spec, label):
            self.record_model("eval")
            self.check_recommendations(agent)

    def record_model(self, kind):
        """After a redeploy, the kit row's baseline ``spec`` carries the model it now runs,
        so the next reset compares against the truth (finish() saves the row)."""
        spec = self.kit_rows[kind].get("spec")
        if self.apply and isinstance(spec, dict):
            spec["model_id"] = self.model_id

    # ----- 2b. service-desk Agent (model switch only) ----------------------------------
    def reset_console_agent(self):
        """N17-obs557: the kit's console Agent is normally left alone (its chat sessions are
        the service desk's history). When the model it runs — the live spec's, else the kit
        row's — differs from the configured one, it is redeployed to its kit ``spec`` with the
        new model so the switch reaches all three Agents."""
        label = self.names["console_agent"]
        agent = self.kit_agents["console"]
        if agent is None:
            self.note("console", "missing; run student_kit.py to rebuild it", name=label)
            return
        spec = self.kit_rows["console"].get("spec")
        if not spec:
            self.note("console", "no kit spec recorded; model not compared (run student_kit.py"
                      " to record one)", name=label)
            return
        running = (agent.get("spec") or {}).get("model_id") or spec.get("model_id")
        if running == self.model_id:
            self.note("console", "unchanged (model matches; sessions kept)", name=label,
                      model=self.model_id)
            return
        self.note("console", "model changed; redeploy to the kit spec", name=label,
                  model=f"{running}→{self.model_id}")
        spec = {**spec, "model_id": self.model_id, "name": agent.get("name") or label}
        if self.redeploy("console", agent, spec, label):
            self.record_model("console")

    def check_recommendations(self, agent):
        accepted = []
        for run in self.runs:
            if run.get("agent_id") not in (None, agent["id"]):
                continue
            path = f"/api/eval/runs/{run['id']}/recommendations"
            status, out = request(self.lab, self.c, "GET", path)
            if status != 200:
                continue
            for rec in out.get("recommendations") or []:
                if rec.get("accepted"):
                    accepted.append({"run_id": run["id"], "run_name": run.get("name"),
                                     "recommendation_id": rec.get("id"),
                                     "status": rec.get("status")})
        self.report["accepted_recommendations"] = accepted
        if accepted:
            self.note("eval", "accepted recommendation(s) remain on old runs (cannot be"
                      " un-accepted; the redeploy restored the kit prompt — tell the class)",
                      count=len(accepted), runs=",".join(sorted({a["run_id"] for a in accepted})))
        else:
            self.note("eval", "no accepted recommendation pending")

    # ----- 3. leftovers ---------------------------------------------------------------
    def delete_leftovers(self):
        pattern = re.compile(rf"^{re.escape(self.student)}-.+-cn$")
        creators = {self.student, self.staff}
        found = False
        for aid, item in sorted(self.live_by_id.items()):
            if aid in self.kit_ids or item.get("name") in self.kit_names:
                continue
            name = item.get("name") or ""
            if not (pattern.match(name) or item.get("created_by_name") in creators):
                continue
            reason = self.protected.agent(item)
            if reason:
                self.protected_skip("leftovers", reason, id=aid, name=name)
                continue
            found = True
            self.delete_agent("leftovers", item, name)
        if not found:
            self.note("leftovers", "none")

    # ----- 4. memory ------------------------------------------------------------------
    def sweep_memory(self):
        agents = [(kind, self.kit_agents[kind]) for kind in ("employee", "eval")]
        actors = [memory_actor(a["id"], self.student) for kind, a in agents
                  if a and not (kind == "employee" and self.employee_deleted())]
        if not actors:
            self.note("memory", "no kit Agent left to sweep")
            return
        for actor in actors:
            self.sweep_actor(actor)

    def employee_deleted(self) -> bool:
        prebuilt = self.kit_rows["employee"].get("prebuilt")
        return self.args.employee_agent == "delete" or prebuilt is False

    def sweep_actor(self, actor_id):
        path = f"/api/memory/namespaces?actor_id={actor_id}"
        status, out = request(self.lab, self.c, "GET", path)
        if status in (400, 404, 503):
            self.note("memory", "skipped", actor_id=actor_id,
                      reason=out.get("code") or f"HTTP {status}")
            return
        if status != 200:
            self.fail("memory", f"GET namespaces → HTTP {status} {out.get('code', '')}",
                      actor_id=actor_id)
            return
        namespaces = [n for n in out.get("items") or [] if n.get("resolvable", True)]
        if not namespaces:
            self.note("memory", "no namespace", actor_id=actor_id)
            return
        deleted = 0
        for ns in namespaces:
            token = None
            while True:
                path = (f"/api/memory/records?actor_id={actor_id}&namespace={ns['namespace']}"
                        f"&max_results=100" + (f"&next_token={token}" if token else ""))
                status, out = request(self.lab, self.c, "GET", path)
                if status in (400, 404, 503):
                    self.note("memory", "skipped", actor_id=actor_id,
                              strategy=ns.get("strategy_id"),
                              reason=out.get("code") or f"HTTP {status}")
                    break
                if status != 200:
                    self.fail("memory", f"GET {path} → HTTP {status} {out.get('code', '')}",
                              actor_id=actor_id)
                    break
                for record in out.get("items") or []:
                    rid = record.get("record_id") or record.get("id")
                    if rid and self.delete_record(actor_id, rid, ns.get("strategy_id")):
                        deleted += 1
                token = out.get("next_token")
                if not token:
                    break
        self.note("memory", "records " + ("deleted" if self.apply else "to delete"),
                  actor_id=actor_id, count=deleted)

    def delete_record(self, actor_id, rid, strategy) -> bool:
        path = f"/api/memory/records/{rid}?actor_id={actor_id}"
        if not self.apply:
            self.note("memory", "delete", record=rid, actor_id=actor_id, strategy=strategy,
                      call=f"DELETE {path}")
            return True
        status, out = request(self.lab, self.c, "DELETE", path)
        if status == 404 or 200 <= status < 300:
            return True
        self.fail("memory", f"DELETE {path} → HTTP {status} {out.get('code', '')}", record=rid)
        return False

    # ----- 5. personal KB -------------------------------------------------------------
    def reset_kb(self):
        kb = self.row.get("kb") or {}
        kb_id, ds_id = kb.get("kb_id"), kb.get("ds_id")
        name = kb.get("name") or self.names["kb"]
        if not kb_id or not ds_id:
            self.note("kb", "none recorded; run student_kit.py", name=name)
            return
        reason = self.protected.kb(kb_id, name)
        if reason:
            self.protected_skip("kb", reason, kb_id=kb_id, name=name)
            return
        if self.get(f"/api/knowledge-bases/{kb_id}") is None:
            self.note("kb", "already gone", kb_id=kb_id, name=name)
            return
        before = self.list_documents(kb_id, ds_id)
        bucket, prefix = kb_location(kb, self.m)
        if not bucket:
            self.fail("kb", "no S3 bucket recorded for the data source", kb_id=kb_id)
            return
        s3 = self.lab.AWS.client("s3")
        keys = []
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents") or [])
        articles = article_keys(keys, prefix)
        self.note("kb", "documents listed", kb_id=kb_id, name=name, indexed=len(before),
                  objects=len(keys), articles=len(articles), prefix=f"s3://{bucket}/{prefix}")
        self.report["kb"] = {"kb_id": kb_id, "ds_id": ds_id, "documents_before": len(before),
                             "articles": articles}
        if not articles:
            self.note("kb", "no published article to remove")
            self.report["kb"]["documents_after"] = len(before)
            return
        for key in articles:
            self.note("kb", "delete", object=f"s3://{bucket}/{key}")
        if not self.apply:
            self.note("kb", "sync", kb_id=kb_id, ds_id=ds_id, full=True,
                      call=f"POST /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync")
            return
        for i in range(0, len(articles), S3_DELETE_BATCH):
            batch = articles[i:i + S3_DELETE_BATCH]
            result = s3.delete_objects(Bucket=bucket, Delete={
                "Objects": [{"Key": k} for k in batch], "Quiet": True})
            errors = result.get("Errors") or []
            if errors:
                self.fail("kb", f"{len(errors)} object(s) not deleted", detail=str(errors)[:400])
                return
        self.note("kb", "articles deleted", count=len(articles))
        path = f"/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"
        status, out = request(self.lab, self.c, "POST", path, {"full": True})
        if status not in (200, 202):
            self.fail("kb", f"POST {path} → HTTP {status} {out.get('code', '')}",
                      detail=str(out)[:400])
            return
        self.note("kb", "full rebuild started", job_id=out.get("job_id"), response=out)
        if self.wait_sync(kb_id, ds_id, out.get("job_id")):
            after = self.list_documents(kb_id, ds_id)
            self.report["kb"]["documents_after"] = len(after)
            verdict = "as expected" if len(after) == BASE_DOCUMENT_COUNT else "UNEXPECTED"
            self.note("kb", f"remaining documents {verdict}", count=len(after),
                      expected=BASE_DOCUMENT_COUNT,
                      names=",".join(sorted(str(d.get("name")) for d in after)))

    def list_documents(self, kb_id, ds_id) -> list[dict]:
        docs, token = [], None
        while True:
            path = (f"/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents?page_size=100"
                    + (f"&token={token}" if token else ""))
            out = self.get(path) or {}
            docs.extend(out.get("documents") or [])
            token = out.get("next_token")
            if not token:
                return docs

    def wait_sync(self, kb_id, ds_id, started) -> bool:
        path = f"/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"
        for _ in range(SYNC_POLL_ROUNDS):
            jobs = self.get(path, "items") or []
            if jobs:
                latest = next((j for j in jobs if j.get("job_id") == started), None) or max(
                    jobs, key=lambda j: str(j.get("created_at") or ""))
                status = str(latest.get("status") or "").upper()
                if status and status not in RUNNING_JOB_STATUSES:
                    if status in ("FAILED", "STOPPED"):
                        self.fail("kb", f"full rebuild {status}", job=latest)
                        return False
                    self.note("kb", "full rebuild finished", status=status,
                              job_id=latest.get("job_id"))
                    return True
            time.sleep(SYNC_POLL_SECONDS)
        self.fail("kb", "full rebuild did not finish; inspect the ingestion jobs")
        return False

    # ----- 6. tickets -----------------------------------------------------------------
    def reset_tickets(self):
        if self.args.skip_tickets:
            self.note("tickets", "skipped (--skip-tickets)")
            return
        import reset_lab

        try:
            plan = reset_lab.ticket_reset_plan(self.m)
        except (SystemExit, KeyError) as exc:
            self.fail("tickets", f"reset_lab refused: {exc}")
            return
        if not self.apply:
            self.note("tickets", "reset ALL tickets of the environment", unit=plan["unit"],
                      preserve=",".join(plan["preserve"]))
            return
        try:
            result = reset_lab.reset_tickets(self.m)
        except Exception as exc:  # one failed step; the remaining steps still run
            self.fail("tickets", f"service reset failed: {exc}")
            return
        self.note("tickets", "reset done", unit=plan["unit"], ssm_status=result.get("Status"))

    # ----- 7. policy ------------------------------------------------------------------
    def reset_policy(self):
        engine = (self.m.get("engine") or {}).get("id")
        policies = self.m.get("policies") or {}
        if not engine or not policies:
            self.note("policy", "no lab engine / policies in the manifest; run prepare_policy.py")
            return
        for name, row in sorted(policies.items()):
            pid = row.get("id")
            if not pid:
                continue
            want = "log_only" if name in LOG_ONLY_RULES else "active"
            current = self.get(f"/api/policy-engines/{engine}/policies/{pid}")
            if current is None:
                self.note("policy", "gone; run prepare_policy.py", name=name, id=pid)
                continue
            have = current.get("enforcement_mode")
            if have == want:
                self.note("policy", "unchanged", name=name, mode=have)
                continue
            path = f"/api/policy-engines/{engine}/policies/{pid}/enforcement-mode"
            if not self.apply:
                self.note("policy", "set", name=name, mode=f"{have}→{want}", call=f"PUT {path}")
                continue
            status, out = request(self.lab, self.c, "PUT", path, {"enforcement_mode": want})
            if status == 200:
                self.note("policy", "mode set", name=name, mode=f"{have}→{want}", response=out)
            else:
                self.fail("policy", f"PUT {path} → HTTP {status} {out.get('code', '')}", name=name)

    # ----- 8. accounts ----------------------------------------------------------------
    def extend_accounts(self):
        users = {u["username"]: u for u in paged(self.lab, self.c, "/api/users?status=all",
                                                 "items")}
        until = (datetime.now(UTC) + timedelta(days=self.args.extend_days)).replace(microsecond=0)
        body = {"expires_at": until.isoformat().replace("+00:00", "Z")}
        self.report["expires_at"] = {}
        for username in (self.student, self.staff):
            reason = self.protected.user(username)
            if reason:
                self.protected_skip("accounts", reason, username=username)
                continue
            user = users.get(username)
            if not user:
                self.fail("accounts", "account missing; run prepare_console.py / the kit",
                          username=username)
                continue
            path = f"/api/users/{user['id']}"
            if not self.apply:
                self.note("accounts", "extend", username=username, expires_at=body["expires_at"],
                          current=user.get("expires_at"), call=f"PATCH {path}")
                self.report["expires_at"][username] = body["expires_at"]
                continue
            status, out = request(self.lab, self.c, "PATCH", path, body)
            if status == 200:
                new = out.get("expires_at") or body["expires_at"]
                self.report["expires_at"][username] = new
                self.note("accounts", "extended", username=username, expires_at=new)
            else:
                self.fail("accounts", f"PATCH {path} → HTTP {status} {out.get('code', '')}",
                          username=username)

    # ----- 9. report ------------------------------------------------------------------
    def finish(self):
        completed = [{"id": r["id"], "name": r.get("name"), "agent_id": r.get("agent_id")}
                     for r in self.runs if r.get("status") == "completed"]
        self.report["completed_runs"] = completed
        if completed:
            self.note("report", "completed evaluation runs remain (permanent history; the API"
                      " refuses to delete them — name them to the class)",
                      count=len(completed),
                      ids=",".join(f"{r['id']}({r['name']})" for r in completed))
        else:
            self.note("report", "no completed evaluation run")
        console = self.kit_agents["console"]
        if console:
            sessions = self.get(f"/api/chat/{console['id']}/sessions", "sessions")
            count = len(sessions or [])
            self.report["console_sessions"] = count
            self.note("report", "chat sessions on the console Agent (not rebuilt unless the"
                      " model changed; no delete route)", name=console.get("name"), count=count)
        self.evidence["failures"] = self.failures
        self.evidence["protected_skipped"] = self.skipped
        self.evidence["report"] = self.report
        self.evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        evidence_name = f"reset-{self.student}-{self.ts}.json"
        self.lab.evidence(evidence_name, self.evidence)
        self.note("report", "evidence written", path=f"evidence/{evidence_name}")
        if not self.apply:
            self.note("card", "write", path=self.row.get("card_path") or f"cards/{self.student}.md",
                      how="student_kit.py --cards-only")
            return
        import student_kit

        self.row["last_reset"] = {"at": self.evidence["finished_at"],
                                  "evidence": f"evidence/{evidence_name}"}
        student_kit.save_student_row(self.lab, self.student, self.row)
        student_kit.cards_only(types.SimpleNamespace(student=self.student))
        self.note("card", "written", path=self.row.get("card_path") or f"cards/{self.student}.md")

    def run(self) -> bool:
        self.log(f"\n== {self.student} (staff {self.staff}) ==")
        self.discover()
        self.stop_active_runs()
        self.reset_employee_agent()
        self.reset_eval_agent()
        self.reset_console_agent()
        self.delete_leftovers()
        self.sweep_memory()
        self.reset_kb()
        self.reset_tickets()
        self.reset_policy()
        self.extend_accounts()
        self.finish()
        return not self.failures


def select_students(manifest, student) -> list[str]:
    recorded = list((manifest.get("students") or {}).keys())
    if student:
        if student not in recorded:
            raise Refused(f"not in the manifest: {student} (recorded: "
                          f"{', '.join(recorded) or 'none'})")
        return [student]
    return [s for s in STUDENTS if s in recorded]


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        import lab
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    lab.sync_assets()  # N17-obs558: the stage-1 prompt follows the package
    manifest = lab.load()
    try:
        students = select_students(manifest, args.student)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    mode = "APPLY" if args.apply else "DRY RUN (nothing is changed; add --apply)"
    print(f"{mode} — post-class reset of {', '.join(students) or 'nobody'}")
    # N17-obs557: the config's model_id is the truth, unless this run deploys a one-off --model
    model_id = args.model or str(lab.CONFIG["model_id"])
    protected = Protected(lab, manifest)
    print("KEEP always: shared KB · Gateway · policy engine · Lambda / service · Skills ·"
          " reference Agent", ", ".join(sorted(protected.agent_names)),
          "· accounts", ", ".join(sorted(protected.users)))
    if not args.skip_tickets:
        print("NOTE: the ticket reset covers ALL synthetic tickets of this environment"
              " (one pair per environment); add --skip-tickets to leave them.")
    if not students:
        print("No student recorded in the manifest.")
        return 0
    admin = lab.session()
    # before any redeploy: a model the console does not offer is refused (exit 2)
    status, catalog = request(lab, admin, "GET", "/api/capabilities")
    if status != 200:
        print(f"ERROR: GET /api/capabilities: HTTP {status}: {str(catalog)[:300]}",
              file=sys.stderr)
        return 1
    try:
        require_model(catalog, model_id)
    except ModelNotUsable as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    if args.model:
        print(f"model_id: {args.model} for THIS run only (config / manifest / card keep"
              f" {lab.CONFIG['model_id']})")
    else:  # the manifest copy follows the config (written only with --apply)
        sync_model_id(lab, manifest, dry_run=not args.apply)
    ok = True
    for student in students:
        if not StudentReset(lab, admin, manifest, student, args=args, model_id=model_id).run():
            ok = False
    if not ok:
        print("\nSome steps failed; see the evidence files, fix and re-run.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
