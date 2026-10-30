"""samples/itsm-cn per-student kit (manual 1.3): pure helpers against the shipped assets.

The adapted datasets and Agent specs are checked with the backend's own request schemas,
so what ``student_kit.py`` would POST is exactly what the routes accept.
"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SAMPLE = REPO / "samples/itsm-cn"
SCRIPTS = SAMPLE / "scripts"


def module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


@pytest.fixture(scope="module")
def lib():
    return module(SCRIPTS / "student_kit_lib.py")


@pytest.fixture(scope="module")
def shared():
    return {
        name: json.loads((SAMPLE / "lab-assets" / f"{name}.json").read_text())
        for name in ("itsm-exam-8-cn", "itsm-held-out-cn")
    }


@pytest.fixture(scope="module")
def prompts():
    return json.loads((SAMPLE / "lab-assets/prompts.json").read_text())


def test_names_follow_the_manual(lib):
    names = lib.resource_names("lab03")
    assert names["staff"] == "itdesk03"
    assert names["console_agent"] == "lab03-itdesk-console-cn"
    assert names["eval_agent"] == "lab03-itdesk-eval-cn"
    assert names["kb"] == "anypharma-it-helpdesk-cn-lab03"
    assert names["exam_dataset"] == "itsm-exam-8-cn-lab03"
    assert names["held_out_dataset"] == "itsm-held-out-cn-lab03"
    assert (names["ritm"], names["inc"]) == ("RITM0010003", "INC0020003")
    assert lib.tickets_for("lab10") == {"ritm": "RITM0010010", "inc": "INC0020010"}
    assert lib.resource_names("lab07", "desk-seven")["staff"] == "desk-seven"
    for bad in ("lab00", "lab11", "itdesk03", "lab3"):
        with pytest.raises(ValueError):
            lib.resource_names(bad)
    with pytest.raises(ValueError):
        lib.resource_names("lab03", "Bad Name")


def test_adapt_dataset_rewrites_identity_and_tickets(lib, shared):
    exam = shared["itsm-exam-8-cn"]["items"]
    before = json.dumps(exam, ensure_ascii=False)
    adapted = lib.adapt_dataset(exam, "lab03")
    assert json.dumps(exam, ensure_ascii=False) == before, "input must not be mutated"
    assert len(adapted) == 8
    assert [i["scenario_id"] for i in adapted] == [i["scenario_id"] for i in exam]
    assert [i["split"] for i in adapted] == [i["split"] for i in exam]
    text = json.dumps(adapted, ensure_ascii=False)
    for token in lib.SHARED_TOKENS:
        assert token not in text
    by_id = {i["scenario_id"]: i for i in adapted}
    assert by_id["T01"]["turns"][0]["input"].startswith("我是lab03")
    assert "RITM0010003" in by_id["T01"]["turns"][0]["expected_response"]
    assert "INC0020003" in by_id["T02"]["turns"][0]["expected_response"]
    assert "INC0020003" in by_id["T03"]["turns"][0]["input"]
    # untouched knowledge questions keep their expectations
    assert by_id["H01"] == exam[0]
    # adapting an adapted copy changes nothing (re-runs are idempotent)
    assert lib.adapt_dataset(adapted, "lab03") == adapted


def test_read_only_items_expect_no_write(lib, shared):
    adapted = {i["scenario_id"]: i
               for i in lib.adapt_dataset(shared["itsm-exam-8-cn"]["items"], "lab05")}
    assert set(lib.READ_ONLY_ITEMS) == {"H02", "T01", "T02", "T03"}
    for sid in lib.READ_ONLY_ITEMS:
        item = adapted[sid]
        expected = item["turns"][-1]["expected_response"]
        # a 建单 / 转派 may only appear negated
        bare = expected.replace("不代为建单或转派", "").replace("不实际建单或转派", "")
        assert "建单" not in bare and "转派" not in bare, (sid, expected)
        assert any(k in expected for k in ("只读", "不修改", "不执行写操作", "不代为")), sid
        assert any(("不" in a) for a in item["assertions"]), sid
    # T-items keep the student's own tickets coherent with the manual's table
    assert "RITM0010005" in adapted["T01"]["turns"][0]["expected_response"]
    assert "INC0020005" in adapted["T02"]["assertions"][0]


def test_dataset_documents_are_valid_create_bodies(lib, shared):
    from app.evaluation.routers import DatasetCreate, _validate_items

    for name, document in shared.items():
        body = lib.adapt_dataset_document(document, "lab03")
        assert body["name"] == f"{name}-lab03"
        assert "lab03" in body["description"]
        req = DatasetCreate(**body)
        _validate_items(req.items)
        assert len(req.items) == len(document["items"])
    held = lib.adapt_dataset_document(shared["itsm-held-out-cn"], "lab03")
    assert len(held["items"]) == 2 and {i["split"] for i in held["items"]} == {"held_out"}


def test_prompts(lib, prompts):
    staff = lib.adapt_staff_prompt(prompts["staff"], "itdesk03")
    expected = prompts["staff"].count("{{staff_account}}") + prompts["staff"].count("itdesk01")
    assert "itdesk01" not in staff and "{{staff_account}}" not in staff
    assert expected > 0 and staff.count("itdesk03") == expected
    prompt = lib.eval_prompt(prompts)
    assert prompt.startswith(prompts["step1"]) and prompts["step2"] in prompt
    assert "不要创建工单" in prompt and "step3" not in prompt


GATEWAY = {"id": "gw-123", "targets": {
    "servicenow": {"id": "t1", "record_id": "rec-sn"},
    "servicenow-knowledge": {"id": "t2", "record_id": "rec-kb"},
}}
SKILLS = {
    "access-request-coach": {"path": "skills/access-request-coach"},
    "ticket-triage": {"path": "skills/ticket-triage"},
    "crm-sync-troubleshooting": {"path": "skills/crm-sync-troubleshooting"},
    "escalation-handoff": {"path": "skills/escalation-handoff"},
    "kb-article-from-ticket": {"path": "skills/kb-article-from-ticket"},
}


@pytest.mark.parametrize("kind", ["eval", "console"])
def test_agent_specs_are_valid_requests_with_the_right_tool_scope(lib, prompts, kind):
    from app.harness_tool_access import gateway_target_selectors
    from app.schemas.agent import AgentSpecRequest

    names = lib.resource_names("lab03")
    spec = lib.build_agent_spec(
        kind, names, model_id="global.zai.glm-5.3", kb_id="KB1LAB03", gateway=GATEWAY,
        skills=SKILLS, prompts=prompts,
    )
    parsed = AgentSpecRequest(**spec)  # the body POST /api/agents accepts
    assert parsed.memory.short_term is False and parsed.memory.long_term is False
    assert [k.kb_id for k in parsed.knowledge_bases] == ["KB1LAB03"]
    refs = [t.model_dump() for t in parsed.tools]
    if kind == "eval":
        assert parsed.name == "lab03-itdesk-eval-cn"
        assert [r["name"] for r in refs] == ["servicenow"]
        assert refs[0]["config"]["tools"] == ["servicenow___get_ticket",
                                              "servicenow___search_tickets"]
        assert gateway_target_selectors("launchpad_gw", refs, shared=True) == [
            "@launchpad_gw/servicenow___get_ticket", "@launchpad_gw/servicenow___search_tickets",
        ]
        assert "skills/kb-article-from-ticket" not in parsed.skills and len(parsed.skills) == 4
        assert "itdesk" not in parsed.system_prompt
    else:
        assert parsed.name == "lab03-itdesk-console-cn"
        assert [r["name"] for r in refs] == ["servicenow", "servicenow-knowledge"]
        assert all("tools" not in r["config"] for r in refs)
        assert gateway_target_selectors("launchpad_gw", refs, shared=True) == [
            "@launchpad_gw/servicenow___*", "@launchpad_gw/servicenow-knowledge___*",
        ]
        assert len(parsed.skills) == 5
        assert "itdesk03" in parsed.system_prompt and "itdesk01" not in parsed.system_prompt


def test_plan_and_card(lib):
    names = lib.resource_names("lab03")
    steps = lib.plan_steps(names, workspace="default", prefix="/launchpad/itsm-workshop/")
    calls = [s["call"] for s in steps]
    assert all(set(s) == {"as", "call", "why"} for s in steps)
    # order: account → KB → datasets (student) → eval Agent (student) → console Agent (staff)
    assert calls.index("POST /api/auth/register") < calls.index("POST /api/knowledge-bases")
    assert calls.index("POST /api/knowledge-bases") < calls.index("POST /api/eval/datasets")
    posts = [s for s in steps if s["call"] == "POST /api/agents"]
    assert [s["as"] for s in posts] == ["lab03", "itdesk03"]
    assert "password" not in json.dumps(steps).lower()
    card = lib.render_card({**names, "fixture": "x"})
    assert card.splitlines()[0] == "| 字段 | 开课前实际值 |"
    assert "| 服务台 Agent | lab03-itdesk-console-cn |" in card
    assert "| 员工审批单 | RITM0010003 |" in card
    assert "| 个人文章发布数据源 | ________ |" in card  # unknown rows stay blank


def test_dry_run_prints_plan_without_config_or_network():
    env = {k: v for k, v in os.environ.items() if k != "LAUNCHPAD_LAB_CONFIG"}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, str(SCRIPTS / "student_kit.py"), "lab03", "--dry-run"],
        capture_output=True, text=True, env=env, check=True, timeout=60,
    ).stdout
    assert "nothing is called" in out
    assert "POST /api/agents" in out and "itdesk03" in out
    assert "| 评测 Agent | lab03-itdesk-eval-cn |" in out
    assert "servicenow___get_ticket, servicenow___search_tickets" in out
    assert "It9!" not in out
