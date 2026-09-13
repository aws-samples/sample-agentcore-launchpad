"""The system-preset editor is the shared agent configure page (SE-044).

The console has no unit-test runner, so this pins the contract statically: the
System presets card carries no Skill-record row / Registry link / REGISTER-VERIFY
SKILL / REPAIR controls and no dedicated settings dialog; CONFIGURE hands the row to
the wizard, whose system branch saves through the administrator maintenance route
(never the ordinary redeploy) with a partial body, and whose shared form carries the
inference / loop knobs the ordinary harness edit must round-trip.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend" / "src"
PANEL = FRONTEND / "pages" / "create" / "SystemPresetsPanel.tsx"
WIZARD = FRONTEND / "pages" / "CreateAgent.tsx"
HELPERS = FRONTEND / "pages" / "create" / "presetSettings.ts"
DIALOG = FRONTEND / "pages" / "create" / "PresetSettingsDialog.tsx"
MOCK = ROOT / "backend" / "scripts" / "ui_system_preset_settings_mock.py"

pytestmark = pytest.mark.skipif(not WIZARD.is_file(), reason="frontend sources not present")


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_dedicated_settings_dialog_is_gone() -> None:
    assert not DIALOG.exists(), "PresetSettingsDialog.tsx must not come back"
    for path in (PANEL, WIZARD, HELPERS):
        assert "PresetSettingsDialog" not in _src(path), path.name
    assert "preset-settings-dialog" not in _src(PANEL)


def test_panel_card_has_no_skill_or_repair_controls() -> None:
    panel = _src(PANEL)
    for banned in (
        "preset-skill-registration",
        "skill-record-link",
        "register-skill-",
        "repair-${preset.key}",
        "registerSystemPresetSkill",
        "create.system.skill.",
        "create.system.repair\"",
        "force: true",
    ):
        assert banned not in panel, banned
    # the only maintenance writes the card itself drives
    assert "api.installSystemPreset(preset.key, {}, startedIn)" in panel
    assert "api.uninstallSystemPreset(" in panel
    # CONFIGURE hands the row to the shared editor
    assert "onConfigure?.(presetConfigureRequest(" in panel
    assert 'data-testid={`settings-${preset.key}`}' in panel


def test_wizard_system_branch_saves_through_maintenance_route_only() -> None:
    wizard = _src(WIZARD)
    start, end = wizard.index("if (systemEdit) {"), wizard.index("const spec = buildSpec();")
    system_branch = wizard[start:end]
    assert "api.installSystemPreset(" in system_branch
    assert "systemChanged ? systemBody : { force: true }" in system_branch
    assert "systemEdit.workspaceId" in system_branch  # pinned save
    assert "redeployAgent" not in system_branch and "buildSpec" not in system_branch
    # the ordinary paths are unchanged
    assert "await api.redeployAgent(editing.id, spec)" in wizard
    assert "await api.createAgent(spec)" in wizard
    # the poll that follows a system save is pinned too
    assert "api.getAgent(launch.agentId, launch.workspaceId)" in wizard
    assert "api.getJob(launch.jobId, launch.workspaceId)" in wizard
    # protected members are rendered, never edited, for a preset
    assert 'data-testid="preset-protected"' in wizard
    assert "{!systemEdit && (" in wizard
    # the table's EDIT on a system row is administrator-only and uses the same path
    assert "(!a.system || isAdmin)" in wizard
    assert "void openSystemEdit(a)" in wizard


def test_async_editor_races_are_generation_guarded() -> None:
    """Host review 1: a late generic KB-catalog response must not replace the pinned
    preset catalog, and a stale table-EDIT preset read must not reset a newer draft.
    The behaviour itself is exercised with held responses by the mock browser
    scenario (`race_scenario`); this pins the guards' presence in both writers."""
    wizard = _src(WIZARD)
    generic = wizard[wizard.index('fetch("/api/knowledge-bases")') :]
    generic = generic[: generic.index(".catch(")]
    assert "kbCatalogGen.current !== catalogGen" in generic
    pinned = wizard[wizard.index("const loadSystemKbCatalog") :]
    pinned = pinned[: pinned.index("const submit = async")]
    assert "++kbCatalogGen.current" in pinned and "kbCatalogGen.current === catalogGen" in pinned
    opener = wizard[wizard.index("const openSystemEdit = async") :]
    opener = opener[: opener.index("const startEdit = (agent")]
    assert "nextEditorIntent()" in opener and "editorGen.current === intent" in opener
    # every newer intent invalidates a pending open
    assert wizard.count("nextEditorIntent()") >= 5  # opener, resetForm, startEdit, details, new
    mock = _src(MOCK)
    assert "def race_scenario" in mock and "release_kb(foreign)" in mock
    assert "release_status()" in mock and "UNSAVED ordinary draft" in mock


def test_partial_edit_helper_matches_backend_contract() -> None:
    helpers = _src(HELPERS)
    # the same two knobs the backend lets a request `clear`
    assert 'const clear: ("max_tokens" | "reasoning_effort")[] = []' in helpers
    assert "MAX_TOKENS_CEILING = 131072" in helpers
    # the ordinary create defaults the shared form starts on = AgentSpec defaults
    assert "DEFAULT_MAX_ITERATIONS = 10" in helpers
    assert "DEFAULT_TIMEOUT_SECONDS = 300" in helpers
    from app.schemas.agent import MAX_TOKENS_CEILING, AgentSpec

    assert MAX_TOKENS_CEILING == 131072
    fields = AgentSpec.model_fields
    assert fields["max_iterations"].default == 10
    assert fields["timeout_seconds"].default == 300


def test_shared_form_carries_the_harness_knobs_for_ordinary_edits() -> None:
    wizard = _src(WIZARD)
    for testid in ("agent-max-tokens", "agent-effort", "agent-max-iterations", "agent-timeout"):
        assert f'data-testid="{testid}"' in wizard, testid
    # loaded exactly as stored (startEdit) …
    assert 'setMaxTokens(spec.max_tokens == null ? "" : String(spec.max_tokens))' in wizard
    assert "setReasoningEffort(spec.reasoning_effort ?? EFFORT_NONE)" in wizard
    # … and sent back for the harness only (the other methods' schema refuses them)
    assert 'method === "harness" && intOrNull(maxTokens)' in wizard
    assert 'method === "harness" && ordinaryEffort' in wizard


def test_i18n_has_no_orphaned_card_strings() -> None:
    for locale in ("en", "zh-CN"):
        data = json.loads((FRONTEND / "locales" / locale / "common.json").read_text("utf-8"))
        system = data["create"]["system"]
        assert "skill" not in system and "repair" not in system and "update" not in system
        assert "registerSkill" not in system["confirm"] and "repair" not in system["confirm"]
        for key in ("configureHint", "editingNote", "protected", "protectedNote", "confirmForce"):
            assert key in system["settings"], (locale, key)


def test_mock_browser_script_targets_the_shared_page() -> None:
    mock = _src(MOCK)
    # the dialog is only ever asserted ABSENT, never waited for
    assert 'get_by_test_id("preset-settings-dialog").wait_for' not in mock
    assert 'get_by_test_id("preset-settings-dialog").count() == 0' in mock
    assert re.search(r'data-testid="configure-step"', mock)
    assert "launch-submit" in mock
    # a preset redeploy is answered 403 by the fixture and asserted never to happen
    assert "/api/agents/{AGENT_ID}/redeploy" in mock and "agent.system_managed" in mock
