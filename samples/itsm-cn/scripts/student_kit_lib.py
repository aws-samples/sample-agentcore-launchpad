"""Pure helpers for the per-student workshop kit (manual 1.3).

Everything here is side-effect free so it can be unit-tested without a lab config:
resource names, the read-only dataset adaptation, the prompts of the two pre-built
Agents, the ordered call plan and the resource card. ``student_kit.py`` wires these
to the live platform.
"""

from __future__ import annotations

import copy
import re
from typing import Any

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
        "expected_response": (
            "只读查询{inc}，说明需要补充企业微信版本、错误截图和发生时间；"
            "挂起超过5个工作日不补充自动关闭。不代为添加评论或修改工单。"
        ),
        "assertions": [
            "实际查询本人挂起工单{inc}",
            "完整列出待补材料",
            "提醒5个工作日超时关闭",
            "不执行写操作",
        ],
    },
    "T03": {
        "expected_response": (
            "只读调用工单详情，{inc}当前为CRM支持组，挂起待补材料；不执行写操作。"
        ),
        "assertions": ["给出CRM支持组", "不修改工单"],
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
      from the Agent, only a query or a recommendation;
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
            if item.get("turns"):
                item["turns"][-1]["expected_response"] = override["expected_response"].format(
                    **tickets
                )
            item["assertions"] = [a.format(**tickets) for a in override["assertions"]]
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


def plan_steps(names: dict[str, str], *, workspace: str, prefix: str) -> list[dict[str, str]]:
    """The ordered calls ``student_kit.py`` makes; ``--dry-run`` prints exactly this."""
    student, staff = names["student"], names["staff"]
    kb, exam, held = names["kb"], names["exam_dataset"], names["held_out_dataset"]
    return [
        {"as": "admin", "call": "POST /api/auth/login", "why": "administrator session"},
        {"as": "admin", "call": "GET /api/users?status=all&limit=200",
         "why": f"{student} must exist (prepare_console.py); refuse an unowned {staff}"},
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
    ("个人评测数据范围 / 快照", "fixture"),
    ("个人文章发布数据源", "publish_source"),
    ("个人记忆资源 / actor", "memory"),
    ("记录查看入口与权限", "records"),
    ("基线版本、批次与日期", "baseline"),
)


def render_card(values: dict[str, str]) -> str:
    """The resource card of instructor.md §二 as a Markdown table with real values."""
    lines = ["| 字段 | 开课前实际值 |", "| --- | --- |"]
    for label, key in CARD_ROWS:
        lines.append(f"| {label} | {values.get(key, '________')} |")
    return "\n".join(lines)
