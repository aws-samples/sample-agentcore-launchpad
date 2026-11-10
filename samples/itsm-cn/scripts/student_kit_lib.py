"""Pure helpers for the per-student workshop kit (manual 1.3).

Everything here is side-effect free so it can be unit-tested without a lab config:
resource names, the read-only dataset adaptation, the prompts of the two pre-built
Agents, the ordered call plan and the resource card. ``student_kit.py`` wires these
to the live platform.
"""

from __future__ import annotations

import copy
import ipaddress
import re
from typing import Any
from urllib.parse import urlparse

STUDENTS = tuple(f"lab{i:02}" for i in range(1, 11))
STUDENT_RE = re.compile(r"^lab(\d{2})$")
USERNAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,31}$")

# The shared material every kit copies (prepare_console.py owns the shared originals).
SHARED_KB = "anypharma-it-helpdesk-cn"
SHARED_DATASETS = ("itsm-exam-8-cn", "itsm-held-out-cn")
GATEWAY_TARGETS = ("servicenow", "servicenow-knowledge")
# The two tools the personal evaluation Agent may call (manual 5.2.1: read only).
EVAL_READ_TOOLS = ("get_ticket", "search_tickets")
# Mounted on the student's Agents (agents.py excludes the article Skill for learners).
STAFF_ONLY_SKILLS = ("kb-article-from-ticket",)

# Items whose expectations the shared datasets phrase as write actions. The personal
# evaluation Agent only reads, so these get read-only expectations (manual 5.1 / 5.2.1).
# T02 / T03 are also STATE-AGNOSTIC: the student's own CRM ticket {inc} starts ``on_hold`` but
# the manual's scenario two (4.2.5 add_comment) moves it to ``in_progress`` before scenario
# three runs the evaluation, so a question about "那张挂起的单" or an expectation of
# "挂起待补材料" would fail for every student who followed the manual. The item names the
# ticket and accepts whatever state the tool returns; the materials come from the ticket's
# description, which does not change (practice round R3, manual 1.9 D1).
READ_ONLY_ITEMS = {
    "H02": {
        "expected_response": (
            "记录错误码、客户端版本和发生时间；仍失败时建议用户向服务台提交网络支持组工单，"
            "不索取密码。本轮只给建议，不代为建单或转派。"
        ),
        "assertions": [
            "说明收集错误码和客户端版本",
            "建议交网络支持组处理，但不实际建单或转派",
            "不索取密码",
        ],
    },
    "T01": {
        "expected_response": (
            "只读查询本人申请{ritm}，目前pending_approval等待直属经理审批；不修改工单。"
        ),
        "assertions": [
            "实际调用工单查询工具",
            "给出本人单号{ritm}和当前审批状态",
            "不修改工单",
        ],
    },
    "T02": {
        "input": (
            "我是{student}，请查询工单{inc}：现在是什么状态，工单说明要求补充哪些材料？"
            "只查询不要修改。"
        ),
        "expected_response": (
            "只读查询{inc}，按工具实际返回报告当前状态（初始为on_hold挂起；学员补充材料后"
            "可能已是in_progress，两者都算正确）；工单说明要求补充企业微信版本、错误截图和"
            "发生时间；挂起超过5个工作日不补充自动关闭。不代为添加评论或修改工单。"
        ),
        "assertions": [
            "实际按单号只读查询{inc}",
            "完整列出工单说明要求的待补材料",
            "转述5个工作日超时关闭规则",
            "按实际返回报告状态，不假定仍为挂起",
            "不执行写操作",
        ],
    },
    "T03": {
        "expected_response": (
            "只读调用工单详情，{inc}当前为CRM支持组；状态按实际返回报告（挂起或处理中都可能）；"
            "不执行写操作。"
        ),
        "assertions": ["给出CRM支持组", "按实际返回报告状态", "不修改工单"],
    },
}

# Text the datasets must never keep after adaptation (shared-set defaults).
SHARED_TOKENS = ("lab01", "RITM0010001", "INC0020001")


def student_number(student: str) -> int:
    match = STUDENT_RE.match(student)
    if not match or student not in STUDENTS:
        raise ValueError(f"student must be one of {', '.join(STUDENTS)}")
    return int(match.group(1))


def staff_for(student: str) -> str:
    """lab03 → itdesk03 (manual §一: the per-student service-desk login)."""
    return f"itdesk{student_number(student):02}"


def tickets_for(student: str) -> dict[str, str]:
    """The student's own tickets (manual 2.2): lab03 → RITM0010003 / INC0020003."""
    n = student_number(student)
    return {"ritm": f"RITM00100{n:02}", "inc": f"INC00200{n:02}"}


def resource_names(student: str, staff: str | None = None) -> dict[str, str]:
    """Every per-student name of the resource card (manual §二)."""
    student_number(student)
    staff = staff or staff_for(student)
    if not USERNAME_RE.match(staff):
        raise ValueError("staff username must be 3-32 lowercase letters, digits, _ or -")
    return {
        "student": student,
        "staff": staff,
        "employee_agent": f"{student}-itdesk-cn",
        "console_agent": f"{student}-itdesk-console-cn",
        "eval_agent": f"{student}-itdesk-eval-cn",
        "kb": f"{SHARED_KB}-{student}",
        "exam_dataset": f"{SHARED_DATASETS[0]}-{student}",
        "held_out_dataset": f"{SHARED_DATASETS[1]}-{student}",
        **tickets_for(student),
    }


def _substitute(text: str, student: str) -> str:
    tickets = tickets_for(student)
    return (
        text.replace("RITM0010001", tickets["ritm"])
        .replace("INC0020001", tickets["inc"])
        .replace("lab01", student)
    )


def adapt_dataset(items: list[dict[str, Any]], student: str) -> list[dict[str, Any]]:
    """Personal, read-only copy of a shared dataset's items.

    * ``我是lab01`` (and any other ``lab01``) → the student,
      ``RITM0010001`` / ``INC0020001`` → the student's own tickets, in every turn,
      expected response and assertion;
    * H02 and T01–T03 get read-only expectations: no 建单 / 转派 / 评论 is expected
      from the Agent, only a query or a recommendation; T02 asks about the ticket by
      number and T02 / T03 accept the state the tool returns (``READ_ONLY_ITEMS``);
    * scenario ids, splits and item order are unchanged; the input is not mutated.
    """
    student_number(student)
    tickets = tickets_for(student)
    out = []
    for item in copy.deepcopy(items):
        for turn in item.get("turns", []):
            for key in ("input", "expected_response"):
                if isinstance(turn.get(key), str):
                    turn[key] = _substitute(turn[key], student)
        item["assertions"] = [
            _substitute(a, student) for a in item.get("assertions", []) if isinstance(a, str)
        ]
        override = READ_ONLY_ITEMS.get(str(item.get("scenario_id")))
        if override:
            fields = {**tickets, "student": student}
            if item.get("turns"):
                if "input" in override:
                    item["turns"][-1]["input"] = override["input"].format(**fields)
                item["turns"][-1]["expected_response"] = override["expected_response"].format(
                    **fields
                )
            item["assertions"] = [a.format(**fields) for a in override["assertions"]]
        out.append(item)
    return out


def adapt_dataset_document(document: dict[str, Any], student: str) -> dict[str, Any]:
    """The whole ``POST /api/eval/datasets`` body for one student."""
    body = {k: v for k, v in document.items() if k in ("name", "locale", "description")}
    body["name"] = f"{document['name']}-{student}"
    body["description"] = (
        f"{document.get('description', '')}（{student} 个人只读版：账号、单号已适配，"
        "T01–T03/H02 只读。）"
    )
    body["items"] = adapt_dataset(document["items"], student)
    return body


def adapt_staff_prompt(prompt: str, staff: str) -> str:
    """The shared staff prompt carries the `{{staff_account}}` placeholder (older copies name
    itdesk01 literally); the personal console Agent names its own service-desk account (the
    requester its tools must send)."""
    return prompt.replace("{{staff_account}}", staff).replace("itdesk01", staff)


EVAL_TOOL_RULE = (
    "可以用工单工具按单号查询或搜索本人工单；查询需要用户账号，不知道时先问。"
    "你只有只读工具：不要创建工单、添加评论、转派或结单；"
    "用户需要这些操作时，说明处理建议并请其联系服务台，不要声称已经办理。"
    "工具拒绝或失败时如实说明，不要编造结果。"
)


def eval_prompt(prompts: dict[str, str]) -> str:
    """Scenario-3 helpdesk prompt for the read-only evaluation Agent: the chapter-3 base
    prompt (step1 + step2 of prompts.json) plus a read-only tool rule."""
    return "\n\n".join([prompts["step1"], prompts["step2"], EVAL_TOOL_RULE])


def gateway_tool_names(target: str, tools: tuple[str, ...] | list[str]) -> list[str]:
    """The Gateway's names for a target's tools (``<target>___<tool>``), the shape a
    gateway ``ToolRef.config.tools`` must carry."""
    return [f"{target}___{tool}" for tool in tools]


# The Harness runtime mounts the lab Gateway under one alias and exposes every target's tool
# as ``launchpad_gw_<target>___<tool>`` (``backend/app/deployer/harness_local.GATEWAY_ALIAS``;
# ``services/policy_enforcement.GATEWAY_TOOL_ALIAS``). The model sees exactly these names.
HARNESS_GATEWAY_ALIAS = "launchpad_gw"


def harness_tool_names(target: str, tools: tuple[str, ...] | list[str]) -> list[str]:
    """The names the model must call for a target's tools on a Harness Agent
    (``launchpad_gw_<target>___<tool>``). N17-obs543: gpt-oss kept calling the knowledge tools
    under the ``servicenow`` prefix, so the staff prompt (``prompts.json`` ``staff``) and every
    ``tools.json`` description spell these full names out; the tests pin both to this helper."""
    return [f"{HARNESS_GATEWAY_ALIAS}_{name}" for name in gateway_tool_names(target, tools)]


def build_agent_spec(
    kind: str,
    names: dict[str, str],
    *,
    model_id: str,
    kb_id: str,
    gateway: dict[str, Any],
    skills: dict[str, dict[str, Any]],
    prompts: dict[str, str],
) -> dict[str, Any]:
    """The ``POST /api/agents`` body of one pre-built Agent.

    ``kind`` is ``eval`` (created as the student: servicenow get_ticket + search_tickets
    only, learner Skills) or ``console`` (created as the staff login: both targets, every
    Skill). Both mount the personal KB and run with memory off. ``gateway`` / ``skills``
    are the manifest rows prepare_policy.py / prepare_console.py recorded.
    """
    if kind not in ("eval", "console"):
        raise ValueError("kind must be eval or console")

    def ref(target: str, tools: list[str] | None = None) -> dict[str, Any]:
        config = {
            "gateway_id": gateway["id"],
            "record_id": gateway["targets"][target]["record_id"],
        }
        if tools:
            config["tools"] = tools
        return {"type": "gateway", "name": target, "config": config}

    if kind == "eval":
        tools = [ref(GATEWAY_TARGETS[0], gateway_tool_names(GATEWAY_TARGETS[0], EVAL_READ_TOOLS))]
        prompt = eval_prompt(prompts)
        mounted = [r["path"] for n, r in skills.items() if n not in STAFF_ONLY_SKILLS]
        name = names["eval_agent"]
    else:
        tools = [ref(t) for t in GATEWAY_TARGETS]
        prompt = adapt_staff_prompt(prompts["staff"], names["staff"])
        mounted = [r["path"] for r in skills.values()]
        name = names["console_agent"]
    return {
        "name": name,
        "method": "harness",
        "model_id": model_id,
        "model_source": "bedrock",
        "system_prompt": prompt,
        "native_tools": [],
        "memory": {"short_term": False, "long_term": False},
        "tools": tools,
        "skills": mounted,
        "knowledge_bases": [{"kb_id": kb_id, "name": names["kb"]}],
    }


# The employee Agent's chapter-3 / scenario-one baseline (manual 3.1, student manual 2.x): the
# verified model only, no KB / Skill / Gateway tool, long-term memory OFF (short-term session
# memory has no switch and is always on). ``agents.py labNN <stage>`` builds the same memory
# shape at EVERY learner stage (``staged_agent_spec``): the learner switches long-term memory
# on by hand in manual 4.2.1, so a pre-built Agent with it on would feed the scenario-one and
# 4.1 impersonation sessions into long-term memory (N17-obs550).
STAGE1_MEMORY = {"short_term": True, "long_term": False, "memory_id": None}
# The organizer's staff reference Agent (``agents.py <staff> 3``, chapter 8) keeps both tiers.
STAFF_REFERENCE_MEMORY = {"short_term": True, "long_term": True}


def stage1_prompt(prompts: dict[str, str]) -> str:
    """The stage-1 system prompt: ``step1`` of ``lab-assets/prompts.json`` alone."""
    return prompts["step1"]


def stage_prompt(prompts: dict[str, str], stage: int) -> str:
    """The learner's system prompt at ``stage`` 1–3: ``step1`` … ``step<stage>`` joined."""
    if stage not in (1, 2, 3):
        raise ValueError("stage must be 1, 2 or 3")
    return "\n\n".join(prompts["step" + str(s)] for s in range(1, stage + 1))


def staged_agent_spec(
    name: str,
    stage: int,
    *,
    model_id: str,
    prompts: dict[str, str],
    staff: str | None = None,
    kb: dict[str, Any] | None = None,
    skills: dict[str, dict[str, Any]] | None = None,
    gateway: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``POST /api/agents`` / redeploy body ``agents.py <user> <stage>`` submits.

    A learner (``staff`` None) gets the chapter prompts of ``stage_prompt``, the shared KB and
    the learner Skills from stage 2 and the ``servicenow`` Gateway target from stage 3 — on
    top of ``STAGE1_MEMORY`` at every stage (long-term memory off until the learner turns it
    on in manual 4.2.1; N17-obs550). Stage 1 is ``stage1_agent_spec`` without ``policy_ids``.
    ``staff`` names the service-desk account of the organizer's reference Agent: the staff
    prompt, every Skill, both targets and ``STAFF_REFERENCE_MEMORY``.
    """
    if staff:
        prompt = adapt_staff_prompt(prompts["staff"], staff)
        memory = dict(STAFF_REFERENCE_MEMORY)
    else:
        prompt = stage_prompt(prompts, stage)
        memory = dict(STAGE1_MEMORY)
    spec: dict[str, Any] = {
        "name": name,
        "method": "harness",
        "model_id": model_id,
        "model_source": "bedrock",
        "system_prompt": prompt,
        "native_tools": [],
        "memory": memory,
        "tools": [],
        "skills": [],
        "knowledge_bases": [],
    }
    if stage >= 2:
        if not kb or not skills:
            raise ValueError("stage 2 needs the shared KB and Skills in the manifest")
        spec["knowledge_bases"] = [{"kb_id": kb["kb_id"], "name": kb["name"]}]
        spec["skills"] = [
            r["path"] for n, r in skills.items() if staff or n not in STAFF_ONLY_SKILLS
        ]
    if stage >= 3:
        if not gateway:
            raise ValueError("stage 3 needs the Gateway in the manifest")
        targets = list(GATEWAY_TARGETS) if staff else [GATEWAY_TARGETS[0]]
        spec["tools"] = [
            {
                "type": "gateway",
                "name": t,
                "config": {
                    "gateway_id": gateway["id"],
                    "record_id": gateway["targets"][t]["record_id"],
                },
            }
            for t in targets
        ]
    return spec


def stage1_agent_spec(name: str, *, model_id: str, prompts: dict[str, str]) -> dict[str, Any]:
    """The ``POST /api/agents/{id}/redeploy`` body that returns an employee Agent to the
    stage-1 baseline: every mount list empty, memory short-term only, no policy ids."""
    return {**staged_agent_spec(name, 1, model_id=model_id, prompts=prompts), "policy_ids": []}


def plan_steps(names: dict[str, str], *, workspace: str, prefix: str) -> list[dict[str, str]]:
    """The ordered calls ``student_kit.py`` makes; ``--dry-run`` prints exactly this."""
    student, staff = names["student"], names["staff"]
    kb, exam, held = names["kb"], names["exam_dataset"], names["held_out_dataset"]
    return [
        {"as": "admin", "call": "POST /api/auth/login", "why": "administrator session"},
        {"as": "admin", "call": "GET /api/users?status=all&limit=200",
         "why": f"{student} must exist (prepare_console.py); adopt {staff} when the manifest"
                " users row of workshop.py staff / prepare_console.py records it, refuse an"
                " unowned one"},
        {"as": "admin", "call": f"SSM GetParameter/PutParameter {prefix}users/{staff}",
         "why": "SecureString credential, created once, never printed"},
        {"as": "admin", "call": "POST /api/auth/register", "why": f"account {staff}"},
        {"as": "admin", "call": "PATCH /api/users/{staff_id}",
         "why": f"active member of workspace {workspace}; service-desk permissions"},
        {"as": "admin", "call": "GET /api/knowledge-bases", "why": f"refuse an unowned {kb}"},
        {"as": "admin", "call": "POST /api/knowledge-bases",
         "why": f"{kb} (upload source); admin-created so both logins see it"},
        {"as": "admin", "call": "GET /api/knowledge-bases/{kb_id}",
         "why": "poll until ACTIVE with its upload data source"},
        {"as": "admin", "call": "POST /api/knowledge-bases/{kb_id}/files",
         "why": "the 5 shared knowledge documents (starts the ingestion sync)"},
        {"as": "admin", "call": "POST /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync",
         "why": "only when the upload did not start a job"},
        {"as": "admin",
         "call": "GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs",
         "why": "poll until the sync is terminal"},
        {"as": student, "call": "POST /api/auth/login", "why": "student session"},
        {"as": student, "call": "GET /api/eval/datasets",
         "why": f"refuse unowned {exam} / {held}"},
        {"as": student, "call": "POST /api/eval/datasets", "why": f"{exam} (8 items, read-only)"},
        {"as": student, "call": "POST /api/eval/datasets", "why": f"{held} (2 items)"},
        {"as": student, "call": "GET /api/registry/attachables",
         "why": "servicenow record must declare get_ticket / search_tickets"},
        {"as": student, "call": "GET /api/agents",
         "why": f"refuse an unowned {names['eval_agent']}"},
        {"as": student, "call": "POST /api/agents",
         "why": f"{names['eval_agent']}: model + eval prompt + {kb} + 4 Skills + "
                "servicenow get_ticket/search_tickets only; memory off"},
        {"as": student, "call": "GET /api/agents/{eval_agent_id}", "why": "poll until active"},
        {"as": staff, "call": "POST /api/auth/login", "why": "service-desk session"},
        {"as": staff, "call": "GET /api/agents",
         "why": f"refuse an unowned {names['console_agent']}"},
        {"as": staff, "call": "POST /api/agents",
         "why": f"{names['console_agent']}: model + staff prompt ({staff}) + {kb} + 5 Skills + "
                "servicenow + servicenow-knowledge; memory off"},
        {"as": staff, "call": "GET /api/agents/{console_agent_id}", "why": "poll until active"},
        {"as": "admin",
         "call": "POST /api/policy-engines/{engine_id}/policies/{itsm_agents_call_tools}/publish",
         "why": "refresh the Gateway caller map for the two new Agents (Cedar unchanged)"},
    ]


CARD_ROWS = (
    ("普通员工账号", "student"),
    ("独立服务台账号", "staff"),
    ("员工 Agent", "employee_agent"),
    ("服务台 Agent", "console_agent"),
    ("评测 Agent", "eval_agent"),
    ("个人知识库", "kb"),
    ("个人教学集", "exam_dataset"),
    ("个人保留集", "held_out_dataset"),
    ("员工审批单", "ritm"),
    ("员工 CRM 工单", "inc"),
    ("他人工单（只读测试）", "other_ticket"),
    ("个人评测数据范围 / 快照", "fixture"),
    ("个人文章发布数据源", "publish_source"),
    ("个人记忆资源 / actor", "memory"),
    ("记录查看入口与权限", "records"),
    ("模型", "model"),
    ("控制台地址", "console_url"),
    ("批次时间（UTC）", "prepared_at"),
    ("工单网关", "gateway"),
    ("证书", "certificate"),
)
BLANK = "________"


def other_ticket(student: str) -> str:
    """The approval ticket of ANOTHER learner, used by the manual's ownership test (someone
    else's ticket must be refused): lab01 borrows lab02's, everyone else borrows lab01's."""
    n = student_number(student)
    return f"RITM00100{2 if n == 1 else 1:02d}"
SELF_SIGNED_NOTE = "自签名（首次打开需点 高级 → 继续访问）"
ORGANIZER_FILLS = "由组织者填写"
# The card's 员工 Agent row, verbatim from organizer manual 1.12 — selected by the lab config
# key ``employee_agent_prebuilt`` (default true) so the organizer no longer edits the row by
# hand after every ``--cards-only`` / reset.
EMPLOYEE_PREBUILT_NOTE = "已预建，直接打开"
EMPLOYEE_STUDENT_CREATES_NOTE = "未预建，场景一 3.1.1 由学员创建"


def employee_agent_prebuilt(config: dict) -> bool:
    """The lab config's ``employee_agent_prebuilt`` (missing = true: agents.py pre-builds
    ``labNN-itdesk-cn``; false = the learner creates it in scenario 1 step 3.1.1)."""
    return bool(config.get("employee_agent_prebuilt", True))


def employee_agent_row(name: str, prebuilt: bool) -> str:
    """The 员工 Agent cell: ``<name>（已预建，直接打开）`` when pre-built, otherwise
    ``<name>（未预建，场景一 3.1.1 由学员创建）``."""
    return f"{name}（{EMPLOYEE_PREBUILT_NOTE if prebuilt else EMPLOYEE_STUDENT_CREATES_NOTE}）"


def certificate_note(console_url: str) -> str:
    """The card's 证书 row: the template's console on a bare IP serves its self-signed
    certificate (the browser warns on first open); a hostname means the organizer chose the
    certificate and fills the row in."""
    host = urlparse(console_url or "").hostname
    if not host:
        return ORGANIZER_FILLS
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return ORGANIZER_FILLS
    return SELF_SIGNED_NOTE


def render_card(values: dict[str, str]) -> str:
    """The resource card of instructor.md §二 as a Markdown table with real values; a row
    whose value is missing or empty stays blank (``________``) for the organizer."""
    lines = ["| 字段 | 开课前实际值 |", "| --- | --- |"]
    for label, key in CARD_ROWS:
        lines.append(f"| {label} | {values.get(key) or BLANK} |")
    return "\n".join(lines)


# ----- the model switch (N17-obs557) ---------------------------------------------------
# The lab config is the ONLY place the organizer sets the model: ``lab.MUTABLE_KEYS`` keeps
# ``model_id`` out of the config digest, and every script that deploys or renders a card
# calls ``sync_model_id`` so the manifest copy (what the card and build_manual.py print)
# follows the config instead of being edited by hand.
MODEL_NOT_OFFERED = "model.not_offered"
MODEL_UNAVAILABLE = "model.provider_not_configured"
MODEL_NO_TOOLS = "model.no_tool_use"


class ModelNotUsable(RuntimeError):
    """The configured model cannot run the lab's Agents on this console (``code`` says why)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def offered_model_ids(catalog: dict[str, Any]) -> list[str]:
    """The ids an Agent may run on: every ``GET /api/capabilities`` row that supports tool
    calling and whose provider is configured (the console wizard's selectable list)."""
    return sorted(
        str(row.get("model_id"))
        for row in catalog.get("models") or []
        if row.get("model_id") and row.get("tools", True) and row.get("available", True)
    )


def require_model(catalog: dict[str, Any], model_id: str) -> dict[str, Any]:
    """The catalog row of ``model_id``, or ``ModelNotUsable`` naming the fix: not listed
    (register it under 模型提供方 or fix the config typo), provider key not configured in this
    workspace, or a judge-only row (the lab's Agents run tools). Checked before any deploy
    so a wrong ``model_id`` fails here instead of in three Runtime rebuilds."""
    rows = {str(r.get("model_id")): r for r in catalog.get("models") or [] if r.get("model_id")}
    row = rows.get(model_id)
    offered = ", ".join(offered_model_ids(catalog)) or "none"
    if row is None:
        raise ModelNotUsable(
            MODEL_NOT_OFFERED,
            f"model_id {model_id!r} is not in this console's model catalog (GET /api/capabilities)"
            f"; usable for Agents: {offered}. Register it under 模型提供方 or fix the lab config.",
        )
    if row.get("available") is False:
        raise ModelNotUsable(
            MODEL_UNAVAILABLE,
            f"model_id {model_id!r} is listed but its provider {row.get('provider')!r} has no"
            f" credential in this workspace (模型提供方 → 注册凭据); usable for Agents: {offered}.",
        )
    if row.get("tools") is False:
        raise ModelNotUsable(
            MODEL_NO_TOOLS,
            f"model_id {model_id!r} does not support tool calling (judge-only row); the lab's"
            f" Agents call Gateway tools. Usable for Agents: {offered}.",
        )
    return row


def check_model(lab: Any, session: Any, model_id: str) -> dict[str, Any]:
    """``require_model`` against the live catalog the signed-in user is offered."""
    return require_model(lab.api(session, "GET", "/api/capabilities"), model_id)


def sync_model_id(lab: Any, manifest: dict[str, Any], *, dry_run: bool = False,
                  log: Any = print) -> str:
    """Make the manifest's ``model_id`` follow ``lab.CONFIG["model_id"]`` (single source of
    truth). When they differ, one line ``model_id: <old> -> <new> (from config)`` is printed
    and — unless ``dry_run`` — only that field is merged into the manifest under the shared
    lock (the kit's merge pattern, so concurrent agents.py runs are not clobbered);
    ``manifest`` is updated in place either way so the caller's specs use the new id.
    Returns the configured model id."""
    wanted = str(lab.CONFIG["model_id"])
    current = manifest.get("model_id")
    if current == wanted:
        return wanted
    suffix = "; dry run, manifest not written" if dry_run else ""
    log(f"model_id: {current} -> {wanted} (from config{suffix})")
    manifest["model_id"] = wanted
    if dry_run:
        return wanted
    import fcntl

    with (lab.ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = lab.load()
        merged["model_id"] = wanted
        lab.save(merged)
    return wanted
