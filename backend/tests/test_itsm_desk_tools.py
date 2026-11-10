"""samples/itsm-cn service-desk Agent tool naming (N17-obs543) and ``refresh_targets.py``.

The Harness exposes a lab Gateway tool as ``launchpad_gw_<target>___<tool>``; gpt-oss kept
calling the knowledge tools under the ``servicenow`` prefix (Unknown tool). The staff prompt
and every ``tools.json`` description now spell the exact full names out, and
``refresh_targets.py`` pushes such description changes to the live targets that
``deploy_tools.py`` skips. Hermetic: a stub Gateway client and a stub ``lab`` module.
"""
from __future__ import annotations

import copy
import importlib.util
import json
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
    sys.path.insert(0, str(SCRIPTS))
    return module(SCRIPTS / "student_kit_lib.py")


@pytest.fixture(scope="module")
def refresh():
    return module(SCRIPTS / "refresh_targets.py")


@pytest.fixture(scope="module")
def prompts():
    return json.loads((SAMPLE / "lab-assets/prompts.json").read_text())


@pytest.fixture(scope="module")
def schemas():
    return json.loads((SAMPLE / "lab-assets/tools.json").read_text())


KNOWLEDGE_TOOLS = ("search_resolved_tickets", "draft_article", "get_article", "publish_article")
TICKET_TOOLS = ("search_tickets", "get_ticket", "create_incident", "add_comment", "escalate",
                "resolve_ticket")


def full_names(lib, schemas):
    return {
        target: lib.harness_tool_names(target, [t["name"] for t in tools])
        for target, tools in schemas.items()
    }


# ----- A. the names the model must call --------------------------------------------------


def test_tool_names_and_targets_are_unchanged(schemas):
    """Cedar (`itsm_requester_is_self`) and the kit's ToolRefs name these; only descriptions
    changed."""
    assert list(schemas) == ["servicenow", "servicenow-knowledge"]
    assert tuple(t["name"] for t in schemas["servicenow"]) == TICKET_TOOLS
    assert tuple(t["name"] for t in schemas["servicenow-knowledge"]) == KNOWLEDGE_TOOLS


def test_harness_tool_names_follow_the_runtime_alias(lib):
    assert lib.HARNESS_GATEWAY_ALIAS == "launchpad_gw"
    assert lib.harness_tool_names("servicenow-knowledge", ["draft_article"]) == [
        "launchpad_gw_servicenow-knowledge___draft_article"]
    # the Gateway-side name the ToolRef / Cedar carry stays prefix-free
    assert lib.gateway_tool_names("servicenow", ["get_ticket"]) == ["servicenow___get_ticket"]


def test_every_tool_description_starts_with_its_exact_call_name(lib, schemas):
    for target, tools in schemas.items():
        for tool in tools:
            full = f"launchpad_gw_{target}___{tool['name']}"
            assert tool["description"].startswith(f"调用名 {full}。"), (target, tool["name"])
            assert full in lib.harness_tool_names(target, [tool["name"]])
            # the rest of the description is still there (not replaced)
            assert len(tool["description"]) > len(f"调用名 {full}。") + 5


def test_staff_prompt_lists_every_exact_tool_name_per_target(lib, prompts, schemas):
    staff = prompts["staff"]
    for names in full_names(lib, schemas).values():
        for name in names:
            assert name in staff, name
    assert "servicenow-knowledge 目标下" in staff and "servicenow 目标下" in staff
    # the two names gpt-oss invented are named as NOT existing
    assert "不存在 launchpad_gw_servicenow___search_resolved_tickets" in staff
    assert "launchpad_gw_servicenow___draft_article" in staff
    # the invented hybrid never appears
    assert "servicenow___knowledge___" not in staff
    # the placeholder survives (adapt_staff_prompt still substitutes it)
    assert "{{staff_account}}" in staff and "itdesk01" not in staff
    # the learner prompts carry none of the knowledge names (the learner has no such tool)
    for key in ("step1", "step2", "step3"):
        assert "launchpad_gw_" not in prompts[key]


def test_console_specs_carry_the_names_and_learner_specs_do_not(lib, prompts, schemas):
    names = full_names(lib, schemas)
    gateway = {"id": "gw-1", "targets": {
        "servicenow": {"id": "t1", "record_id": "rec-sn"},
        "servicenow-knowledge": {"id": "t2", "record_id": "rec-kn"}}}
    skills = {n: {"path": f"skills/{n}"} for n in
              ("ticket-triage", "access-request-coach", "crm-sync-troubleshooting",
               "escalation-handoff", "kb-article-from-ticket")}
    kit_console = lib.build_agent_spec(
        "console", lib.resource_names("lab03"), model_id="m", kb_id="kb-1",
        gateway=gateway, skills=skills, prompts=prompts)
    reference = lib.staged_agent_spec(
        "itdesk-console-cn", 3, model_id="m", prompts=prompts, staff="itdesk01",
        kb={"kb_id": "kb-1", "name": "kb"}, skills=skills, gateway=gateway)
    for spec, account in ((kit_console, "itdesk03"), (reference, "itdesk01")):
        prompt = spec["system_prompt"]
        assert account in prompt and "{{staff_account}}" not in prompt
        for name in names["servicenow"] + names["servicenow-knowledge"]:
            assert name in prompt, name
        assert [t["name"] for t in spec["tools"]] == ["servicenow", "servicenow-knowledge"]
    # the learner's eval / stage Agents keep their prompts free of the knowledge names
    kit_eval = lib.build_agent_spec(
        "eval", lib.resource_names("lab03"), model_id="m", kb_id="kb-1",
        gateway=gateway, skills=skills, prompts=prompts)
    learner = lib.staged_agent_spec(
        "lab03-itdesk-cn", 3, model_id="m", prompts=prompts,
        kb={"kb_id": "kb-1", "name": "kb"}, skills=skills, gateway=gateway)
    for spec in (kit_eval, learner):
        assert "servicenow-knowledge" not in spec["system_prompt"]
        assert "launchpad_gw_" not in spec["system_prompt"]


# ----- B. refresh_targets.py ---------------------------------------------------------------


class StubGateway:
    """``get_gateway_target`` / ``update_gateway_target`` over an in-memory target table; an
    update parks the target in UPDATING for ``updating_polls`` reads."""

    def __init__(self, targets: dict[str, dict], updating_polls: int = 2):
        self.targets = targets
        self.updating_polls = updating_polls
        self.updates: list[dict] = []
        self.reads = 0
        self._pending: dict[str, int] = {}

    def get_gateway_target(self, *, gatewayIdentifier, targetId):
        assert gatewayIdentifier == "gw-1"
        self.reads += 1
        row = copy.deepcopy(self.targets[targetId])
        left = self._pending.get(targetId, 0)
        if left:
            self._pending[targetId] = left - 1
            row["status"] = "UPDATING"
        return row

    def update_gateway_target(self, **body):
        self.updates.append(copy.deepcopy(body))
        row = self.targets[body["targetId"]]
        row["targetConfiguration"] = body["targetConfiguration"]
        row["updatedAt"] = "2026-11-09T02:00:00+00:00"
        self._pending[body["targetId"]] = self.updating_polls
        return {"targetId": body["targetId"], "status": "UPDATING"}


def live_target(target_id, name, tools, *, inline=True):
    schema = {"inlinePayload": copy.deepcopy(tools)} if inline else {"s3": {"uri": "s3://b/k"}}
    return {
        "targetId": target_id, "name": name, "status": "READY",
        "description": "Synthetic ITSM workshop " + name,
        "targetConfiguration": {"mcp": {"lambda": {
            "lambdaArn": "arn:aws-cn:lambda:cn-northwest-1:1:function:launchpad-itsm",
            "toolSchema": schema}}},
        "credentialProviderConfigurations": [{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
    }


def old_schemas(schemas):
    """What a Gateway deployed before this change still serves: descriptions without the
    ``调用名 …。`` prefix."""
    old = copy.deepcopy(schemas)
    for tools in old.values():
        for t in tools:
            t["description"] = t["description"].split("。", 1)[1]
    return old


@pytest.fixture
def manifest():
    return {"gateway": {"id": "gw-1", "targets": {
        "servicenow": {"id": "t-sn", "name": "servicenow", "record_id": "rec-sn"},
        "servicenow-knowledge": {"id": "t-kn", "name": "servicenow-knowledge",
                                 "record_id": "rec-kn"}}}}


def test_parse_args_defaults_to_dry_run(refresh):
    assert refresh.parse_args([]).dry_run is True and refresh.parse_args([]).apply is False
    args = refresh.parse_args(["--apply"])
    assert args.apply is True and args.dry_run is False
    with pytest.raises(SystemExit):
        refresh.parse_args(["--apply", "--dry-run"])


def test_tool_diff_names_what_changed(refresh, schemas):
    local = schemas["servicenow-knowledge"]
    old = old_schemas(schemas)["servicenow-knowledge"]
    assert refresh.tool_diff(local, copy.deepcopy(local)) == []
    assert refresh.tool_diff(old, local) == [f"{t['name']}: description" for t in local]
    changed = copy.deepcopy(local)
    changed[0]["inputSchema"]["properties"]["keywords"]["description"] = "x"
    del changed[1]
    changed.append({"name": "new_tool", "description": "d", "inputSchema": {"type": "object"}})
    assert refresh.tool_diff(local, changed) == [
        "search_resolved_tickets: inputSchema", "new_tool: added", "draft_article: removed"]
    # same tools, different order → reported, not silently equal
    assert refresh.tool_diff(local, list(reversed(local))) == ["(tool order)"]


def test_dry_run_reports_per_target_and_writes_nothing(refresh, schemas, manifest):
    old = old_schemas(schemas)
    gw = StubGateway({
        "t-sn": live_target("t-sn", "servicenow", schemas["servicenow"]),       # already current
        "t-kn": live_target("t-kn", "servicenow-knowledge", old["servicenow-knowledge"]),
    })
    lines: list[str] = []
    results = refresh.refresh(gw, manifest, schemas, apply=False, log=lines.append)
    by = {r["target"]: r for r in results}
    assert by["servicenow"]["action"] == "UNCHANGED" and by["servicenow"]["changes"] == []
    assert by["servicenow-knowledge"]["action"] == "WOULD UPDATE"
    assert by["servicenow-knowledge"]["changes"] == [
        f"{n}: description" for n in KNOWLEDGE_TOOLS]
    assert gw.updates == []
    assert any("WOULD UPDATE" in line and "draft_article: description" in line for line in lines)


def test_apply_updates_only_the_differing_target_and_is_idempotent(refresh, schemas, manifest):
    old = old_schemas(schemas)
    gw = StubGateway({
        "t-sn": live_target("t-sn", "servicenow", schemas["servicenow"]),
        "t-kn": live_target("t-kn", "servicenow-knowledge", old["servicenow-knowledge"]),
    })
    sleeps: list[float] = []
    results = refresh.refresh(gw, manifest, schemas, apply=True, log=lambda s: None,
                              sleep=sleeps.append)
    by = {r["target"]: r for r in results}
    assert by["servicenow"]["action"] == "UNCHANGED"
    assert by["servicenow-knowledge"]["action"] == "UPDATED"
    assert by["servicenow-knowledge"]["updated_at"] == "2026-11-09T02:00:00+00:00"
    assert len(gw.updates) == 1
    body = gw.updates[0]
    assert body["gatewayIdentifier"] == "gw-1" and body["targetId"] == "t-kn"
    assert body["name"] == "servicenow-knowledge"
    assert body["description"] == "Synthetic ITSM workshop servicenow-knowledge"
    assert body["credentialProviderConfigurations"] == [
        {"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    lam = body["targetConfiguration"]["mcp"]["lambda"]
    assert lam["lambdaArn"].endswith(":function:launchpad-itsm")
    assert lam["toolSchema"] == {"inlinePayload": schemas["servicenow-knowledge"]}
    assert lam["toolSchema"]["inlinePayload"][1]["description"].startswith(
        "调用名 launchpad_gw_servicenow-knowledge___draft_article。")
    # waited through the UPDATING polls, then READY
    assert len(sleeps) == 2
    # second run: nothing left to do
    gw.updates.clear()
    again = refresh.refresh(gw, manifest, schemas, apply=True, log=lambda s: None,
                            sleep=sleeps.append)
    assert {r["action"] for r in again} == {"UNCHANGED"} and gw.updates == []


def test_apply_raises_when_the_target_never_becomes_ready(refresh, schemas, manifest):
    old = old_schemas(schemas)
    gw = StubGateway({
        "t-sn": live_target("t-sn", "servicenow", schemas["servicenow"]),
        "t-kn": live_target("t-kn", "servicenow-knowledge", old["servicenow-knowledge"]),
    }, updating_polls=10**6)
    with pytest.raises(RuntimeError, match="not READY"):
        refresh.refresh(gw, manifest, schemas, apply=True, log=lambda s: None,
                        sleep=lambda s: None)
    assert len(gw.updates) == 1


def test_unknown_not_inline_and_undeployed_targets_are_reported_not_touched(
        refresh, schemas, manifest):
    manifest["gateway"]["targets"]["legacy"] = {"id": "t-old", "name": "legacy"}
    del manifest["gateway"]["targets"]["servicenow-knowledge"]
    gw = StubGateway({
        "t-sn": live_target("t-sn", "servicenow", schemas["servicenow"], inline=False),
        "t-old": live_target("t-old", "legacy", []),
    })
    results = refresh.refresh(gw, manifest, schemas, apply=True, log=lambda s: None)
    by = {r["target"]: r for r in results}
    assert by["servicenow"]["action"].startswith("UNSUPPORTED")
    assert by["legacy"]["action"].startswith("SKIPPED (not in tools.json)")
    assert by["servicenow-knowledge"]["action"].startswith("NOT DEPLOYED")
    assert by["servicenow-knowledge"]["id"] is None
    assert gw.updates == []
    with pytest.raises(SystemExit):
        refresh.refresh(gw, {"gateway": {"id": "gw-1", "targets": {}}}, schemas, apply=False)


def test_main_uses_the_lab_session_and_writes_evidence(refresh, schemas, manifest, tmp_path,
                                                        monkeypatch, capsys):
    (tmp_path / "lab-assets").mkdir()
    (tmp_path / "lab-assets/tools.json").write_text(json.dumps(schemas, ensure_ascii=False))
    old = old_schemas(schemas)
    gw = StubGateway({
        "t-sn": live_target("t-sn", "servicenow", old["servicenow"]),
        "t-kn": live_target("t-kn", "servicenow-knowledge", old["servicenow-knowledge"]),
    }, updating_polls=0)
    evidence: dict[str, dict] = {}
    stub = types.SimpleNamespace(
        ROOT=tmp_path,
        sync_assets=lambda: [],  # N17-obs558
        load=lambda: copy.deepcopy(manifest),
        AWS=types.SimpleNamespace(client=lambda name: gw if name == "bedrock-agentcore-control"
                                  else pytest.fail(name)),
        evidence=lambda name, obj: evidence.__setitem__(name, obj),
    )
    monkeypatch.setitem(sys.modules, "lab", stub)
    assert refresh.main([]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "2 target(s) differ" in out and gw.updates == []
    (name, doc), = evidence.items()
    assert name.startswith("refresh-targets-") and doc["mode"] == "dry-run"
    assert {r["action"] for r in doc["results"]} == {"WOULD UPDATE"}
    evidence.clear()
    assert refresh.main(["--apply"]) == 0
    assert [u["targetId"] for u in gw.updates] == ["t-sn", "t-kn"]
    (_, doc), = evidence.items()
    assert doc["mode"] == "apply" and {r["action"] for r in doc["results"]} == {"UPDATED"}
