"""Per-student resource kit for workshop manual 1.3 (one learner, two logins, three Agents).

    python3 student_kit.py lab03 [--dry-run] [--staff itdesk03]
    python3 student_kit.py --cards-only            # re-write every card from the manifest

For ONE student it provisions, idempotently and recorded under ``students[labNN]`` of
the manifest with ownership:

* the service-desk account (SSM SecureString ``<PREFIX>users/<staff>``; never printed),
* the personal knowledge base with the 5 shared documents uploaded and synced,
* the personal read-only datasets (``adapt_dataset`` in ``student_kit_lib``),
* the personal evaluation Agent (created as the student; get_ticket + search_tickets only),
* the personal service-desk Agent (created as the staff account),
* a caller-map refresh so the Gateway recognises the two new Agents,

and prints the resource card of the organizer checklist (§二), also written to
``<state_dir>/cards/<student>.md`` and recorded as ``card_path`` on the student's manifest
row. Resources that exist but are not recorded in the manifest are never overwritten
(prepare_console.py's rule). ``--dry-run`` prints the complete plan and the card without
any network call. ``--cards-only`` re-renders and writes the cards of every student the
manifest records from the manifest alone (no login, no API call), so the organizer can
regenerate the hand-outs at any time; with a student name it rewrites that card only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import secrets
import sys
import time

from student_kit_lib import (
    EVAL_READ_TOOLS,
    GATEWAY_TARGETS,
    SHARED_DATASETS,
    STAFF_ONLY_SKILLS,
    STUDENTS,
    adapt_dataset_document,
    build_agent_spec,
    certificate_note,
    check_model,
    employee_agent_prebuilt,
    employee_agent_row,
    gateway_tool_names,
    other_ticket,
    plan_steps,
    render_card,
    resource_names,
    staff_for,
    sync_model_id,
)

# Mirrors prepare_console.py's itdesk01 grant, minus policy.manage and eval.run: the
# manual's service desk reviews, publishes and syncs (member routes) but neither edits
# the shared Cedar rules nor runs the student's evaluations (manual §一, 5.2).
STAFF_PERMISSIONS = {
    "agents.deploy": True,
    "agents.delete": True,
    "agents.import": False,
    "agents.convert": False,
    "eval.run": False,
    "identity.manage": False,
    "identity.grant": False,
    "memory.manage": True,
    "policy.manage": False,
}
AGENT_POLL_SECONDS = 5
AGENT_POLL_ROUNDS = 160
KB_POLL_SECONDS = 3
KB_POLL_ROUNDS = 60
SYNC_POLL_SECONDS = 10
SYNC_POLL_ROUNDS = 90
RUNNING_JOB_STATUSES = {"STARTING", "IN_PROGRESS", "RUNNING"}


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("student", nargs="?", choices=STUDENTS,
                   help="the learner (required unless --cards-only)")
    p.add_argument("--staff", help="service-desk username (default itdesk + the two digits)")
    p.add_argument("--dry-run", action="store_true", help="print the plan; call nothing")
    p.add_argument(
        "--skip-refresh-callers", action="store_true",
        help="do not re-publish itsm_agents_call_tools after creating the Agents",
    )
    p.add_argument(
        "--cards-only", action="store_true",
        help="re-render and write <state_dir>/cards/<student>.md for every student in the"
             " manifest (or the named one) from the manifest alone; no login, no API call",
    )
    args = p.parse_args(argv)
    if args.cards_only and args.dry_run:
        p.error("--cards-only and --dry-run are exclusive")
    if args.student is None and not args.cards_only:
        p.error("student is required unless --cards-only")
    return args


def card_values(names, *, model_id, kb_ds=None, kb_id=None, console_url="", prepared_at="",
                publish_bound=False, gateway_name="", employee_prebuilt=True):
    """``gateway_name`` is the console-visible gateway ID (name + random suffix) — what the
    策略 page shows under 已附加的网关 — so the student can match the card against the page.
    ``employee_prebuilt`` (lab config ``employee_agent_prebuilt``) picks the 员工 Agent row's
    manual-1.12 wording: 已预建，直接打开 / 未预建，场景一 3.1.1 由学员创建."""
    publish_source = "待绑定（config.staff_accounts → prepare_policy/tools）"
    if kb_ds:
        publish_source = f"{names['kb']} / data source {kb_ds}" + (
            "" if publish_bound else "（发布目标绑定由 staff_accounts 配置完成）"
        )
    return {
        **names,
        "employee_agent": employee_agent_row(names["employee_agent"], employee_prebuilt),
        "kb": names["kb"] + (f"（{kb_id}）" if kb_id else ""),
        "fixture": "共享只读工单数据（平台无独立评测快照；见 README）",
        "publish_source": publish_source,
        "other_ticket": other_ticket(names["student"]),
        "memory": f"{names['employee_agent']} + {names['student']}（kit 的两个 Agent 记忆关闭）",
        "records": "员工 / 服务台分别查看本人记录；跨账号读取取决于工作区可见性模式",
        # manual 1.9 (PRACTICE-S1 §3-16): one row per fact instead of 基线版本、批次与日期
        "model": model_id,
        "console_url": console_url,
        "prepared_at": prepared_at or "未执行",
        "gateway": gateway_name,
        "certificate": certificate_note(console_url),
    }


def write_card(root, student, card):
    """Write the rendered card to ``<root>/cards/<student>.md``; return the relative path."""
    cards = root / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / f"{student}.md").write_text(card + "\n", encoding="utf-8")
    return f"cards/{student}.md"


def save_student_row(lab, student, row):
    """Merge only ``students[<student>]`` so concurrent agents.py runs are not clobbered."""
    with (lab.ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = lab.load()
        merged.setdefault("students", {})[student] = row
        lab.save(merged)
        return merged


def student_card(lab, manifest, student, row):
    """Render one recorded student's card from the manifest alone (no network). The 模型 row
    is the lab config's ``model_id`` (N17-obs557: the single source of truth), never a stale
    manifest copy."""
    staff = (row.get("staff") or {}).get("username") or staff_for(student)
    names = resource_names(student, staff)
    kb = row.get("kb") or {}
    values = card_values(
        names, model_id=lab.CONFIG["model_id"], kb_ds=kb.get("ds_id"), kb_id=kb.get("kb_id"),
        console_url=lab.PUBLIC, prepared_at=row.get("prepared_at", ""),
        gateway_name=(manifest.get("gateway") or {}).get("id")
        or (manifest.get("gateway") or {}).get("name", ""),
        employee_prebuilt=employee_agent_prebuilt(lab.CONFIG),
    )
    return render_card(values)


def cards_only(args):
    """Re-write the cards of every student the manifest records; manifest-only, no API."""
    import lab

    lab.sync_assets()  # N17-obs558: prompts / knowledge follow the package
    manifest = lab.load()
    sync_model_id(lab, manifest)  # the manifest copy follows the config before the cards do
    students = manifest.get("students") or {}
    if args.student:
        if args.student not in students:
            raise RuntimeError(f"{args.student} is not recorded in the manifest; run the kit first")
        students = {args.student: students[args.student]}
    if not students:
        print("No students recorded in the manifest; nothing to write")
        return
    for student, row in students.items():
        card = student_card(lab, manifest, student, row)
        row["card_path"] = write_card(lab.ROOT, student, card)
        save_student_row(lab, student, row)
        print("Card written", lab.ROOT / row["card_path"], flush=True)


def dry_run(args, names):
    import os

    workspace, prefix, model_id = "<workspace>", "/launchpad/<scenario>/", "<model_id>"
    prebuilt = True
    if os.environ.get("LAUNCHPAD_LAB_CONFIG"):
        try:
            import lab

            workspace, prefix = lab.CONFIG["workspace"], lab.PREFIX
            model_id = lab.CONFIG["model_id"]
            prebuilt = employee_agent_prebuilt(lab.CONFIG)
        except Exception as exc:  # the plan is printable without a usable config
            print(f"(config not loaded: {exc.__class__.__name__}; showing placeholders)")
    print(f"DRY RUN — {names['student']} / {names['staff']}: nothing is called.\n")
    for i, step in enumerate(plan_steps(names, workspace=workspace, prefix=prefix), 1):
        print(f"{i:2}. [{step['as']}] {step['call']}\n      {step['why']}")
    print("\nEvaluation Agent tools:", ", ".join(
        gateway_tool_names(GATEWAY_TARGETS[0], EVAL_READ_TOOLS)))
    print("Service-desk Agent tools: targets", ", ".join(GATEWAY_TARGETS), "(all tools)")
    print("Skills: service desk = all approved Skills; evaluation = all but",
          ", ".join(STAFF_ONLY_SKILLS))
    print("\n" + render_card(card_values(names, model_id=model_id, employee_prebuilt=prebuilt)))


class Kit:
    """One student's provisioning run against the live platform (imports lab lazily)."""

    def __init__(self, args):
        import lab

        lab.sync_assets()  # N17-obs558: prompts / knowledge / datasets follow the package
        self.lab = lab
        self.args = args
        self.names = resource_names(args.student, args.staff)
        self.student, self.staff = self.names["student"], self.names["staff"]
        self.admin = lab.session()
        self.m = lab.load()
        self.row = self.m.setdefault("students", {}).get(self.student) or {
            "staff": None, "kb": None, "datasets": {}, "agents": {}, "ownership": "created",
        }
        self.sessions = {}

    # ----- manifest ---------------------------------------------------------------
    def save(self):
        """Merge only ``students[<student>]`` so concurrent agents.py runs are not clobbered."""
        self.m = save_student_row(self.lab, self.student, self.row)

    def session_as(self, user):
        if user not in self.sessions:
            self.sessions[user] = self.lab.session(user)
        return self.sessions[user]

    def api(self, c, method, path, body=None, **kw):
        return self.lab.api(c, method, path, body, **kw)

    def owned(self, kind, name):
        """Refuse to adopt a same-named resource the manifest does not record."""
        raise RuntimeError(
            f"Unowned existing {kind}: {name}; not recorded in manifest students[{self.student}]"
            " — do not overwrite. Remove or rename it by hand first."
        )

    def workshop_staff_row(self, live_user):
        """The manifest ``users[<staff>]`` row prepare_console.py (``workshop.py console`` /
        ``staff``) wrote for a desk account already listed in ``config.staff_accounts`` — the
        only existing account the kit may adopt (N17-obs549). None for anything else: a row of
        another account with the same name (id mismatch) is as unowned as no row."""
        recorded = (self.m.get("users") or {}).get(self.staff)
        if not isinstance(recorded, dict) or recorded.get("ownership") != "created":
            return None
        if recorded.get("id") and recorded["id"] != live_user.get("id"):
            return None
        return recorded

    # ----- 1. accounts ------------------------------------------------------------
    def ensure_staff(self):
        lab = self.lab
        users = self.api(self.admin, "GET", "/api/users?status=all&limit=200")["items"]
        by_name = {u["username"]: u for u in users}
        if self.student not in by_name:
            raise RuntimeError(f"{self.student} does not exist; run prepare_console.py first")
        staff_accounts = lab.CONFIG.get("staff_accounts")
        if isinstance(staff_accounts, list) and self.staff not in staff_accounts:
            print(f"WARNING: {self.staff} is not in config.staff_accounts; the Cedar requester /"
                  " publish / resolve rules and the tools backend will not recognise it.",
                  flush=True)
        if self.row.get("staff") and self.row["staff"].get("id"):
            return
        key = lab.PREFIX + "users/" + self.staff
        if self.staff in by_name:
            recorded = self.workshop_staff_row(by_name[self.staff])
            if recorded is None:
                self.owned("user", self.staff)
            # staff_accounts listed the desk account up front, so workshop.py created it
            # (same SSM credential key, its own permission grant): adopt, do not re-create.
            self.row["staff"] = {
                "username": self.staff, "id": by_name[self.staff]["id"],
                "role": recorded.get("role", "member"),
                "credential_parameter": recorded.get("credential_parameter") or key,
                "permissions": None,  # left as prepare_console.py granted them
                "ownership": "adopted", "adopted_from": "users",
            }
            self.save()
            print("Service-desk account adopted from the workshop stage", self.staff, flush=True)
            return
        try:
            password = lab.SSM.get_parameter(Name=key, WithDecryption=True)["Parameter"]["Value"]
        except lab.SSM.exceptions.ParameterNotFound:
            password = "It9!" + secrets.token_urlsafe(24)
            lab.SSM.put_parameter(
                Name=key, Type="SecureString", Value=password, Overwrite=False,
                Description="ITSM CN workshop service-desk credential; scenario-owned",
            )
        body = {"username": self.staff, "email": self.staff + "@itsm-lab.example.org",
                "password": password}
        r = self.admin.post(lab.BASE + "/api/auth/register", json=body, timeout=60)
        if r.status_code == 429:
            print("Registration rate limit; retry in 60 seconds", flush=True)
            time.sleep(60)
            r = self.admin.post(lab.BASE + "/api/auth/register", json=body, timeout=60)
        if not r.ok:
            # Validation errors may echo the submitted password; never print the body.
            raise RuntimeError(f"Register {self.staff}: HTTP {r.status_code}")
        del password, body
        users = self.api(self.admin, "GET", "/api/users?status=all&limit=200")["items"]
        user = next(u for u in users if u["username"] == self.staff)
        self.api(self.admin, "PATCH", "/api/users/" + user["id"], {
            "status": "active", "role": "member",
            "workspace_ids": [lab.CONFIG["workspace"]], "permissions": STAFF_PERMISSIONS,
        })
        self.row["staff"] = {"username": self.staff, "id": user["id"], "role": "member",
                             "credential_parameter": key, "permissions": STAFF_PERMISSIONS,
                             "ownership": "created"}
        self.save()
        print("Service-desk account ready", self.staff, flush=True)

    # ----- 2. personal knowledge base ------------------------------------------------
    def ensure_kb(self):
        lab, name = self.lab, self.names["kb"]
        if not self.row.get("kb"):
            existing = self.api(self.admin, "GET", "/api/knowledge-bases")["items"]
            if any(k.get("name") == name for k in existing):
                self.owned("knowledge base", name)
            # Admin-created: visible to BOTH of the student's logins in either workspace
            # visibility mode (an isolated workspace hides a member's KB from every other
            # member — there is no per-member grant to give the staff login access).
            created = self.api(self.admin, "POST", "/api/knowledge-bases", {
                "name": name,
                "description": f"{self.student} 个人 IT 服务台知识库：5 份基础资料＋本人发布的文章",
                "source": {"mode": "upload"},
            })
            self.row["kb"] = {**created, "ownership": "created", "uploaded": False,
                              "synced": False}
            self.save()
            print("KB created", created.get("kb_id"), flush=True)
        kb_id = self.row["kb"]["kb_id"]
        for _ in range(KB_POLL_ROUNDS):
            kb = self.api(self.admin, "GET", "/api/knowledge-bases/" + kb_id)
            if kb.get("status") == "ACTIVE" and kb.get("data_sources"):
                break
            time.sleep(KB_POLL_SECONDS)
        else:
            raise RuntimeError(f"KB {name} not ready")
        ds_id = kb["data_sources"][0]["ds_id"]
        self.row["kb"].update(kb, ds_id=ds_id)
        self.save()
        if not self.row["kb"].get("uploaded"):
            docs = sorted((lab.ROOT / "lab-assets/knowledge").glob("*.md"))
            if len(docs) != 5:
                raise RuntimeError(f"expected the 5 shared knowledge documents, found {len(docs)}")
            r = self.admin.post(
                lab.BASE + f"/api/knowledge-bases/{kb_id}/files",
                files=[("files", (p.name, p.read_bytes(), "text/markdown")) for p in docs],
                timeout=180,
            )
            r.raise_for_status()
            upload = r.json()
            lab.evidence(f"student-{self.student}-kb-upload.json", upload)
            self.row["kb"]["uploaded"] = True
            self.row["kb"]["upload_job"] = upload.get("job")
            self.save()
            if not upload.get("job"):
                self.api(self.admin, "POST",
                         f"/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync", {})
            print("Knowledge uploaded", name, flush=True)
        if not self.row["kb"].get("synced"):
            self.wait_sync(kb_id, ds_id)

    def wait_sync(self, kb_id, ds_id):
        path = f"/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"
        started = (self.row["kb"].get("upload_job") or {}).get("job_id")
        for _ in range(SYNC_POLL_ROUNDS):
            jobs = self.api(self.admin, "GET", path)["items"]
            if jobs:
                # the job the upload started when known, otherwise the newest one
                latest = next((j for j in jobs if j.get("job_id") == started), None) or max(
                    jobs, key=lambda j: str(j.get("created_at") or "")
                )
                status = str(latest.get("status") or "").upper()
                if status and status not in RUNNING_JOB_STATUSES:
                    if status in ("FAILED", "STOPPED"):
                        self.lab.evidence(f"student-{self.student}-kb-sync-failed.json", latest)
                        raise RuntimeError(f"KB sync {status}; see evidence")
                    self.row["kb"]["synced"] = True
                    self.row["kb"]["sync_job"] = latest
                    self.save()
                    print("KB synced", self.names["kb"], status, flush=True)
                    return
            time.sleep(SYNC_POLL_SECONDS)
        raise RuntimeError("KB sync did not finish; inspect the ingestion jobs before retrying")

    # ----- 3. personal datasets (as the student) -----------------------------------------
    def ensure_datasets(self):
        c = self.session_as(self.student)
        existing = {d["name"]: d for d in self.api(c, "GET", "/api/eval/datasets")["datasets"]}
        for shared in SHARED_DATASETS:
            document = json.loads((self.lab.ROOT / "lab-assets" / f"{shared}.json").read_text())
            body = adapt_dataset_document(document, self.student)
            if body["name"] in self.row["datasets"]:
                continue
            if body["name"] in existing:
                self.owned("dataset", body["name"])
            created = self.api(c, "POST", "/api/eval/datasets", body)
            self.row["datasets"][body["name"]] = {
                "id": created["id"], "item_count": created.get("item_count"),
                "revision": created.get("revision"), "ownership": "created",
                "created_by": self.student,
            }
            self.save()
            print("Dataset created", body["name"], created.get("item_count"), "items",
                  flush=True)

    # ----- 4. the two Agents ------------------------------------------------------------
    def check_read_tools(self, c):
        """The servicenow record must declare the two read tools under the Gateway's names."""
        wanted = gateway_tool_names(GATEWAY_TARGETS[0], EVAL_READ_TOOLS)
        catalog = self.api(c, "GET", "/api/registry/attachables")
        record = next((r for r in catalog.get("mcp_servers", [])
                       if r.get("name") == GATEWAY_TARGETS[0]), None)
        declared = (record or {}).get("runtime_tools")
        if declared is not None:
            missing = [t for t in wanted if t not in declared]
            if missing:
                raise RuntimeError(f"servicenow record does not declare {missing}; "
                                   f"declared: {declared}")
        return wanted

    def agent_spec(self, kind):
        prompts = json.loads((self.lab.ROOT / "lab-assets/prompts.json").read_text())
        return build_agent_spec(
            kind, self.names, model_id=self.lab.CONFIG["model_id"], kb_id=self.row["kb"]["kb_id"],
            gateway=self.m["gateway"], skills=self.m["skills"], prompts=prompts,
        )

    def ensure_agent(self, kind):
        user = self.student if kind == "eval" else self.staff
        c = self.session_as(user)
        if kind == "eval":
            self.check_read_tools(c)
        spec = self.agent_spec(kind)
        row = self.row["agents"].get(kind)
        if row and row.get("ready"):
            print("Agent already ready", spec["name"], row["id"], flush=True)
            return
        if not row:
            live = self.api(c, "GET", "/api/agents")["agents"]
            if any(a.get("name") == spec["name"] for a in live):
                self.owned("agent", spec["name"])
            response = self.api(c, "POST", "/api/agents", spec)
            agent = response.get("agent", response)
            row = {"id": agent["id"], "name": spec["name"], "created_by": user, "ready": False,
                   "spec": spec, "job_id": response.get("job_id"), "ownership": "created"}
            self.row["agents"][kind] = row
            self.save()
            self.lab.evidence(f"student-{self.student}-agent-{kind}-deploy.json", response)
            print("Deployment submitted", spec["name"], row["id"], flush=True)
        start = time.monotonic()
        for _ in range(AGENT_POLL_ROUNDS):
            detail = self.api(c, "GET", "/api/agents/" + row["id"])
            item = detail.get("agent", detail)
            if item.get("status") in ("active", "running"):
                row.update(ready=True, seconds=round(time.monotonic() - start),
                           allowed_tools=(item.get("spec") or {}).get("allowed_tools"))
                self.save()
                self.lab.evidence(f"student-{self.student}-agent-{kind}-ready.json", detail)
                print("Agent ready", spec["name"], row["seconds"], "s", flush=True)
                return
            if item.get("status") in ("failed", "error"):
                self.lab.evidence(f"student-{self.student}-agent-{kind}-failed.json", detail)
                raise RuntimeError(str(item)[:500])
            time.sleep(AGENT_POLL_SECONDS)
        raise RuntimeError(f"Deployment of {spec['name']} did not finish")

    # ----- 5. caller map ------------------------------------------------------------------
    def refresh_callers(self):
        policy = (self.m.get("policies") or {}).get("itsm_agents_call_tools")
        engine = self.m.get("engine")
        if not policy or not engine:
            print("No lab engine/policy in the manifest; skipping the caller-map refresh")
            return
        result = self.api(
            self.admin, "POST",
            f"/api/policy-engines/{engine['id']}/policies/{policy['id']}/publish",
            {"version": policy.get("active_version") or 1,
             "reason": f"Workshop: caller roles after {self.student} kit; unchanged Cedar."},
        )
        self.lab.evidence(f"student-{self.student}-caller-refresh.json", result)
        self.row["caller_refresh"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.save()
        print("Caller map refreshed; allow the Gateway configuration cache to refresh.")

    def run(self):
        # Refuse a model the console does not offer BEFORE any resource is created: the two
        # Agents would otherwise fail in their Runtime builds (N17-obs557).
        check_model(self.lab, self.admin, self.lab.CONFIG["model_id"])
        sync_model_id(self.lab, self.m)  # then the manifest copy follows the config
        self.ensure_staff()
        self.ensure_kb()
        self.ensure_datasets()
        self.ensure_agent("eval")
        self.ensure_agent("console")
        if not self.args.skip_refresh_callers:
            self.refresh_callers()
        self.row["prepared_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.save()
        self.finish_card()

    def finish_card(self):
        """Render the card, write ``cards/<student>.md`` and record it on the manifest row."""
        card = student_card(self.lab, self.m, self.student, self.row)
        self.lab.evidence(f"student-{self.student}-resource-card.md", {"card": card})
        self.row["card_path"] = write_card(self.lab.ROOT, self.student, card)
        self.save()
        print("\n" + card)
        print("\nCard written", self.lab.ROOT / self.row["card_path"])
        print("Passwords: SSM", self.row["staff"]["credential_parameter"],
              "(service desk) and", self.lab.PREFIX + "users/" + self.student,
              "(student); hand out through a separate channel.")


def main(argv=None):
    args = parse_args(argv)
    if args.cards_only:
        cards_only(args)
        return 0
    names = resource_names(args.student, args.staff or staff_for(args.student))
    if args.dry_run:
        dry_run(args, names)
        return 0
    Kit(args).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
