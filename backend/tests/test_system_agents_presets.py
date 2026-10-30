"""N12 E0-7 — `SystemPreset` generalised to `method="zip_runtime"` presets (the evaluation
worker, W-E1-3b) without changing what the existing Harness presets render."""

from dataclasses import replace

from app.system_agents import presets, service
from app.system_agents.presets import ARCHITECT, PRESETS, InstallOptions, SystemPreset

BUCKET = "launchpad-artifacts-test"


def test_harness_presets_carry_no_template_or_lifecycle_and_the_worker_does():
    harness = [p for p in PRESETS.values() if p.method == "harness"]
    workers = [p for p in PRESETS.values() if p.method == "zip_runtime"]
    assert [p.key for p in harness] == [ARCHITECT.key]
    assert all(p.template is None and p.lifecycle is None for p in harness)
    assert [p.key for p in workers] == [presets.EVAL_WORKER.key]
    assert presets.EVAL_WORKER.template == "eval_worker"
    assert presets.EVAL_WORKER.lifecycle == {"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800}
    assert presets.EVAL_WORKER.extra_iam is not None


def test_harness_preset_spec_is_unchanged():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(), digest="d" * 64)
    assert spec.method == "harness"
    assert spec.skills == [ARCHITECT.skill_uri(BUCKET, "d" * 64)]
    assert [t.name for t in spec.tools] == [presets.AWS_KNOWLEDGE_TOOL_NAME]
    assert spec.allowed_tools == list(ARCHITECT.allowed_tools)
    assert spec.memory.short_term is False and spec.memory.long_term is False
    assert spec.model_id == ARCHITECT.resolved_model_id()  # the agent_default role (N17-obs202)


def _worker_preset() -> SystemPreset:
    return replace(
        ARCHITECT, key="eval-worker", name="launchpad-eval-worker", label="Evaluation worker",
        description="probe", method="zip_runtime", template="eval_worker",
        lifecycle={"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800},
        allowed_tools=(), skill_dir="", skill_version="0.0.0",
    )


def test_zip_runtime_preset_renders_a_bare_runtime_spec():
    preset = _worker_preset()
    spec = presets.build_spec(preset, BUCKET, preset.default_options(), digest=None)
    assert spec.method == "zip_runtime"
    assert spec.skills == [] and spec.tools == [] and spec.allowed_tools is None
    assert spec.memory.short_term is False and spec.memory.long_term is False
    assert spec.name == "launchpad-eval-worker"
    assert preset.template == "eval_worker"
    assert preset.lifecycle == {"idleRuntimeSessionTimeout": 300, "maxLifetime": 28800}


def test_status_and_install_follow_the_preset_method(monkeypatch, client):
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.models.ledger import Workspace

    preset = _worker_preset()
    monkeypatch.setattr(service.catalogue, "PRESETS", {**PRESETS, preset.key: preset})
    monkeypatch.setattr(presets, "PRESETS", service.catalogue.PRESETS)
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        statuses = {s["key"]: s for s in service.list_status(db, row, is_admin=True)}
    finally:
        db.close()
    assert statuses[ARCHITECT.key]["method"] == "harness"
    assert statuses["eval-worker"]["method"] == "zip_runtime"


def test_install_writes_the_preset_method_on_the_agent_row():
    """`install_preset` builds the Agent row from the preset — the only place the method
    literal used to be hard-coded."""
    import inspect

    source = inspect.getsource(service.install_preset)
    assert 'method="harness"' not in source and "method=preset.method" in source
    status_source = inspect.getsource(service.preset_status)
    assert '"method": "harness"' not in status_source and '"method": preset.method' in status_source
