"""The evaluation worker template (N12 E1-3b; plan §B.0 "worker packaging").

A system-managed ``zip_runtime`` preset (``launchpad-eval-worker``) whose code is this
template: ``main.py`` (the ``BedrockAgentCoreApp`` entrypoint that acks a job and runs it
on a thread) plus the engine library shipped as a real package ``launchpad_eval/`` inside
the zip. The library files are the backend's own modules **byte-for-byte** except for one
mechanical import rewrite (``app.evaluation.jobs`` / ``app.evaluation.telemetry`` /
``app.services.strands_eval`` → ``launchpad_eval``), pinned by
``tests/test_eval_worker_mirror.py`` — dev (in-process executor) and prod (the worker) run
the same code.

The release of the worker is the sha256 over everything the zip carries from this repo
(``release()``); the install pins it on the job and the spec (``RELEASE_ENV``), so an engine
change makes the preset's repair redeploy a new Runtime version.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.schemas.agent import AgentSpec, allowed_model_ids, default_judge_max_tokens
from app.templates.model_support import render_model_source

TEMPLATE_DIR = Path(__file__).parent
APP_DIR = TEMPLATE_DIR.parent.parent
LIBRARY_PACKAGE = "launchpad_eval"
# Bump when the protocol or the worker's behaviour changes in a way operators must notice;
# a pure engine fix only changes the digest (repair redeploys it either way).
EVAL_WORKER_VERSION = "1.0.0"
RELEASE_ENV = "LAUNCHPAD_EVAL_WORKER_RELEASE"
# N17-obs393: the worker's code-evaluator switch (main.py reads it); in the preset spec env only
# while `Settings.eval_code_evaluators_enabled` is on (`spec_env`)
CODE_EVALUATORS_ENV = "LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED"
RUNTIME_USER_ID = "launchpad-eval"

# zip path → repository source. E2's worker-side handlers (jobs_online / jobs_recommend /
# jobs_optimize) join the manifest automatically once their files exist.
_LIBRARY_SOURCES: dict[str, str] = {
    "jobs.py": "evaluation/jobs.py",
    "telemetry.py": "evaluation/telemetry.py",
    "spans_provider.py": "services/strands_eval/spans_provider.py",
    "langgraph_mapper.py": "services/strands_eval/langgraph_mapper.py",
    "evaluators.py": "services/strands_eval/evaluators.py",
    "code_evaluators.py": "services/strands_eval/code_evaluators.py",
    "deadlines.py": "services/strands_eval/deadlines.py",
    "results.py": "services/strands_eval/results.py",
    "simulator.py": "services/strands_eval/simulator.py",
    "engine_core.py": "services/strands_eval/engine_core.py",
    "insights.py": "services/strands_eval/insights.py",
    "sink.py": "services/strands_eval/sink.py",
    "runtime_invoke.py": "services/strands_eval/runtime_invoke.py",
    "jobs_batch.py": "services/strands_eval/jobs_batch.py",
    "model_calls.py": "services/strands_eval/model_calls.py",
    "redaction.py": "services/strands_eval/redaction.py",
    "judge_placeholders.py": "services/strands_eval/judge_placeholders.py",
    "extraction_check.py": "services/strands_eval/extraction_check.py",
    "tool_catalog.py": "services/strands_eval/tool_catalog.py",
    "global_templates.py": "services/strands_eval/global_templates.py",
    "context_relevance.py": "services/strands_eval/context_relevance.py",
    "skill_catalog.py": "services/strands_eval/skill_catalog.py",
    "judge_client.py": "services/strands_eval/judge_client.py",
    "principal_tokens.py": "services/strands_eval/principal_tokens.py",
}
_OPTIONAL_SOURCES: dict[str, str] = {
    "jobs_online.py": "services/strands_eval/jobs_online.py",
    "jobs_recommend.py": "services/strands_eval/jobs_recommend.py",
    "jobs_optimize.py": "services/strands_eval/jobs_optimize.py",
}
# The ONLY edit between a repository module and its copy in the zip.
IMPORT_REWRITES: tuple[tuple[str, str], ...] = (
    ("from app.evaluation.jobs import", f"from {LIBRARY_PACKAGE}.jobs import"),
    ("from app.evaluation import jobs", f"from {LIBRARY_PACKAGE} import jobs"),
    ("from app.evaluation.telemetry import", f"from {LIBRARY_PACKAGE}.telemetry import"),
    ("from app.services.strands_eval.", f"from {LIBRARY_PACKAGE}."),
    ("from app.services.strands_eval import", f"from {LIBRARY_PACKAGE} import"),
)
_PACKAGE_INIT = (
    '"""Launchpad evaluation engine library — shipped into the worker zip from\n'
    "backend/app/{evaluation/jobs.py, evaluation/telemetry.py, services/strands_eval/*}\n"
    "(byte-identical except the import rewrite; see app/templates/eval_worker/__init__.py)."
    '"""\n'
)


def library_manifest() -> dict[str, Path]:
    """``{<zip-relative path>: <repository file>}`` of the shipped library."""
    out = {f"{LIBRARY_PACKAGE}/{name}": APP_DIR / rel for name, rel in _LIBRARY_SOURCES.items()}
    for name, rel in _OPTIONAL_SOURCES.items():
        path = APP_DIR / rel
        if path.exists():
            out[f"{LIBRARY_PACKAGE}/{name}"] = path
    return out


def rewrite_imports(source: str) -> str:
    for old, new in IMPORT_REWRITES:
        source = source.replace(old, new)
    return source


def library_files() -> dict[str, str]:
    """The library as it lands in the zip (sorted paths)."""
    files = {f"{LIBRARY_PACKAGE}/__init__.py": _PACKAGE_INIT}
    for rel, path in library_manifest().items():
        files[rel] = rewrite_imports(path.read_text(encoding="utf-8"))
    return dict(sorted(files.items()))


def base_requirements() -> list[str]:
    lines = (TEMPLATE_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def rendered_judge_budget_defaults() -> str:
    """Python literal of the catalog's per-model judge budgets (N-SCN-bug2), inlined into the
    worker like `MODEL_INFERENCE_PARAMS` — one line per catalog model that declares one."""
    lines = ["{"]
    for model_id in allowed_model_ids():
        default = default_judge_max_tokens(model_id)
        if default is not None:
            lines.append(f"    {model_id!r}: {default!r},")
    lines.append("}")
    return "\n".join(lines)


def render_main_py(spec: AgentSpec) -> str:
    """The worker entrypoint: the Strands template's model constants + bearer helpers
    (byte-identical block, rendered for this spec) + the job dispatcher."""
    source = (TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    settings = get_settings()
    return (
        source.replace("__LAUNCHPAD_MODEL_SOURCE__", render_model_source(spec))
        .replace("__LAUNCHPAD_WORKER_NAME__", spec.name)
        .replace("__LAUNCHPAD_WORKER_RELEASE__", repr(release_tag()))
        # N-SCN-bug1: the worker's InvokeAgentRuntime read timeout follows the hub setting
        .replace("__LAUNCHPAD_INVOKE_READ_TIMEOUT_S__",
                 str(int(settings.agentcore_read_timeout_s)))
        # N-SCN-bug2: judge budget table (catalog) + floor / cap (hub settings)
        .replace("__LAUNCHPAD_JUDGE_DEFAULT_MAX_TOKENS__", rendered_judge_budget_defaults())
        .replace("__LAUNCHPAD_JUDGE_MAX_TOKENS_FLOOR__", str(int(settings.eval_judge_max_tokens)))
        .replace("__LAUNCHPAD_JUDGE_MAX_TOKENS_CAP__",
                 str(int(settings.eval_judge_max_tokens_cap)))
        # N12-obs53 / N12-obs55: the waits before the 2nd / 3rd attempt on a transient
        # upstream error (scenario replay and judge calls) follow the hub setting
        .replace("__LAUNCHPAD_TRANSIENT_RETRY_BACKOFF_S__",
                 repr(tuple(settings.eval_transient_retry_backoff)))
    )


def write_library(pkg_dir: Path) -> int:
    """Stage the library package next to ``main.py`` in the build directory."""
    count = 0
    for rel, content in library_files().items():
        dest = pkg_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        count += 1
    return count


def release() -> dict[str, Any]:
    """``{version, digest, files{rel: sha256}}`` over everything the zip carries from the
    repository: the template, the library and the requirement set."""
    files: dict[str, str] = {
        "main.py.tmpl": _sha(TEMPLATE_DIR / "main.py.tmpl"),
        "requirements.txt": _sha(TEMPLATE_DIR / "requirements.txt"),
        # the worker inlines the model block of the FLAG-OFF Strands render: the per-user
        # principal placeholders (stream A3) are removed first, so they are no new release
        "model_support": hashlib.sha256(_strands_template_off().encode("utf-8")).hexdigest(),
        # N17-obs302: inlined into the model block, so a normalizer change is a new release
        "tool_schema": _sha(APP_DIR / "templates" / "tool_schema.py"),
    }
    for rel, content in library_files().items():
        files[rel] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[rel].encode("utf-8"))
        digest.update(b"\0")
    return {"version": EVAL_WORKER_VERSION, "digest": digest.hexdigest(), "files": files}


def release_tag(rel: dict[str, Any] | None = None) -> str:
    rel = rel or release()
    return f"{rel['version']}-{rel['digest'][:12]}"


def release_version(tag: str | None) -> str | None:
    """``"1.0.0"`` from a ``"1.0.0-<digest12>"`` release tag."""
    if not tag:
        return None
    version, sep, _digest = str(tag).rpartition("-")
    return version if sep else str(tag)


def _strands_template_off() -> str:
    from app.templates.principal_support import render_principal_placeholders

    source = (APP_DIR / "templates" / "strands_agent" / "main.py.tmpl").read_text(
        encoding="utf-8")
    return render_principal_placeholders(source, enabled=False)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def spec_env() -> dict[str, str]:
    """The settings-dependent env of the worker's preset spec (N17-obs393). Switching
    ``eval_code_evaluators_enabled`` on changes the desired spec, so the preset's repair path
    redeploys the worker with the variable and — ``ensure_role`` re-puts the caps policy — the
    ``EvalWorkerCodeEvaluators`` grant; with it off nothing is added, so an existing install's
    spec stays identical (no repair churn)."""
    from app.core.config import get_settings

    return {CODE_EVALUATORS_ENV: "1"} if get_settings().eval_code_evaluators_enabled else {}


# ── execution role statements (plan §B.0 "worker execution role") ─────────────
def preset_iam_statements(ctx: Any, spec: AgentSpec) -> list[dict[str, Any]]:
    """The ``SystemPreset.extra_iam`` hook: ``agent_iam.eval_worker_statements`` (W-E3-5 owns
    the statements; this resolves the two settings-dependent opt-ins)."""
    from app.core.config import get_settings
    from app.services.agent_iam import eval_worker_statements

    settings = get_settings()
    return eval_worker_statements(
        ctx, code_evaluators=settings.eval_code_evaluators_enabled,
        results_mirror_prefix=settings.eval_results_log_group_prefix,
    )
