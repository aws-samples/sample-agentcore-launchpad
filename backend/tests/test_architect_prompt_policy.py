"""Architect assistant prompt policy (N-ARCH-obs4 / obs5 / helpdesk "decided without
asking"): the model catalog is rendered from the table — into the skill bundle at
install/repair time and into the protocol preamble per turn — the system prompt says
"choose only from this list" without naming an id, the skill states the evaluation-plan
invariants, and the prompt asks before disabling a capability the workspace lacks.
Hermetic: no AWS, no network."""

import re

import pytest

from app.assistant import service as assistant_service
from app.schemas import agent as schema
from app.system_agents import presets
from tests.model_fixtures import publish_siliconflow

_MODEL_ID_IN_BULLET = re.compile(r"^- `([^`]+)`", re.MULTILINE)


def _catalog_section(skill_md: str) -> str:
    start = skill_md.index("## Model catalog (hard constraint)")
    end = skill_md.index("\n## ", start + 10)
    return skill_md[start:end]


def _ids_listed(text: str) -> list[str]:
    return _MODEL_ID_IN_BULLET.findall(text)


def test_skill_bundle_states_exactly_the_catalog_ids_with_their_table_traits():
    snapshot = presets.snapshot_bundle(presets.ARCHITECT)
    section = _catalog_section(snapshot.files["SKILL.md"].decode("utf-8"))
    assert _ids_listed(section) == list(schema.allowed_model_ids())
    assert "Choose only from this list" in section
    # traits come from model_capabilities(), one line per model
    for model_id in schema.allowed_model_ids():
        line = next(ln for ln in section.splitlines() if f"`{model_id}`" in ln)
        caps = schema.model_capabilities()[model_id]
        assert ("yes (native vision)" if caps["vision"] else "no (text only)") in line
        assert f"starts at {caps['default_judge_max_tokens']} tokens" in line
        # N17-obs298: the install-time skill catalog tags no default — the live
        # `agent_default` role is stated by the protocol preamble and the per-turn catalog
        assert " (default)" not in line
    # the checkout itself keeps the placeholder: ids are never written into prose
    raw = (presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8")
    assert raw.count(presets.MODEL_CATALOG_PLACEHOLDER) == 1
    assert not any(m in raw for m in schema.allowed_model_ids())


def _with_fake_model(fake: str, **traits):
    """Publish the current snapshot plus one extra chat row derived from GLM 5.3 (C5: no
    module-level overlay any more — a test publishes a variant snapshot)."""
    import dataclasses

    from app.schemas import model_catalog

    glm = model_catalog.current().models["global.zai.glm-5.3"]
    row = dataclasses.replace(glm, model_id=fake, label=fake, api_model_name=fake,
                              default=False, **traits)
    model_catalog.publish(model_catalog.extended(model_catalog.current(), models={fake: row}))


def test_adding_a_model_to_the_table_changes_the_rendered_skill_and_its_release():
    from app.schemas import model_catalog

    before = presets.snapshot_bundle(presets.ARCHITECT)
    fake = "global.example.new-model-9"
    try:
        _with_fake_model(fake, vision=True, temperature=True, default_judge_max_tokens=1024,
                         max_output_tokens=32000)
        after = presets.snapshot_bundle(presets.ARCHITECT)
        ids = list(schema.allowed_model_ids())
    finally:
        model_catalog.reset()
    section = _catalog_section(after.files["SKILL.md"].decode("utf-8"))
    assert _ids_listed(section) == ids and ids[-1] == fake
    assert "documented hard output limit 32000 tokens" in section
    # a different bundle → a different content-addressed release → REPAIR republishes
    assert after.digest != before.digest and after.version == before.version
    assert presets.ARCHITECT.skill_prefix(after.digest) != presets.ARCHITECT.skill_prefix(
        before.digest)


def test_skill_md_must_state_the_whole_catalog():
    with pytest.raises(ValueError, match="states no model catalog"):
        presets.render_skill_md("# no placeholder", preset_key="x")
    twice = f"{presets.MODEL_CATALOG_PLACEHOLDER}\n{presets.MODEL_CATALOG_PLACEHOLDER}"
    with pytest.raises(ValueError, match="found 2"):
        presets.render_skill_md(twice, preset_key="x")
    # a copy of an earlier snapshot (already rendered) still states every id → accepted
    rendered = presets.render_skill_md(presets.MODEL_CATALOG_PLACEHOLDER, preset_key="x")
    assert presets.render_skill_md(rendered, preset_key="x") == rendered
    with pytest.raises(ValueError, match="states no model catalog"):
        presets.render_skill_md(rendered.replace(f"`{schema.allowed_model_ids()[0]}`", "`gone`"),
                                preset_key="x")


def _with_disabled(*, provider: str | None = None, model: str | None = None) -> None:
    """Publish the current snapshot with one provider and/or one chat model disabled — what
    an administrator does through `/api/settings/model-providers` (D21)."""
    import dataclasses

    from app.schemas import model_catalog

    cat = model_catalog.current()
    providers = models = None
    if provider:
        providers = {provider: dataclasses.replace(cat.providers[provider], enabled=False)}
    if model:
        models = {model: dataclasses.replace(cat.models[model], enabled=False)}
    model_catalog.publish(model_catalog.extended(cat, providers=providers, models=models))


@pytest.mark.siliconflow
def test_shipped_skill_releases_against_the_full_builtin_catalog():
    """N-ARCH-obs19 gate: the checkout's SKILL.md rendered against EVERY allowed id of the
    catalog (the Bedrock seed + the administrator's SiliconFlow rows, all enabled — R13) yields
    a release — version, 64-hex digest, every file hashed — and the section states exactly
    those ids, in order."""
    from app.schemas import model_catalog

    ids = list(schema.allowed_model_ids())
    assert len(ids) >= 4 and all(model_catalog.current().model_enabled(m) for m in ids)
    assert presets.stated_model_ids() == ids
    release = presets.bundle_release(presets.ARCHITECT)
    assert release["version"] == presets.ARCHITECT.skill_version == "1.7.3"
    assert re.fullmatch(r"[0-9a-f]{64}", release["digest"])
    assert "SKILL.md" in release["files"] and len(release["files"]) >= 2
    skill = presets.snapshot_bundle(presets.ARCHITECT).files["SKILL.md"].decode("utf-8")
    assert _ids_listed(_catalog_section(skill)) == ids
    # 1.7.2 states the disabled-row rule next to the list
    assert "A model an administrator has disabled in that registry is not" in skill


@pytest.mark.siliconflow
def test_release_survives_an_administrator_disabling_a_provider_or_a_model():
    """N-ARCH-obs19 (live, 2026-10-16: the repair of 1.7.0 answered 500 ``SKILL.md states
    no model catalog (missing ['siliconflow.…'])``): the rendered section correctly omits a
    row an administrator disabled, so the check must demand exactly the rows the section
    renders — the release succeeds, states the enabled ids only and moves its digest."""
    from app.schemas import model_catalog

    sf = [m for m in schema.allowed_model_ids() if m.startswith("siliconflow.")]
    assert len(sf) >= 2
    full = presets.snapshot_bundle(presets.ARCHITECT)
    try:
        _with_disabled(provider="siliconflow")
        snapshot = presets.snapshot_bundle(presets.ARCHITECT)  # raised ValueError before
        listed = _ids_listed(_catalog_section(snapshot.files["SKILL.md"].decode("utf-8")))
        assert listed == [m for m in schema.allowed_model_ids() if m not in sf]
        assert listed == presets.stated_model_ids()
        assert sf[0] in schema.allowed_model_ids()  # the disabled rows still parse (G4)
        assert snapshot.version == full.version and snapshot.digest != full.digest
        model_catalog.reset()
        publish_siliconflow()  # R13: back to the seed + the administrator's rows
        _with_disabled(model=sf[0])
        skill = presets.snapshot_bundle(presets.ARCHITECT).files["SKILL.md"].decode("utf-8")
        listed = _ids_listed(_catalog_section(skill))
        assert sf[0] not in listed and sf[1] in listed
    finally:
        model_catalog.reset()


def test_an_empty_enabled_catalog_is_unreleasable_not_vacuously_valid():
    """rev-r11: with every provider disabled the invariant "states every enabled id" would
    hold over the empty set — a bundle naming no model at all must not release, whether
    the body carries the placeholder or is an earlier rendered copy."""
    from app.schemas import model_catalog

    rendered = presets.render_skill_md(presets.MODEL_CATALOG_PLACEHOLDER, preset_key="x")
    try:
        for provider in list(model_catalog.current().providers):
            _with_disabled(provider=provider)
        assert presets.stated_model_ids() == []
        with pytest.raises(ValueError, match="no enabled chat model"):
            presets.render_skill_md(presets.MODEL_CATALOG_PLACEHOLDER, preset_key="x")
        with pytest.raises(ValueError, match="no enabled chat model"):
            presets.render_skill_md(rendered, preset_key="x")
        with pytest.raises(ValueError, match="no enabled chat model"):
            presets.snapshot_bundle(presets.ARCHITECT)
    finally:
        model_catalog.reset()


@pytest.mark.siliconflow
def test_prerendered_copy_is_judged_against_the_enabled_rows_only():
    """A SKILL.md rendered earlier (no placeholder) passes while it states every ENABLED
    id — a disabled id it lacks is not demanded; an enabled id it lacks still is."""
    from app.schemas import model_catalog

    sf = [m for m in schema.allowed_model_ids() if m.startswith("siliconflow.")]
    rendered = presets.render_skill_md(presets.MODEL_CATALOG_PLACEHOLDER, preset_key="x")
    try:
        _with_disabled(model=sf[0])
        lacking_disabled = rendered.replace(f"`{sf[0]}`", "`gone`")
        assert presets.render_skill_md(lacking_disabled, preset_key="x") == lacking_disabled
        with pytest.raises(ValueError, match=re.escape(sf[1])):
            presets.render_skill_md(rendered.replace(f"`{sf[1]}`", "`gone`"), preset_key="x")
    finally:
        model_catalog.reset()


def test_protocol_preamble_lists_the_live_catalog_per_turn():
    from app.schemas import model_catalog

    section = assistant_service.catalog_section({"resources": {}})
    tail = section[section.index("Model catalog ("):]
    assert _ids_listed(tail) == list(schema.allowed_model_ids())
    assert "choose only from this list" in tail
    assert 'MUST be one\n  of the ids under "Model catalog"' in assistant_service.PROTOCOL_PREAMBLE
    fake = "global.example.new-model-9"
    try:
        _with_fake_model(fake)
        changed = assistant_service.catalog_section({"resources": {}})
    finally:
        model_catalog.reset()
    assert f"`{fake}`" in changed and f"`{fake}`" not in section


def test_system_prompt_rules_name_no_model_and_say_ask_before_disabling():
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    assert not any(m in prompt for m in schema.allowed_model_ids())
    assert "Choose only from this list" in prompt
    assert "never write a model outside it into a\n    proposal" in prompt
    assert "Ask, don't assume" in prompt
    assert "ask ONE\n    clarifying question before emitting a proposal" in prompt
    assert "at most one or two key questions per turn" in prompt
    assert "proposal-self-check.md` §5" in prompt
    # the fence rule the protocol already carried stays
    assert "Never paste the proposal object outside that fence" in \
        assistant_service.PROTOCOL_PREAMBLE


def test_skill_self_check_states_the_evaluation_plan_invariants():
    ref = (presets.ARCHITECT.skill_path() / "references" / "proposal-self-check.md").read_text(
        encoding="utf-8")
    section = ref[ref.index("## 5. `evaluation_plan` seed"):ref.index("## 6. Last look")]
    for phrase in (
        "Count ≤ 10 evaluators", "assertions judge", "expected-tools rule",
        "a scenario OR blocked — never both", "`golden_test_ids` is `[]` on every evaluator",
        "every\n      scenario must carry that reference", "recommendation_keys",
        "0-based", "Keys and names unique", "`tool_count` needs `min` and/or `max`",
        "`reference_response` at level TRACE", "No capability conflict",
    ):
        assert phrase in section, phrase
    # the stale "no evaluation-assets seed" wording that contradicted the protocol is gone
    assert "no evaluation-assets seed" not in ref
    assert "every failing rule — not only the first" in ref


def test_skill_asks_before_assuming_and_the_preamble_memory_line_says_so():
    skill = (presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8")
    assert "**Ask, don't assume.**" in skill
    assert "Never silently disable the capability and move on." in skill
    memory_line = assistant_service.catalog_section({"resources": {}})
    assert "ask the member before proposing instead of silently disabling it" in memory_line
    with_memory = assistant_service.catalog_section({"resources": {"memory_arn": "arn:x"}})
    assert "shared memory available" in with_memory
    assert "silently" not in with_memory.split("Model catalog")[0]
    assert "AgentCore Memory" in with_memory.split("Model catalog")[0]
    # N-ARCH-bug1: the CN self-hosted substrate is shared memory too — the preamble
    # must say it is available (with its kind), never "ask the member"
    cn = assistant_service.catalog_section({"resources": {
        "memory_id": "launchpad_memory-default",
        "memory_os_endpoint": "search-lp.cn-northwest-1.es.example",
        "memory_index": "launchpad-memory", "memory_unavailable_reason": "",
    }})
    cn_memory = cn.split("Model catalog")[0]
    assert "`disabled` or `workspace` (shared memory available" in cn_memory
    assert "self-hosted memory" in cn_memory and "long-term" in cn_memory
    assert "silently" not in cn_memory and "ask the member" not in cn_memory


def test_skill_version_bumped_so_a_repair_publishes_the_new_bundle():
    # 1.7.0 → 1.7.1 (N12-obs49): the skill's iteration loop proposes a runtime canary;
    # 1.7.1 → 1.7.2 (N-ARCH-obs19): the catalog check demands the ENABLED rows and the
    # skill states the disabled-row rule — both bumps ship together as 1.7.2;
    # 1.7.2 → 1.7.3 (N17-obs295): `[]` is not a trajectory — the bump makes the N17-obs206
    # auto-repair sweep re-publish the architect preset in every workspace
    assert presets.ARCHITECT.skill_version == "1.7.3"
    assert presets.snapshot_bundle(presets.ARCHITECT).version == "1.7.3"


def test_skill_states_that_an_empty_trajectory_is_missing():
    """N17-obs295 (live 2026-10-23, E22): a complete first proposal bundled a
    `reference_trajectory` check with a global prohibition and gave four refusal scenarios
    `expected_trajectory: []` — rejected once per scenario. The rule, the reference-driven
    list and the split-evaluator example are where the model reads them."""
    files = presets.snapshot_bundle(presets.ARCHITECT).files
    skill = files["SKILL.md"].decode("utf-8")
    check = files["references/proposal-self-check.md"].decode("utf-8")
    assert "An empty\n  `expected_trajectory: []` counts as MISSING" in skill
    assert "`[]` is not a trajectory" in skill and "`[]` is not a trajectory" in check
    for evaluator in ("Builtin.TrajectoryExactOrderMatch", "Builtin.TrajectoryInOrderMatch",
                      "Builtin.TrajectoryAnyOrderMatch", "`reference_trajectory`",
                      "`reference_response`", "`{expected_tool_trajectory}`",
                      "`{expected_response}`", "`{assertions}`"):
        assert evaluator in check, evaluator
    assert '"type": "tool_set",\n' in check and "Never bundle a `reference_trajectory`" in check
    assert "`[]` counts as missing" in presets.ARCHITECT_SYSTEM_PROMPT
    assert "`expected_trajectory: []` counts as missing" in assistant_service.PROTOCOL_PREAMBLE


def test_skill_and_prompts_state_the_recommendation_entry_shape_and_gate_rule():
    """WB-006 / WB-009: the object shape of an ``evaluator_recommendations`` entry and
    the rule that a stated gate must name a resolvable evaluator are written where the
    model reads them — SKILL.md §4, the self-check (§2 and §5), system-prompt rule 14
    and the per-turn preamble — in the words the validator uses."""
    skill_dir = presets.ARCHITECT.skill_path()
    skill = _flat((skill_dir / "SKILL.md").read_text(encoding="utf-8"))
    check = _flat((skill_dir / "references" / "proposal-self-check.md")
                  .read_text(encoding="utf-8"))
    rule14 = _flat(presets.ARCHITECT_SYSTEM_PROMPT.split("14. A proposal")[1])
    preamble = _flat(assistant_service.PROTOCOL_PREAMBLE)
    shape = '{"text", "evaluator_id"?, "blocking"?, "threshold"?, "metric"?}'
    assert shape in check and shape in preamble  # the preamble is an f-string
    assert '"blocking": true|false, "threshold": 0–1, "metric": "<name>"' in skill
    assert "recommendation entries carry the gate" in skill
    assert "**Recommendation gates" in check
    for text in (skill, check, rule14, preamble):
        assert "seed evaluator" in text and "evaluator_id" in text
    for text in (skill, check):
        assert "阻断" in text and "门禁" in text and "阈值" in text
        assert "asks for a gate" in text and "no evaluator carries" in text
    assert "registry row" in rule14 and "never a bare sentence" in rule14
    assert "no evaluator carries makes the proposal invalid" in preamble


def _flat(text: str) -> str:
    """Collapse line wraps so the prose assertions pin wording, not wrap positions."""
    return " ".join(text.split())


def test_ask_first_covers_open_material_decisions_when_the_capability_is_available():
    """N-ARCH-bug2: "ask, don't assume" is not scoped to capabilities the workspace lacks —
    an unspecified material decision (memory while shared memory is available, the model
    while several are selectable, a KB / tools the brief implies) is asked in the first
    reply, never defaulted and flagged as an assumption; a brief that states or delegates
    every decision gets its proposal at once (spec 11a)."""
    skill = (presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8")
    rule = _flat(
        skill[skill.index("- **Ask, don't assume.**"):skill.index("After the two baseline")]
    )
    for phrase in (
        "*A material decision the brief leaves open.*", "**asked, never defaulted**",
        "**long-term memory** (`memory: workspace` or `disabled`)",
        "**the model** whenever more than one catalog model is selectable",
        "**knowledge-base attachment** whenever the catalog lists an active knowledge base",
        "**tools / MCP servers** whenever the catalog lists tools",
        "Skip a decision only when the brief states it",
        '"decide yourself", "all decisions are final", "你自己决定"',
        "A brief that states or delegates every material decision gets its proposal at once",
        "`memory: \"disabled\"` plus a note", "exactly the failure this rule forbids",
        "Example — a helpdesk brief", "> Before I write the proposal, one decision only you "
        "can make. **1. Long-term > memory**", "(a) `workspace`", "(b) `disabled`",
    ):
        assert phrase in rule, phrase
    # the system prompt (rule 13) and the per-turn protocol preamble (hard rule 1) say it too
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    rule13 = _flat(prompt[prompt.index("13. Ask, don't assume"):prompt.index("14. A proposal")])
    for phrase in ("material decision the brief leaves open", "long-term memory while shared "
                   "memory is available", "more than one catalog model is selectable",
                   "FIRST reply", "propose only after the answer", "never pick a default",
                   '"decide yourself", "all decisions are final", "你自己决定"',
                   "gets its proposal at once"):
        assert phrase in rule13, phrase
    preamble = assistant_service.PROTOCOL_PREAMBLE
    rule1 = _flat(preamble[preamble.index("Hard rules of this environment"):preamble.index(
        "2. The block is a **proposal**")])
    for phrase in ("A material decision the brief leaves open is asked too",
                   '"Memory" below says shared memory is available', "FIRST reply",
                   "propose only after the answer", "never pick a default",
                   "gets its proposal at once"):
        assert phrase in rule1, phrase
    # the Memory line carries the rule exactly when memory IS available (the unavailable
    # branch keeps its own "ask before disabling" wording)
    cn = assistant_service.catalog_section({"resources": {
        "memory_id": "launchpad_memory-default",
        "memory_os_endpoint": "search-lp.cn-northwest-1.es.example",
        "memory_index": "launchpad-memory", "memory_unavailable_reason": "",
    }}).split("Model catalog")[0]
    agentcore = assistant_service.catalog_section(
        {"resources": {"memory_arn": "arn:x"}}).split("Model catalog")[0]
    for line in (cn, agentcore):
        assert "Material decision (N-ARCH-bug2)" in line
        assert "put the memory question to the customer in your first reply" in line
        assert "never default it to `disabled` and call it an assumption" in line
        assert "delegates the choice" in line
    none = assistant_service.catalog_section({"resources": {}}).split("Model catalog")[0]
    assert "Material decision" not in none and "`disabled` only" in none


@pytest.mark.siliconflow
def test_unconfigured_rows_name_the_ssm_parameter_in_bundle_and_preamble(monkeypatch):
    """N-ARCH-bug3: the architect refuses an unconfigured model naming the exact SSM
    parameter — so the rendered row must carry it (from PROVIDERS, never prose) and the
    rule must tell the architect to quote it."""
    sf = "siliconflow.zai-org/GLM-5.3"
    row = schema.providers()["siliconflow"]
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    section = _catalog_section(
        presets.snapshot_bundle(presets.ARCHITECT).files["SKILL.md"].decode("utf-8"))
    line = next(ln for ln in section.splitlines() if f"`{sf}`" in ln)
    assert line.startswith(f"- `{sf}` {schema.MODEL_UNAVAILABLE_MARKER} — selectable once "
                           "the operator stores the SiliconFlow API key as SSM SecureString "
                           f"parameter `{row['ssm_parameter']}`")
    assert f"`{row['api_key_env']}`" in line and "re-runs bootstrap" in line
    assert "quote the SSM SecureString parameter named on that model's row" in section
    assert "no console setting for it" in section
    glm = next(ln for ln in section.splitlines() if "`global.zai.glm-5.3`" in ln)
    assert "SSM" not in glm
    preamble = assistant_service.catalog_section({"resources": {}})
    pline = next(ln for ln in preamble.splitlines() if f"`{sf}`" in ln)
    assert f"`{row['ssm_parameter']}`" in pline
    assert "quote the SSM SecureString parameter named on its row" in preamble
    # the parameter follows the table: a renamed parameter reaches the prose unedited
    import dataclasses

    from app.schemas import model_catalog

    snapshot = model_catalog.current()
    renamed = dataclasses.replace(snapshot.providers["siliconflow"], ssm_parameter="lp-sf-key")
    model_catalog.publish(model_catalog.extended(snapshot, providers={"siliconflow": renamed}))
    try:
        assert "`lp-sf-key`" in next(ln for ln in schema.model_catalog_lines() if f"`{sf}`" in ln)
    finally:
        model_catalog.publish(snapshot)
    # configured → no SSM hint on any row
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    rows = [ln for ln in schema.model_catalog_lines() if ln.startswith("- `")]
    assert rows and all("SSM" not in ln for ln in rows)


def test_self_check_states_the_reference_driven_trajectory_rule():
    """N-ARCH-obs10: a reference-driven trajectory evaluator needs `expected_trajectory`
    on EVERY scenario — stated in the self-check §5, SKILL.md §4 and system-prompt rule 14."""
    ref = (presets.ARCHITECT.skill_path() / "references" / "proposal-self-check.md").read_text(
        encoding="utf-8")
    section = _flat(ref[ref.index("## 5. `evaluation_plan` seed"):ref.index("## 6. Last look")])
    assert "**Reference-driven trajectory evaluator ⇒ `expected_trajectory` on EVERY scenario.**" \
        in section
    for phrase in ("count the scenarios that carry `expected_trajectory`",
                   "add the trajectory to every scenario or drop / replace the evaluator",
                   "`reference_trajectory` code rule", "`{expected_tool_trajectory}`",
                   "cannot be scoped to some scenarios"):
        assert phrase in section, phrase
    skill = _flat((presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8"))
    assert "may be seeded only when EVERY scenario carries `expected_trajectory`" in skill
    prompt = _flat(presets.ARCHITECT_SYSTEM_PROMPT)
    assert ("requires a non-empty `expected_trajectory` on EVERY scenario, so count them "
            "before you emit — `[]` counts as missing") in prompt


@pytest.mark.siliconflow
def test_prose_marks_an_unconfigured_provider_model_and_the_rule_forbids_proposing_it(
    monkeypatch,
):
    """N-R6-obs1: the skill bundle (install/repair) and the per-turn preamble both mark a
    model whose provider has no key and tell the architect never to propose it."""
    sf = "siliconflow.zai-org/GLM-5.3"
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    snapshot = presets.snapshot_bundle(presets.ARCHITECT)
    section = _catalog_section(snapshot.files["SKILL.md"].decode("utf-8"))
    assert _ids_listed(section) == list(schema.allowed_model_ids())  # rows stay listed
    line = next(ln for ln in section.splitlines() if f"`{sf}`" in ln)
    assert schema.MODEL_UNAVAILABLE_MARKER in line
    glm = next(ln for ln in section.splitlines() if "`global.zai.glm-5.3`" in ln)
    assert schema.MODEL_UNAVAILABLE_MARKER not in glm
    assert "never propose it" in section and "SSM SecureString parameter" in section
    preamble = assistant_service.catalog_section({"resources": {}})
    assert schema.MODEL_UNAVAILABLE_MARKER in preamble and "never propose it" in preamble
    # the per-turn preamble reads the WORKSPACE's record from the catalog summary
    configured = assistant_service.catalog_section(
        {"resources": {"model_credentials": {"siliconflow": {"configured": True}}}})
    assert all(schema.MODEL_UNAVAILABLE_MARKER not in ln for ln in configured.splitlines()
               if ln.startswith("- `siliconflow."))
    # configured → no row carries the marker (the rule sentence still quotes it)
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    snapshot = presets.snapshot_bundle(presets.ARCHITECT)
    section = _catalog_section(snapshot.files["SKILL.md"].decode("utf-8"))
    rows = [ln for ln in section.splitlines() if ln.startswith("- `")]
    assert len(rows) == len(schema.allowed_model_ids())
    assert all(schema.MODEL_UNAVAILABLE_MARKER not in ln for ln in rows)
    preamble_rows = [ln for ln in assistant_service.catalog_section({"resources": {}})
                     .splitlines() if ln.startswith("- `siliconflow.")]
    assert preamble_rows and all(schema.MODEL_UNAVAILABLE_MARKER not in ln
                                 for ln in preamble_rows)


def test_skill_and_prompts_state_the_evaluator_entry_shape_rules_the_validator_quotes():
    """N-ARCH-obs11: the two first-revision errors of 2026-10-14 (hyphenated judge `name`,
    `description` on a built-in entry) are now rules the architect reads before the block —
    self-check §5, SKILL.md §4, system-prompt rule 14 and the per-turn preamble — quoting
    the exact pattern the validator enforces, so skill and validator cannot drift apart."""
    from app.assistant import evaluation_plan as plan

    pattern = f"`{plan._EVALUATOR_NAME_RE}`"
    assert pattern in plan.EVALUATOR_NAME_RULE and "`note`" in plan.EXISTING_NO_DESCRIPTION_RULE
    ref = (presets.ARCHITECT.skill_path() / "references" / "proposal-self-check.md").read_text(
        encoding="utf-8")
    section = _flat(ref[ref.index("## 5. `evaluation_plan` seed"):ref.index("## 6. Last look")])
    assert "**Entry shape" in section and pattern in section
    for phrase in ("letters, digits and underscores only", "**no hyphens**",
                   "`helpdesk_guardrails_judge`", "never `helpdesk-guardrails-judge`",
                   "`kind: existing` entry carries no `description`", "put commentary in `note`"):
        assert phrase in section, phrase
    skill = _flat((presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8"))
    assert pattern in skill and "built-in `existing` entries carry no `description`" in skill
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    rule14 = _flat(prompt[prompt.index("14. A proposal"):])
    assert "letters, digits and underscores, never hyphens" in rule14
    assert "no `description` on a built-in (`existing`) entry" in rule14
    preamble = _flat(assistant_service.PROTOCOL_PREAMBLE)
    assert pattern in preamble and "underscores, never hyphens" in preamble
    assert "`existing` entries take no `description` — use `note`" in preamble


def test_skill_and_prompts_state_the_judge_description_limit_the_validator_trims_at():
    """N-ARCH-obs14 (2026-10-14, architect 1.6.3): a judge `description` of 201 characters
    made a whole proposal INVALID ("String should have at most 200 characters") and cost a
    turn. Skill 1.6.4 states the limit in one sentence — verbatim the validator's
    ``JUDGE_DESCRIPTION_RULE`` — in all four places the model reads before the block:
    SKILL.md §4, the self-check §5 "Entry shape", system-prompt rule 14 and the per-turn
    preamble; the platform now trims instead of refusing, and the skill says so."""
    from app.assistant import evaluation_plan as plan

    rule = plan.JUDGE_DESCRIPTION_RULE
    assert rule == ("`judge.description` ≤ 200 characters — one sentence; put detail in the "
                    "rubric/instructions")
    assert f"{plan.MAX_DESCRIPTION} characters" in rule
    ref = (presets.ARCHITECT.skill_path() / "references" / "proposal-self-check.md").read_text(
        encoding="utf-8")
    section = _flat(ref[ref.index("## 5. `evaluation_plan` seed"):ref.index("## 6. Last look")])
    entry_shape = section[section.index("**Entry shape"):section.index("**Recommendation gates")]
    assert rule in entry_shape
    assert "trims an over-long `title` / `description` / `note`" in entry_shape
    assert "never touches `name`, `instructions`" in entry_shape
    skill = _flat((presets.ARCHITECT.skill_path() / "SKILL.md").read_text(encoding="utf-8"))
    assert rule in skill and "records a `proposal_repaired` note on the entry" in skill
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    rule14 = _flat(prompt[prompt.index("14. A proposal"):])
    assert rule in rule14
    preamble = _flat(assistant_service.PROTOCOL_PREAMBLE)
    assert rule in preamble


def test_preamble_names_no_global_recommendation_service_for_the_red_team_rule():
    """N12-obs32: the CN optimizer is a self-hosted provider (GEPA-lite / Strands
    harness-optimizer), so the ``adversarial`` rule describes what Launchpad does — keep
    the session out of the optimizer's evidence (``recommendations.adversarial_sessions``)
    — instead of blaming the Global managed service's refusal. The needles are composed so
    the repo-wide sweep (`git grep` for the service name) stays empty."""
    import inspect

    from app.assistant import proposal

    global_service = "AgentCore " + "Recommendations"
    text = assistant_service.PROTOCOL_PREAMBLE
    flat = " ".join(text.split())  # the preamble wraps at 90 columns
    assert "`adversarial` — `true` for every red-team test" in flat
    assert "Launchpad keeps that session out of the evidence the prompt optimizer reads" in flat
    for copy in (flat, inspect.getsource(proposal)):
        assert global_service not in copy
        for verb in ("refuses", "rejects"):
            assert f"Recommendations {verb}" not in copy


def _skill_text(*names: str) -> tuple[str, ...]:
    """Flattened text of SKILL.md (no name) or of a reference file under ``references/``."""
    root = presets.ARCHITECT.skill_path()
    out = []
    for name in names:
        path = root / "SKILL.md" if name == "SKILL.md" else root / "references" / name
        out.append(_flat(path.read_text(encoding="utf-8")))
    return tuple(out)


def test_blanket_acceptance_of_the_express_close_confirms_its_suggestions():
    """Upstream fb947d44 (skill 1.5.8, ported as 1.7.0 — N-ARCH-obs16): the express close
    listed one suggested barrier per dimension, the member answered "默认", and every
    revision dropped the fishbone as merely assumed. A blanket acceptance confirms the
    listed suggestions; the methodology, the skill, its self-check and the protocol
    preamble all say so."""
    method, skill, check = _skill_text(
        "fishbone-methodology.md", "SKILL.md", "proposal-self-check.md")
    preamble = _flat(assistant_service.PROTOCOL_PREAMBLE)
    for text in (method, skill, check, preamble):
        assert "blanket acceptance" in text and "默认" in text
    assert "confirms EVERY suggestion in that list" in method
    assert '"default" is not a decline' in skill
    assert "the first option says outright that the listed barriers apply" in method
    assert "so those are `confirmed` and the member is emitted" in preamble


def test_intake_reaches_a_proposal_in_few_turns():
    """Upstream fb947d44 (skill 1.5.8, ported as 1.7.0 — N-ARCH-obs16): a rich brief took
    11 member turns — two question rounds, a one-question-at-a-time fishbone with
    drill-downs, a summary that withheld the proposal at "需求先到这里", then a table
    confirmation after "启动 proposal". The skill now asks one round for a rich brief,
    defaults the non-material questions, runs the fishbone as one express message and
    treats a proposal request as the authorization to submit. The target pace and the
    system-prompt rule 5 carry the same rule."""
    skill, intake, method, pain = _skill_text(
        "SKILL.md", "intake-options.md", "fishbone-methodology.md", "painpoint-workflow.md")
    assert "rich brief" in skill and "rich brief" in intake
    assert "ONE message of at most 4 questions" in skill
    assert "Never ask what has a safe library default" in skill
    assert '"default — say if you want it changed"' in skill
    assert "Acknowledge each answer in at most one line" in skill
    assert "express mode by default" in skill and "Express mode inside the intake" in method
    assert "no drill-down follow-ups" in method and "No drill-down follow-ups" in skill
    assert "**Pick the mode, then keep its rhythm.**" in method
    for text in (skill, pain):
        assert "需求先到这里" in text and "启动 proposal" in text
    assert "**A request to produce the proposal is that authorization**" in skill
    assert "A request for the proposal skips this gate" in pain
    assert "never ask for the table to be confirmed first" in skill
    assert "Never answer it with a summary that withholds the proposal" in skill
    assert ("**Target pace:** a rich brief reaches a reviewable proposal in about four member "
            "turns") in skill
    assert "a sparse one in about five" in skill
    # current state and failure cases ride in the express message, not in rounds of their own
    assert "rides in the express fishbone message as one optional line" in pain
    assert "do NOT ask a separate round for failure cases or evidence sources" in pain
    # the system prompt states the pace in one rule (rule 5) — rules 13 and 14 untouched
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    rule5 = _flat(prompt[prompt.index("5. Ask only"):prompt.index("6. Conclusions")])
    for phrase in ("A rich brief gets ONE round of at most 4 questions",
                   "the fishbone is ONE express message", '"出方案"', '"需求先到这里"',
                   "submit the proposal in the same reply", "never a summary that withholds it",
                   "about four member turns"):
        assert phrase in rule5, phrase


def test_library_defaults_never_cover_a_material_decision():
    """Reconciliation of upstream 1.5.8 with N-ARCH-bug2: upstream lists cross-session
    memory among the safe defaults that are never asked; this build keeps memory, the model,
    a knowledge base and tools as MATERIAL decisions asked in the first reply, and adopts the
    upstream defaults only for native sandbox tools, the access channel and log retention.
    The skill, the option library and the system prompt state the reconciliation
    explicitly, and a proposal request never skips the first-reply question."""
    skill, intake, pain = _skill_text("SKILL.md", "intake-options.md", "painpoint-workflow.md")
    defaults = skill[skill.index("Never ask what has a safe library default"):
                     skill.index("Acknowledge each answer")]
    assert "native sandbox tools (`shell`, `file_operations`)" in defaults
    assert "the access channel when none was named (Launchpad first)" in defaults
    assert "log retention (platform default, flagged for the admin)" in defaults
    assert "**Reconciliation — cross-session memory is NOT a library default in this build.**" \
        in defaults
    assert "long-term memory is a material decision and is asked" in defaults
    assert "material decisions are asked, never defaulted" in defaults
    assert "memory" not in defaults[:defaults.index("**Reconciliation")].replace(
        "say if you want it changed", "")
    # the open material decisions ride in the single rich-brief round, never a round of their own
    assert "ride in that same numbered list, never as a round of their own" in skill
    # a proposal request in the opening message still gets the bug2 question first
    ask = skill[skill.index("- **Ask, don't assume.**"):skill.index("After the two baseline")]
    assert "does not skip this rule when it arrives in the opening message" in ask
    assert "the answer is the only thing between the customer and the proposal" in ask
    assert "that IS the delegation: choose, record each choice under `assumptions`" in ask
    assert "Cross-session memory, the model, a knowledge base and tools are NOT such defaults" \
        in intake
    assert "A material decision the customer was asked and left open is thereby delegated" in pain
    prompt = presets.ARCHITECT_SYSTEM_PROMPT
    rule5 = _flat(prompt[prompt.index("5. Ask only"):prompt.index("6. Conclusions")])
    assert "never memory, the model, a knowledge base or tools" in rule5
    # the bug2 statements themselves are untouched (their own test pins the wording)
    assert "**asked, never defaulted**" in skill
    assert "exactly the failure this rule forbids" in skill
