"""Codegen module configuration (fix pipeline only).

Ported from strands_studio_ui ``backend/codegen/config.py`` (origin/main) and
adapted to launchpad's ``pydantic-settings`` block: the tunables
(``codegen_backend`` / ``codegen_model`` / ``codegen_timeout_s`` /
``codegen_max_repair_rounds``) live in ``app.core.config.Settings``; this module
keeps the path constants and the file-based guidance VERSION read.

Generation-only knobs (cache dir, example selection) were dropped with the
generate pipeline — launchpad ports the AI-fix half only.
"""

from pathlib import Path

from app.core.config import get_settings

# Directory layout
CODEGEN_DIR = Path(__file__).resolve().parent
GUIDANCE_DIR = CODEGEN_DIR / "guidance"

# The single file the coding-agent backend must produce inside its workspace
GENERATED_FILENAME = "generated_agent.py"

# Hard cap on agent turns to prevent runaway sessions
CODEGEN_MAX_TURNS = 30

# Import smoke test timeout (seconds)
IMPORT_SMOKE_TIMEOUT_S = 20


def get_backend_name() -> str:
    """Selected coding-agent backend (settings.codegen_backend, default 'kimi')."""
    return (get_settings().codegen_backend or "kimi").strip().lower()


def get_model() -> str:
    """Model id used by the coding agent (settings.codegen_model)."""
    return get_settings().codegen_model.strip()


def get_timeout_s() -> float:
    """End-to-end AI-fix timeout in seconds (settings.codegen_timeout_s)."""
    return float(get_settings().codegen_timeout_s)


def get_max_repair_rounds() -> int:
    """Maximum validation repair rounds (settings.codegen_max_repair_rounds)."""
    return int(get_settings().codegen_max_repair_rounds)


def get_exec_python() -> str:
    """Interpreter used for the import smoke test (settings.studio_exec_python).

    Only this interpreter has strands installed; the lean control-plane backend
    env does not, so the import stage skips-with-note when it is missing.
    """
    return get_settings().studio_exec_python


def get_guidance_version() -> str:
    """Content of guidance/VERSION (informational; part of status). '0' if missing."""
    version_file = GUIDANCE_DIR / "VERSION"
    try:
        return version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "0"
