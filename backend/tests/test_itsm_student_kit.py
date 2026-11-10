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
import types
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
    assert "adopt itdesk03" in steps[1]["why"] and "refuse" in steps[1]["why"]
    card = lib.render_card({**names, "fixture": "x", "gateway": ""})
    assert card.splitlines()[0] == "| 字段 | 开课前实际值 |"
    assert "| 服务台 Agent | lab03-itdesk-console-cn |" in card
    assert "| 员工审批单 | RITM0010003 |" in card
    assert "| 个人文章发布数据源 | ________ |" in card  # unknown rows stay blank
    assert "| 工单网关 | ________ |" in card  # ... and so do empty ones
    assert "| 他人工单（只读测试） | ________ |" in card  # raw names carry no other_ticket
    assert lib.other_ticket("lab03") == "RITM0010001" and lib.other_ticket("lab01") == "RITM0010002"
    # manual 1.9 (PRACTICE-S1 §3-16): 基线版本、批次与日期 split into one row per fact
    labels = [label for label, _ in lib.CARD_ROWS]
    assert "基线版本、批次与日期" not in labels
    assert labels[-5:] == ["模型", "控制台地址", "批次时间（UTC）", "工单网关", "证书"]
    assert labels[2] == "员工 Agent" and len(labels) == len(set(labels))


def test_certificate_note_depends_on_an_ip_literal_console_host(lib):
    for url in ("https://198.51.100.44", "http://10.0.0.8:8443/", "https://[2400:cb00::1]/"):
        assert lib.certificate_note(url) == lib.SELF_SIGNED_NOTE, url
    for url in ("https://console.example.org", "https://lab.itsm.example.cn:8443", ""):
        assert lib.certificate_note(url) == lib.ORGANIZER_FILLS, url
    assert "高级" in lib.SELF_SIGNED_NOTE and "继续访问" in lib.SELF_SIGNED_NOTE


# ----- agents.py stages share the kit's spec builder (N17-obs550) -------------------------

KB = {"kb_id": "KBSHARED1", "name": "anypharma-it-helpdesk-cn"}


def test_staged_spec_keeps_long_term_memory_off_at_every_learner_stage(lib, prompts):
    from app.schemas.agent import AgentSpecRequest

    for stage in (1, 2, 3):
        spec = lib.staged_agent_spec(
            "lab03-itdesk-cn", stage, model_id="global.zai.glm-5.3", prompts=prompts,
            kb=KB, skills=SKILLS, gateway=GATEWAY,
        )
        parsed = AgentSpecRequest(**spec)
        assert spec["memory"] == lib.STAGE1_MEMORY, stage
        assert parsed.memory.short_term is True and parsed.memory.long_term is False, stage
        steps = [prompts[f"step{s}"] for s in range(1, stage + 1)]
        assert parsed.system_prompt == "\n\n".join(steps)
        kb_ids = [k.kb_id for k in parsed.knowledge_bases]
        assert kb_ids == ([] if stage == 1 else ["KBSHARED1"])
        assert len(parsed.skills) == (0 if stage == 1 else 4)
        assert "skills/kb-article-from-ticket" not in parsed.skills
        assert [t.name for t in parsed.tools] == ([] if stage < 3 else ["servicenow"])
    # stage 1 is exactly the reset_student baseline (without the redeploy-only policy_ids)
    stage1 = lib.staged_agent_spec("lab03-itdesk-cn", 1, model_id="m", prompts=prompts)
    baseline = lib.stage1_agent_spec("lab03-itdesk-cn", model_id="m", prompts=prompts)
    assert {**stage1, "policy_ids": []} == baseline
    # stage ≥ 2 without the shared resources is a manifest problem, not a KeyError
    with pytest.raises(ValueError):
        lib.staged_agent_spec("x", 2, model_id="m", prompts=prompts)
    with pytest.raises(ValueError):
        lib.staged_agent_spec("x", 3, model_id="m", prompts=prompts, kb=KB, skills=SKILLS)


def test_staged_spec_for_the_staff_reference_agent(lib, prompts):
    from app.schemas.agent import AgentSpecRequest

    spec = lib.staged_agent_spec(
        "itdesk-console-cn", 3, model_id="global.zai.glm-5.3", prompts=prompts, staff="itdesk01",
        kb=KB, skills=SKILLS, gateway=GATEWAY,
    )
    parsed = AgentSpecRequest(**spec)
    assert spec["memory"] == lib.STAFF_REFERENCE_MEMORY
    assert "itdesk01" in parsed.system_prompt and "{{staff_account}}" not in parsed.system_prompt
    assert len(parsed.skills) == 5
    assert [t.name for t in parsed.tools] == ["servicenow", "servicenow-knowledge"]


class Submitted(Exception):
    """Raised by the stub ``lab.api`` when agents.py submits its POST: carries the body."""


def run_agents_py(tmp_path, monkeypatch, user, stage, *, prompts):
    """Run ``agents.py <user> <stage>`` against a stub ``lab``; return the submitted spec.

    The stub manifest carries the shared KB / Skills / Gateway rows; ``api`` raises on the
    first POST so nothing past the spec (polling, manifest writes) runs."""
    import runpy

    (tmp_path / "lab-assets").mkdir(exist_ok=True)
    (tmp_path / "lab-assets/prompts.json").write_text(json.dumps(prompts, ensure_ascii=False))
    stub = types.ModuleType("lab")
    stub.LEARNERS, stub.STAFF = [f"lab{i:02}" for i in range(1, 11)], ["itdesk01"]
    stub.ROOT = tmp_path
    stub.CONFIG = {"model_id": "global.zai.glm-5.3"}  # = the manifest's: nothing to sync
    manifest = {"model_id": "global.zai.glm-5.3", "agents": {}, "kb": KB, "skills": SKILLS,
                "gateway": GATEWAY}
    stub.load = lambda: json.loads(json.dumps(manifest))
    stub.save = lambda state: pytest.fail("agents.py must not save before submitting")
    stub.evidence = lambda name, obj: pytest.fail("no evidence before submitting")
    stub.session = lambda user=None: object()
    stub.sync_assets = lambda: []  # N17-obs558: the real one mirrors the package

    def api(c, method, path, body=None, **kw):
        if (method, path) == ("GET", "/api/capabilities"):  # the model check (N17-obs557)
            return {"models": [{"model_id": "global.zai.glm-5.3", "tools": True,
                                "available": True}]}
        assert (method, path) == ("POST", "/api/agents"), (method, path)
        raise Submitted(body)

    stub.api = api
    monkeypatch.setitem(sys.modules, "lab", stub)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr(sys, "argv", ["agents.py", user, str(stage)])
    with pytest.raises(Submitted) as info:
        runpy.run_path(str(SCRIPTS / "agents.py"), run_name="__main__")
    return info.value.args[0]


def test_agents_py_stage_1_submits_the_stage1_baseline_memory(tmp_path, monkeypatch, lib, prompts):
    spec = run_agents_py(tmp_path, monkeypatch, "lab03", 1, prompts=prompts)
    assert spec["name"] == "lab03-itdesk-cn"
    assert spec["memory"] == {"short_term": True, "long_term": False, "memory_id": None}
    assert spec == lib.staged_agent_spec("lab03-itdesk-cn", 1, model_id="global.zai.glm-5.3",
                                         prompts=prompts)
    assert spec["tools"] == [] and spec["skills"] == [] and spec["knowledge_bases"] == []


def test_agents_py_stage_3_learner_and_staff(tmp_path, monkeypatch, prompts):
    learner = run_agents_py(tmp_path, monkeypatch, "lab03", 3, prompts=prompts)
    assert learner["memory"]["long_term"] is False and learner["memory"]["short_term"] is True
    assert [t["name"] for t in learner["tools"]] == ["servicenow"]
    assert learner["knowledge_bases"] == [{"kb_id": "KBSHARED1", "name": KB["name"]}]
    assert "skills/kb-article-from-ticket" not in learner["skills"] and len(learner["skills"]) == 4
    staff = run_agents_py(tmp_path, monkeypatch, "itdesk01", 3, prompts=prompts)
    assert staff["name"] == "itdesk-console-cn" and staff["memory"]["long_term"] is True
    assert [t["name"] for t in staff["tools"]] == ["servicenow", "servicenow-knowledge"]
    assert "itdesk01" in staff["system_prompt"] and len(staff["skills"]) == 5


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


# ----- resource cards on disk ---------------------------------------------------------
# ``student_kit.py`` imports ``lab`` lazily; a stub module stands in for it so the card
# paths run against a temp state_dir and every login / API call is recorded, not made.

MANIFEST_ROWS = {
    "lab03": {
        "staff": {"username": "itdesk03", "id": "u-3",
                  "credential_parameter": "/launchpad/itsm-workshop/users/itdesk03"},
        "kb": {"kb_id": "KB1LAB03", "ds_id": "DS1LAB03"},
        "datasets": {}, "agents": {}, "ownership": "created",
        "prepared_at": "2026-11-06T01:02:03Z",
    },
    "lab07": {
        "staff": {"username": "desk-seven", "id": "u-7",
                  "credential_parameter": "/launchpad/itsm-workshop/users/desk-seven"},
        "kb": {"kb_id": "KB1LAB07", "ds_id": "DS1LAB07"},
        "datasets": {}, "agents": {}, "ownership": "created",
        "prepared_at": "2026-11-06T01:05:00Z",
    },
}


def stub_lab(root):
    stub = types.ModuleType("lab")
    stub.ROOT = root
    stub.PUBLIC = "https://console.example.org"
    stub.PREFIX = "/launchpad/itsm-workshop/"
    stub.CONFIG = {"workspace": "default", "model_id": "global.zai.glm-5.3"}
    stub.calls = []
    stub.sync_assets = lambda: []  # N17-obs558: the real one mirrors the package
    manifest = {"model_id": "global.zai.glm-5.3", "workspace": "default",
                "gateway": {"id": "gw-123", "name": "launchpad-itsm-workshop", "targets": {}},
                "users": {}, "students": json.loads(json.dumps(MANIFEST_ROWS))}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False))
    stub.load = lambda: json.loads((root / "manifest.json").read_text())
    stub.save = lambda state: (root / "manifest.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2))
    stub.evidence = lambda name, obj: (root / "evidence").mkdir(exist_ok=True) or (
        root / "evidence" / name).write_text(json.dumps(obj, ensure_ascii=False))

    def session(user=None):
        stub.calls.append(("login", user))
        return object()

    def api(c, method, path, body=None, **kw):
        stub.calls.append((method, path))
        if (method, path) == ("GET", "/api/capabilities"):
            return {"models": list(stub.catalog)}
        raise AssertionError(f"unexpected API call {method} {path}")

    stub.catalog = [{"model_id": "global.zai.glm-5.3", "provider": "bedrock", "tools": True,
                     "available": True},
                    {"model_id": "litellm.gpt-oss-120b", "provider": "litellm", "tools": True,
                     "available": True}]
    stub.session, stub.api = session, api
    return stub


@pytest.fixture
def kit(tmp_path, monkeypatch):
    """``student_kit`` loaded against a stub ``lab`` whose state_dir is ``tmp_path``."""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    stub = stub_lab(tmp_path)
    monkeypatch.setitem(sys.modules, "lab", stub)
    return module(SCRIPTS / "student_kit.py"), stub


def test_kit_run_writes_the_card_file_and_records_card_path(kit, lib, capsys):
    sk, stub = kit
    k = sk.Kit(sk.parse_args(["lab03"]))
    assert stub.calls == [("login", None)]  # the admin login of the live run, nothing else
    k.finish_card()
    path = stub.ROOT / "cards" / "lab03.md"
    assert k.row["card_path"] == "cards/lab03.md" and path.is_file()
    manifest = json.loads((stub.ROOT / "manifest.json").read_text())
    assert manifest["students"]["lab03"]["card_path"] == "cards/lab03.md"
    assert manifest["students"]["lab07"] == MANIFEST_ROWS["lab07"], "other rows untouched"
    expected = lib.render_card(sk.card_values(
        lib.resource_names("lab03"), model_id="global.zai.glm-5.3", kb_ds="DS1LAB03",
        kb_id="KB1LAB03", console_url=stub.PUBLIC, prepared_at="2026-11-06T01:02:03Z",
        gateway_name="gw-123",
    ))
    text = path.read_text(encoding="utf-8")
    assert text == expected + "\n"
    assert "| 服务台 Agent | lab03-itdesk-console-cn |" in text
    assert "anypharma-it-helpdesk-cn-lab03（KB1LAB03）" in text and "DS1LAB03" in text
    # the split rows (manual 1.9): model / console / batch time / gateway / certificate
    assert "| 模型 | global.zai.glm-5.3 |" in text
    assert f"| 控制台地址 | {stub.PUBLIC} |" in text
    assert "| 批次时间（UTC） | 2026-11-06T01:02:03Z |" in text
    assert "| 工单网关 | gw-123 |" in text  # the console-visible ID, not the bare name
    assert f"| 证书 | {lib.ORGANIZER_FILLS} |" in text  # a hostname, not an IP literal
    assert "基线版本" not in text
    out = capsys.readouterr().out
    assert expected in out and str(path) in out
    # the card and the console output name the SSM parameters only, never a password
    assert "It9!" not in text and "password" not in text.lower()
    assert "/launchpad/itsm-workshop/users/itdesk03" in out
    assert stub.calls == [("login", None)]


def test_cards_only_rewrites_every_card_from_the_manifest_without_api_calls(kit, lib):
    sk, stub = kit
    stale = stub.ROOT / "cards" / "lab03.md"
    stale.parent.mkdir()
    stale.write_text("stale\n", encoding="utf-8")
    assert sk.main(["--cards-only"]) == 0
    assert stub.calls == [], "manifest-only: no login, no API call"
    files = sorted(p.name for p in (stub.ROOT / "cards").iterdir())
    assert files == ["lab03.md", "lab07.md"]
    manifest = json.loads((stub.ROOT / "manifest.json").read_text())
    for student, staff in (("lab03", "itdesk03"), ("lab07", "desk-seven")):
        text = (stub.ROOT / "cards" / f"{student}.md").read_text(encoding="utf-8")
        row = MANIFEST_ROWS[student]
        expected = lib.render_card(sk.card_values(
            lib.resource_names(student, staff), model_id="global.zai.glm-5.3",
            kb_ds=row["kb"]["ds_id"], kb_id=row["kb"]["kb_id"], console_url=stub.PUBLIC,
            prepared_at=row["prepared_at"], gateway_name="gw-123",
        ))
        assert text == expected + "\n"
        assert f"| 独立服务台账号 | {staff} |" in text
        assert manifest["students"][student]["card_path"] == f"cards/{student}.md"
    assert "stale" not in (stub.ROOT / "cards" / "lab03.md").read_text(encoding="utf-8")


def test_cards_only_for_one_student_and_unknown_student(kit):
    sk, stub = kit
    assert sk.main(["--cards-only", "lab07"]) == 0
    assert [p.name for p in (stub.ROOT / "cards").iterdir()] == ["lab07.md"]
    assert stub.calls == []
    with pytest.raises(RuntimeError, match="lab05 is not recorded"):
        sk.main(["--cards-only", "lab05"])


def test_cli_requires_a_student_unless_cards_only(kit):
    sk, _ = kit
    assert sk.parse_args(["--cards-only"]).student is None
    assert sk.parse_args(["lab03", "--skip-refresh-callers"]).skip_refresh_callers
    for argv in ([], ["--cards-only", "--dry-run"], ["lab11"]):
        with pytest.raises(SystemExit):
            sk.parse_args(argv)


def test_card_marks_the_self_signed_certificate_for_an_ip_console(kit, lib):
    sk, stub = kit
    stub.PUBLIC = "https://198.51.100.44"
    assert sk.main(["--cards-only", "lab03"]) == 0
    text = (stub.ROOT / "cards" / "lab03.md").read_text(encoding="utf-8")
    assert "| 控制台地址 | https://198.51.100.44 |" in text
    assert f"| 证书 | {lib.SELF_SIGNED_NOTE} |" in text


def test_employee_agent_row_follows_the_lab_config_prebuilt_flag(kit, lib):
    """Manual 1.12: the 员工 Agent row says 已预建，直接打开 or 未预建，场景一 3.1.1 由学员创建 —
    selected by ``employee_agent_prebuilt`` (missing = true), never edited by hand."""
    sk, stub = kit
    names = lib.resource_names("lab03")
    prebuilt = "lab03-itdesk-cn（已预建，直接打开）"
    student_made = "lab03-itdesk-cn（未预建，场景一 3.1.1 由学员创建）"
    row = lambda **kw: sk.card_values(names, model_id="m", **kw)["employee_agent"]  # noqa: E731
    assert row() == prebuilt  # default
    assert row(employee_prebuilt=True) == prebuilt
    assert row(employee_prebuilt=False) == student_made
    assert lib.employee_agent_prebuilt({}) is True
    assert lib.employee_agent_prebuilt({"employee_agent_prebuilt": False}) is False
    # --cards-only reads the key from the lab config: absent → prebuilt, false → student-made
    assert "employee_agent_prebuilt" not in stub.CONFIG
    assert sk.main(["--cards-only", "lab03"]) == 0
    text = (stub.ROOT / "cards" / "lab03.md").read_text(encoding="utf-8")
    assert f"| 员工 Agent | {prebuilt} |" in text and "agents.py 预建" not in text
    stub.CONFIG["employee_agent_prebuilt"] = False
    assert sk.main(["--cards-only", "lab03"]) == 0
    text = (stub.ROOT / "cards" / "lab03.md").read_text(encoding="utf-8")
    assert f"| 员工 Agent | {student_made} |" in text and prebuilt not in text
    stub.CONFIG["employee_agent_prebuilt"] = True
    assert sk.main(["--cards-only", "lab03"]) == 0
    assert f"| 员工 Agent | {prebuilt} |" in (stub.ROOT / "cards" / "lab03.md").read_text(
        encoding="utf-8")
    assert stub.calls == [], "manifest-only: no login, no API call"


# ----- ensure_staff: adopt the workshop stage's desk account, refuse unknown ones (obs549)

def live_users(*usernames):
    return {"items": [{"id": f"u-{u}", "username": u, "status": "active"} for u in usernames]}


def kit_for_lab05(kit, *, users_row=None, live=("lab05", "itdesk05"), staff_accounts=None):
    """A ``Kit`` for lab05 (no manifest row yet) whose stub API serves only the users list."""
    sk, stub = kit
    manifest = stub.load()
    manifest["users"] = {"itdesk05": users_row} if users_row else {}
    stub.save(manifest)
    stub.CONFIG["staff_accounts"] = staff_accounts or ["itdesk01", "itdesk05"]
    stub.PREFIX = "/launchpad/itsm-workshop/"

    def api(c, method, path, body=None, **kw):
        stub.calls.append((method, path))
        if (method, path) == ("GET", "/api/users?status=all&limit=200"):
            return live_users(*live)
        raise AssertionError(f"unexpected API call {method} {path}")

    stub.api = api
    stub.SSM = None  # any SSM / register attempt would fail loudly
    return sk.Kit(sk.parse_args(["lab05"])), stub


WORKSHOP_ROW = {"id": "u-itdesk05", "role": "member", "ownership": "created",
                "credential_parameter": "/launchpad/itsm-workshop/users/itdesk05"}


def test_ensure_staff_adopts_the_desk_account_workshop_py_created(kit, capsys):
    k, stub = kit_for_lab05(kit, users_row=WORKSHOP_ROW)
    k.ensure_staff()
    users_get = ("GET", "/api/users?status=all&limit=200")
    assert [c for c in stub.calls if c[0] != "login"] == [users_get]
    row = json.loads((stub.ROOT / "manifest.json").read_text())["students"]["lab05"]["staff"]
    assert row["username"] == "itdesk05" and row["id"] == "u-itdesk05"
    assert row["ownership"] == "adopted" and row["adopted_from"] == "users"
    assert row["credential_parameter"] == "/launchpad/itsm-workshop/users/itdesk05"
    assert row["permissions"] is None  # the workshop-stage grant is left alone
    assert "adopted" in capsys.readouterr().out
    # idempotent: the recorded row short-circuits the next run before any user lookup
    stub.calls.clear()
    k.ensure_staff()
    assert [c for c in stub.calls if c[0] != "login"] == [users_get]
    manifest = json.loads((stub.ROOT / "manifest.json").read_text())
    assert manifest["students"]["lab05"]["staff"] == row


@pytest.mark.parametrize("users_row", [
    None,                                              # nobody recorded it: truly unknown
    {**WORKSHOP_ROW, "id": "u-someone-else"},          # a recorded row of another account
    {**WORKSHOP_ROW, "ownership": "referenced"},       # not created by the workshop stage
])
def test_ensure_staff_still_refuses_an_unrecorded_existing_desk_account(kit, users_row):
    k, stub = kit_for_lab05(kit, users_row=users_row)
    with pytest.raises(RuntimeError, match="Unowned existing user: itdesk05"):
        k.ensure_staff()
    assert "lab05" not in json.loads((stub.ROOT / "manifest.json").read_text())["students"]


def test_ensure_staff_requires_the_learner_account(kit):
    k, _ = kit_for_lab05(kit, users_row=WORKSHOP_ROW, live=("itdesk05",))
    with pytest.raises(RuntimeError, match="lab05 does not exist"):
        k.ensure_staff()


# ----- agents.py and the model switch (N17-obs557) -------------------------------------
def run_agents_switch(tmp_path, monkeypatch, argv, *, prompts, config_model, catalog_ids):
    """``agents.py`` against a stub lab whose config model may differ from the manifest's;
    records manifest saves and API calls; returns ``(submitted spec | None, stub)``."""
    import runpy

    (tmp_path / "lab-assets").mkdir(exist_ok=True)
    (tmp_path / "lab-assets/prompts.json").write_text(json.dumps(prompts, ensure_ascii=False))
    stub = types.ModuleType("lab")
    stub.LEARNERS, stub.STAFF = [f"lab{i:02}" for i in range(1, 11)], ["itdesk01"]
    stub.ROOT = tmp_path
    stub.CONFIG = {"model_id": config_model}
    manifest = {"model_id": "global.zai.glm-5.3", "agents": {}, "kb": KB, "skills": SKILLS,
                "gateway": GATEWAY}
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    stub.load = lambda: json.loads((tmp_path / "manifest.json").read_text())
    stub.saves, stub.calls = [], []

    def save(state):
        stub.saves.append(json.loads(json.dumps(state)))
        (tmp_path / "manifest.json").write_text(json.dumps(state))

    stub.save = save
    stub.evidence = lambda name, obj: pytest.fail("no evidence before submitting")
    stub.session = lambda user=None: object()
    stub.sync_assets = lambda: []  # N17-obs558: the real one mirrors the package

    def api(c, method, path, body=None, **kw):
        stub.calls.append((method, path))
        if (method, path) == ("GET", "/api/capabilities"):
            return {"models": [{"model_id": m, "tools": True, "available": True}
                               for m in catalog_ids]}
        assert (method, path) == ("POST", "/api/agents"), (method, path)
        raise Submitted(body)

    stub.api = api
    monkeypatch.setitem(sys.modules, "lab", stub)
    monkeypatch.syspath_prepend(str(SCRIPTS))
    monkeypatch.setattr(sys, "argv", ["agents.py", *argv])
    try:
        runpy.run_path(str(SCRIPTS / "agents.py"), run_name="__main__")
    except Submitted as submitted:
        return submitted.args[0], stub
    return None, stub


def test_agents_py_follows_the_config_model_and_syncs_the_manifest(tmp_path, monkeypatch,
                                                                   prompts, capsys):
    spec, stub = run_agents_switch(
        tmp_path, monkeypatch, ["lab03", "1"], prompts=prompts,
        config_model="litellm.gpt-oss-120b",
        catalog_ids=["global.zai.glm-5.3", "litellm.gpt-oss-120b"])
    assert spec["model_id"] == "litellm.gpt-oss-120b"
    assert stub.calls[0] == ("GET", "/api/capabilities")
    assert [s["model_id"] for s in stub.saves] == ["litellm.gpt-oss-120b"]
    assert stub.saves[0]["kb"] == KB, "merge: the other manifest keys survive"
    out = capsys.readouterr().out
    assert "model_id: global.zai.glm-5.3 -> litellm.gpt-oss-120b (from config)" in out


def test_agents_py_model_override_does_not_persist(tmp_path, monkeypatch, prompts, capsys):
    spec, stub = run_agents_switch(
        tmp_path, monkeypatch, ["lab03", "1", "--model", "litellm.gpt-oss-120b"],
        prompts=prompts, config_model="global.zai.glm-5.3",
        catalog_ids=["global.zai.glm-5.3", "litellm.gpt-oss-120b"])
    assert spec["model_id"] == "litellm.gpt-oss-120b"
    assert stub.saves == [] and "model_id:" not in capsys.readouterr().out
    assert stub.load()["model_id"] == "global.zai.glm-5.3"


def test_agents_py_refuses_a_model_the_console_does_not_offer(tmp_path, monkeypatch, prompts):
    with pytest.raises(RuntimeError, match="not in this console's model catalog") as info:
        run_agents_switch(tmp_path, monkeypatch, ["lab03", "1"], prompts=prompts,
                          config_model="litellm.gpt-oss-20b", catalog_ids=["global.zai.glm-5.3"])
    assert "global.zai.glm-5.3" in str(info.value)
    assert getattr(info.value, "code", None) == "model.not_offered"
