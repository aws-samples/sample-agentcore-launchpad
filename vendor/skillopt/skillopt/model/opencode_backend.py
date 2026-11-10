"""OpenCode executor backend — LAUNCHPAD PATCH (D16 / N13, see LAUNCHPAD_DEVIATIONS.md).

The ONE module that knows OpenCode. Everything the vendored pipelines need from an
exec agent CLI — the headless command, the per-run ``opencode.json`` (Bedrock's
OpenAI-compatible endpoint as a custom provider, the API key referenced as
``{env:AWS_BEARER_TOKEN_BEDROCK}`` and never written to disk), the ``--format json``
event stream parsed into SkillOpt's transcript shape (turns, tool calls, final
message, token usage), the restricted judge agent, and the ``ExecutorBackend``
protocol every other module programs against — lives here, so the N13 spike's
findings (real CLI flags, real event schema, the version pin) slot into one file.

Replaces the former Claude Code / Codex executors (deleted, not flagged):
``run_opencode_exec`` keeps the keyword surface of the executor it replaces, so
rollout, taskgen, the judge worker and the AgentCore worker call it unchanged.

Import rules: the builders and the parser below import only the stdlib (the hermetic
backend tests load this module alone, with stub parent packages); the run path
lazy-imports its siblings (``agentcore_runner`` for the remote worker,
``codex_harness`` for the shared artifact persistence) to avoid import cycles.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

OPENCODE_BACKEND = "opencode_exec"
PROVIDER_ID = "bedrock-openai"
PROVIDER_NAME = "Amazon Bedrock (OpenAI-compatible)"
PROVIDER_NPM = "@ai-sdk/openai-compatible"
CONFIG_SCHEMA_URL = "https://opencode.ai/config.json"
CONFIG_FILENAME = "opencode.json"
JUDGE_AGENT = "skillopt-judge"
ARTIFACT_PREFIX = "opencode"
# Per-job OpenCode home (XDG dirs, TMPDIR, the config file) — a sibling of the
# work_dir inside the prediction dir, so nothing the CLI writes lands in the
# workspace manifest and nothing survives the job dir (spike §7).
HOME_DIRNAME = ".opencode-home"
SESSION_TITLE = "skillopt"

# Set by the Launchpad side (backend/app/skill_lab/executors/opencode.py builds it
# with the partition helper; the worker runtime carries it as an env var) — this
# module never spells a Bedrock hostname itself.
BASE_URL_ENV = "SKILLOPT_OPENCODE_BASE_URL"
# The Global Bedrock API key, resolved through Identity by the host/worker and
# injected as an environment variable only (docs/cn/interfaces/model-credential.md).
API_KEY_ENV = "AWS_BEARER_TOKEN_BEDROCK"
# Egress allow-list for the dead-proxy trick (spike §7): the Launchpad side passes
# the partition suffixes the worker/host legitimately talk to; absent, the suffix of
# the provider base URL is used.
NO_PROXY_ENV = "SKILLOPT_OPENCODE_NO_PROXY"
CONFIG_PATH_ENV = "OPENCODE_CONFIG"
# LAUNCHPAD PATCH (N-R6): extra OpenAI-compatible providers (SiliconFlow …), declared by the
# Launchpad side as JSON in this variable — the vendored module never spells a provider:
#   {"<provider id>": {"name": str, "base_url": str, "api_key_env": str,
#                      "credential_provider": str, "id_prefix": str,
#                      "models": {"<api model name>": {catalog entry}}}}
# A target/judge model whose id carries a provider's `id_prefix` (Launchpad ids such as
# `siliconflow.Qwen/Qwen3.8-27B`) or `<provider id>/` is addressed on that provider; every
# other id stays on the Bedrock default provider above. The key of an extra provider is
# read by the CLI from `api_key_env` (exported by the host/worker — never written here).
PROVIDERS_ENV = "SKILLOPT_OPENCODE_PROVIDERS"
DISABLE_AUTOUPDATE_ENV = "OPENCODE_DISABLE_AUTOUPDATE"
# The CLI never self-updates, never fetches the model catalog or ripgrep/LSPs from
# GitHub, never loads external plugins or the user's Claude Code skills, never
# shares sessions (spike §7, pinned per call AND in the worker image env).
DISABLE_ENV = {
    DISABLE_AUTOUPDATE_ENV: "1",
    "OPENCODE_DISABLE_MODELS_FETCH": "1",
    "OPENCODE_DISABLE_SHARE": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE_SKILLS": "1",
    "OPENCODE_DISABLE_LSP_DOWNLOAD": "1",
    "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
}
# Variables the CLI child inherits from the caller (never os.environ.copy()).
INHERITED_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR", API_KEY_ENV,
)
DEAD_PROXY = "http://127.0.0.1:9"
# Catalog entries declared on the custom provider (spike §3; limits per model card).
MODEL_CATALOG: dict[str, dict[str, Any]] = {
    "global.moonshotai.kimi-k3": {
        "name": "Kimi K3", "tool_call": True, "reasoning": True,
        "limit": {"context": 262144, "output": 16384},
    },
    "global.zai.glm-5.3": {
        "name": "GLM 5.3", "tool_call": True, "reasoning": True,
        "limit": {"context": 200000, "output": 16384},
    },
}

# OpenCode's built-in tool ids; the judge switches every one of them off and
# enables only the Artifact MCP tools named by its policy.
BUILTIN_TOOLS: tuple[str, ...] = (
    "bash", "edit", "write", "read", "glob", "grep", "list", "patch",
    "todowrite", "todoread", "webfetch", "task",
)
_VALID_EFFORTS = frozenset({"low", "medium", "high"})

# ---------------------------------------------------------------------------
# Exec configuration (module globals, env-seeded like the former backends)
# ---------------------------------------------------------------------------

OPENCODE_EXEC_PATH = os.environ.get("OPENCODE_EXEC_PATH", "opencode")
OPENCODE_EXEC_EFFORT = os.environ.get("OPENCODE_EXEC_EFFORT", "medium")


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except ValueError:
        return default


EXEC_EMPTY_RESPONSE_RETRIES = max(0, _parse_int(os.environ.get("EXEC_EMPTY_RESPONSE_RETRIES"), 1))


def set_empty_response_retries(value: int) -> None:
    global EXEC_EMPTY_RESPONSE_RETRIES
    EXEC_EMPTY_RESPONSE_RETRIES = max(0, int(value))


def configure_opencode_exec(
    *,
    path: str | None = None,
    effort: str | None = None,
    use_sdk: str | None = None,  # accepted for call-site parity; OpenCode is CLI-only
) -> None:
    """Pin the CLI path / reasoning effort for this process (blank = default)."""
    global OPENCODE_EXEC_PATH, OPENCODE_EXEC_EFFORT
    del use_sdk
    if path is not None:
        OPENCODE_EXEC_PATH = str(path).strip() or "opencode"
        os.environ["OPENCODE_EXEC_PATH"] = OPENCODE_EXEC_PATH
    if effort is not None:
        value = str(effort).strip().lower() or "medium"
        if value not in _VALID_EFFORTS:
            raise ValueError(
                f"opencode exec effort must be one of {sorted(_VALID_EFFORTS)}: {effort!r}"
            )
        OPENCODE_EXEC_EFFORT = value
        os.environ["OPENCODE_EXEC_EFFORT"] = OPENCODE_EXEC_EFFORT


def get_opencode_exec_config() -> dict[str, str | int]:
    return {
        "path": OPENCODE_EXEC_PATH,
        "effort": OPENCODE_EXEC_EFFORT,
        "empty_response_retries": EXEC_EMPTY_RESPONSE_RETRIES,
    }


# ---------------------------------------------------------------------------
# opencode.json
# ---------------------------------------------------------------------------


def provider_base_url() -> str:
    """The OpenAI-compatible root (``…/openai/v1``) — fail closed when unset."""
    url = os.environ.get(BASE_URL_ENV, "").strip()
    if not url:
        raise RuntimeError(
            f"{BASE_URL_ENV} is not set — the Launchpad runner/worker must provide the "
            "Bedrock OpenAI-compatible base URL (built with the partition helper)"
        )
    return url.rstrip("/")


def extra_providers(source: dict[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """The PROVIDERS_ENV table (``{}`` when unset/empty); a malformed value is a loud error."""
    env = os.environ if source is None else source
    raw = (env.get(PROVIDERS_ENV) or os.environ.get(PROVIDERS_ENV, "")).strip()
    if not raw:
        return {}
    table = json.loads(raw)
    if not isinstance(table, dict):
        raise ValueError(f"{PROVIDERS_ENV} must be a JSON object keyed by provider id")
    return {str(pid): dict(spec) for pid, spec in table.items() if pid != PROVIDER_ID}


def provider_for_model(model: str, source: dict[str, str] | None = None
                       ) -> tuple[str, dict[str, Any] | None, str]:
    """``(provider id, extra-provider spec or None for the Bedrock default, bare model)``."""
    model = str(model or "").strip()
    for pid, spec in extra_providers(source).items():
        if model.startswith(pid + "/"):
            return pid, spec, model[len(pid) + 1:]
        prefix = str(spec.get("id_prefix") or "")
        if prefix and model.startswith(prefix):
            return pid, spec, model[len(prefix):]
    if model.startswith(PROVIDER_ID + "/"):
        model = model[len(PROVIDER_ID) + 1:]
    return PROVIDER_ID, None, model


def model_ref(model: str) -> str:
    """``provider/model`` as OpenCode addresses it; idempotent on prefixed ids."""
    pid, _spec, bare = provider_for_model(model)
    return f"{pid}/{bare}"


def _bare_model(model: str) -> str:
    return provider_for_model(model)[2]


def _judge_mcp_server(policy: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The single required Artifact MCP server of a judge policy, or fail closed."""
    servers = policy.get("mcp_servers")
    if not isinstance(servers, dict) or len(servers) != 1:
        raise RuntimeError("judge policy must define exactly one required Artifact MCP server")
    name, spec = next(iter(servers.items()))
    if not isinstance(spec, dict) or spec.get("required") is not True:
        raise RuntimeError("judge Artifact MCP server must be marked required")
    command = spec.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(part, str) for part in command)
    ):
        raise RuntimeError("judge Artifact MCP server command must be a non-empty string argv")
    return str(name), spec


def rollout_permission(allow_file_edits: bool) -> dict[str, str]:
    """Spike §5 file-edit gate: edits/writes only when the caller allows them
    (bash stays on — tasks run scripts; same posture as the former Bash-only mode),
    never the network, never outside the project."""
    edits = "allow" if allow_file_edits else "deny"
    return {
        "read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "bash": "allow",
        "edit": edits, "write": edits,
        "webfetch": "deny", "websearch": "deny", "external_directory": "deny",
    }


def build_opencode_config(
    *,
    model: str,
    allow_file_edits: bool,
    policy: dict[str, Any] | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """The per-run ``opencode.json``.

    Rollout/taskgen: Bedrock provider + model, shell allowed (tasks run scripts), file
    edits only when the caller allows them, no web fetch. Judge (``policy``): the same
    provider, every built-in tool off, exactly one required stdio MCP server whose
    allowlisted tools are the only ones enabled, no project instructions, and a
    primary agent carrying the trusted system prompt.
    """
    pid, extra, bare = provider_for_model(model)
    providers: dict[str, Any] = {
        PROVIDER_ID: {
            "npm": PROVIDER_NPM,
            "name": PROVIDER_NAME,
            "options": {
                "baseURL": (base_url or provider_base_url()),
                # resolved by OpenCode at runtime from the process environment;
                # the key value is never materialised into this file
                "apiKey": "{env:" + API_KEY_ENV + "}",
            },
            "models": {
                **{mid: dict(spec) for mid, spec in MODEL_CATALOG.items()},
                **({} if extra else
                   {bare: dict(MODEL_CATALOG.get(bare, {"name": bare, "tool_call": True}))}),
            },
        }
    }
    if extra is not None:
        # LAUNCHPAD PATCH (N-R6): the model's own OpenAI-compatible provider, from the
        # table the Launchpad side passed; its key is `{env:<api_key_env>}` like Bedrock's.
        known = {str(mid): dict(spec) for mid, spec in (extra.get("models") or {}).items()}
        providers[pid] = {
            "npm": PROVIDER_NPM,
            "name": str(extra.get("name") or pid),
            "options": {
                "baseURL": str(extra["base_url"]).rstrip("/"),
                "apiKey": "{env:" + str(extra["api_key_env"]) + "}",
            },
            "models": {**known, bare: known.get(bare, {"name": bare, "tool_call": True})},
        }
    config: dict[str, Any] = {
        "$schema": CONFIG_SCHEMA_URL,
        "provider": providers,
        "model": f"{pid}/{bare}",
        # No git snapshots of the workspace (they live outside the project and cost
        # a copy per step); the rollout manifest diff is the platform's record.
        "snapshot": False,
        "permission": rollout_permission(allow_file_edits),
    }
    if policy is None:
        return config
    if policy.get("judge") is not True:
        raise ValueError("policy must be a judge backend policy dict")
    name, spec = _judge_mcp_server(policy)
    allowed = [str(tool) for tool in (spec.get("tools") or []) if str(tool)]
    tools: dict[str, bool] = {tool: False for tool in BUILTIN_TOOLS}
    tools.update({f"{name}_{tool}": True for tool in allowed})
    config["permission"] = {
        **{k: "deny" for k in rollout_permission(False)},
        "read": "deny",  # evidence only through the Artifact MCP
    }
    config["tools"] = tools
    config["mcp"] = {name: {"type": "local", "command": list(spec["command"]), "enabled": True}}
    config["instructions"] = []  # no AGENTS.md / rules / skills in the judge
    config["agent"] = {
        JUDGE_AGENT: {
            "mode": "primary",
            "prompt": str(policy.get("system_prompt") or ""),
            "tools": dict(tools),
            "permission": dict(config["permission"]),
        }
    }
    return config


def job_home(work_dir: str, home: str | None = None) -> str:
    """`<prediction dir>/.opencode-home` — beside the work_dir, never inside it — unless
    the caller names the home itself (``home``). N10v4-bug1: the AgentCore worker does,
    so OpenCode's sqlite db (``$XDG_DATA_HOME``) lives on local container disk even when
    the work_dir sits on the Runtime's session-storage mount, where sqlite locking fails
    with "database is locked"."""
    if home:
        return os.path.abspath(home)
    return os.path.join(os.path.dirname(os.path.abspath(work_dir).rstrip(os.sep)), HOME_DIRNAME)


def write_opencode_config(work_dir: str, config: dict[str, Any], *, home: str | None = None) -> str:
    home = job_home(work_dir, home)
    os.makedirs(home, exist_ok=True)
    path = os.path.join(home, CONFIG_FILENAME)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    return path


def prepare_work_dir(work_dir: str) -> None:
    """Make the work_dir OpenCode's project root (spike finding 4): without a `.git`
    the CLI walks the ancestors up to $HOME looking for `.agents`/`.opencode`,
    quadrupling the system prompt. `git init -q` is idempotent and `.git` is already
    skipped by the rollout manifest."""
    if os.path.isdir(os.path.join(work_dir, ".git")):
        return
    git = shutil.which("git")
    if git is None:
        return  # the CLI still runs; only the discovery walk is wider
    subprocess.run([git, "init", "-q"], cwd=work_dir, check=False, capture_output=True)


def ensure_ripgrep(cache_home: str) -> None:
    """Seed `$XDG_CACHE_HOME/opencode/bin/rg` from the system ripgrep (spike finding
    2): OpenCode otherwise downloads ripgrep from GitHub and glob/grep/skill fail."""
    target_dir = os.path.join(cache_home, "opencode", "bin")
    target = os.path.join(target_dir, "rg")
    if os.path.exists(target):
        return
    system_rg = shutil.which("rg")
    if system_rg is None:
        return
    os.makedirs(target_dir, exist_ok=True)
    try:
        os.symlink(system_rg, target)
    except OSError:
        shutil.copy2(system_rg, target)


def _host_suffix(url: str) -> str:
    host = urlparse(url).hostname or ""
    return ".".join(host.split(".")[-2:]) if host.count(".") >= 2 else host


def _no_proxy(base_url: str, source: dict[str, str] | None = None) -> str:
    env = os.environ if source is None else source
    explicit = (os.environ.get(NO_PROXY_ENV) or env.get(NO_PROXY_ENV, "")).strip()
    if explicit:
        return explicit
    # LAUNCHPAD PATCH (N-R6): the extra providers' hosts are reachable too
    hosts = [_host_suffix(base_url)] + [
        _host_suffix(str(spec.get("base_url") or "")) for spec in extra_providers(source).values()
    ]
    parts = list(dict.fromkeys(h for h in hosts if h)) + ["169.254.169.254", "localhost", "127.0.0.1"]
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Headless command + child environment
# ---------------------------------------------------------------------------


def build_command(
    *,
    path: str,
    model: str,
    prompt: str,
    work_dir: str | None = None,
    agent: str | None = None,
    images: list[str] | None = None,
) -> list[str]:
    """``opencode run --pure --format json --title skillopt --dir W --model p/m [--agent a]
    [-f image…] <prompt>`` (spike §2/§5). ``--pure``: no external plugins; ``--title``:
    skips the extra model call that names the session."""
    cmd = [str(path), "run", "--pure", "--format", "json", "--title", SESSION_TITLE]
    if work_dir:
        cmd.extend(["--dir", work_dir])
    cmd.extend(["--model", model_ref(model)])
    if agent:
        cmd.extend(["--agent", agent])
    for image in images or []:
        cmd.extend(["-f", image])
    cmd.append(prompt)
    return cmd


def subprocess_env(
    config_path: str,
    *,
    base: dict[str, str] | None = None,
    work_dir: str | None = None,
    base_url: str | None = None,
    home: str | None = None,
) -> dict[str, str]:
    """Allow-listed child env (spike §5/§7): inherited basics + the bearer, the config
    path, every OPENCODE_DISABLE_* switch, per-job XDG/TMPDIR under the job home (an
    explicit ``home`` wins — N10v4-bug1), and the dead proxy with NO_PROXY naming only
    the partition suffixes."""
    source = os.environ if base is None else base
    # LAUNCHPAD PATCH (N-R6): an extra provider's key variable is inherited like the bearer
    inherited = INHERITED_ENV + tuple(
        str(spec["api_key_env"]) for spec in extra_providers(source).values()
        if spec.get("api_key_env")
    )
    env = {key: source[key] for key in inherited if key in source}
    env[CONFIG_PATH_ENV] = config_path
    env.update(DISABLE_ENV)
    if home:
        home = os.path.abspath(home)
    elif work_dir is None:
        home = os.path.dirname(config_path)
    else:
        home = job_home(work_dir)
    for var, sub in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
        ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state"), ("TMPDIR", "tmp"),
    ):
        path = os.path.join(home, sub)
        os.makedirs(path, exist_ok=True)
        env[var] = path
    ensure_ripgrep(env["XDG_CACHE_HOME"])
    env["HTTPS_PROXY"] = DEAD_PROXY
    env["HTTP_PROXY"] = DEAD_PROXY
    env["NO_PROXY"] = _no_proxy(base_url or source.get(BASE_URL_ENV, ""), source)
    return env


# ---------------------------------------------------------------------------
# Event stream → transcript
# ---------------------------------------------------------------------------


@dataclass
class OpenCodeTranscript:
    """What SkillOpt's pipelines consume from one headless run."""

    turns: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    text_parts: list[str] = field(default_factory=list)
    final_message: str = ""
    usage: dict[str, int] = field(
        default_factory=lambda: {
            "input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0,
        }
    )
    errors: list[str] = field(default_factory=list)
    event_count: int = 0


def _iter_events(stdout: str):
    """One JSON object per line (the documented stream); a whole-document JSON
    object/array is accepted too; non-JSON lines (banners, warnings) are skipped."""
    text = (stdout or "").strip()
    if not text:
        return
    try:
        whole = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        whole = None
    if isinstance(whole, dict):
        yield whole
        return
    if isinstance(whole, list):
        for item in whole:
            if isinstance(item, dict):
                yield item
        return
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(event, dict):
            yield event


def _error_text(event: dict[str, Any]) -> str:
    part = event.get("part") if isinstance(event.get("part"), dict) else {}
    err = part.get("error") if isinstance(part.get("error"), dict) else event.get("error")
    if isinstance(err, dict):
        data = err.get("data") if isinstance(err.get("data"), dict) else {}
        message = str(data.get("message") or err.get("message") or err.get("name") or "")
        status = data.get("statusCode")
        if status and str(status) not in message:
            return f"{status} {message}".strip()
        return message
    if err:
        return str(err)
    return str(event.get("message") or event.get("data") or "unknown error")


def parse_events(stdout: str) -> OpenCodeTranscript:
    """Tolerant parser for ``--format json``: text parts, tool calls (with their
    completed/error state), per-step token usage (summed — ``step_finish`` tokens are
    per model call), errors. The final message is the text of the LAST step (after
    the final ``step_start``; spike §5); failing that, the last text part."""
    t = OpenCodeTranscript()
    current: dict[str, Any] | None = None
    for event in _iter_events(stdout):
        t.event_count += 1
        kind = str(event.get("type") or "")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind in ("step_start", "step-start"):
            current = {"texts": [], "tool_calls": []}
            t.turns.append(current)
        elif kind == "text":
            text = str(part.get("text") or event.get("text") or "")
            if text:
                t.text_parts.append(text)
                if current is not None:
                    current["texts"].append(text)
        elif kind in ("tool_use", "tool", "tool-invocation"):
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            call = {
                "tool": str(part.get("tool") or part.get("name") or ""),
                "status": str(state.get("status") or ""),
                "input": state.get("input") if isinstance(state.get("input"), dict) else {},
                "output": str(state.get("output") or ""),
                "error": str(state.get("error") or ""),
                "title": str(state.get("title") or ""),
            }
            t.tool_calls.append(call)
            if current is not None:
                current["tool_calls"].append(call)
        elif kind in ("step_finish", "step-finish"):
            tokens = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            t.usage["input"] += int(tokens.get("input") or 0)
            t.usage["output"] += int(tokens.get("output") or 0)
            t.usage["reasoning"] += int(tokens.get("reasoning") or 0)
            t.usage["cache_read"] += int(cache.get("read") or 0)
            t.usage["cache_write"] += int(cache.get("write") or 0)
        elif kind == "error":
            t.errors.append(_error_text(event))
    last_step_texts = t.turns[-1]["texts"] if t.turns else []
    if last_step_texts:
        t.final_message = "".join(last_step_texts).strip()
    elif t.text_parts:
        t.final_message = t.text_parts[-1].strip()
    return t


def usage_line(transcript: OpenCodeTranscript) -> str:
    """Token usage in the key shape ``codex_harness.extract_exec_usage`` already
    reads out of persisted raws, so results keep their usage columns. Reasoning
    tokens are billed as output and folded into ``output_tokens`` (spike §5); the
    separate ``reasoning_tokens`` is informational."""
    u = transcript.usage
    return json.dumps(
        {
            "type": "usage",
            "backend": OPENCODE_BACKEND,
            "input_tokens": int(u["input"]),
            "cache_creation_input_tokens": int(u["cache_write"]),
            "cache_read_input_tokens": int(u["cache_read"]),
            "output_tokens": int(u["output"]) + int(u["reasoning"]),
            "reasoning_tokens": int(u["reasoning"]),
        }
    )


def normalize_raw(*, stdout: str, stderr: str, transcript: OpenCodeTranscript) -> str:
    """The persisted raw: the event stream verbatim, stderr labelled, one usage line."""
    raw = (stdout or "").rstrip()
    if stderr and stderr.strip():
        raw = f"{raw}\n[stderr]\n{stderr.rstrip()}" if raw else f"[stderr]\n{stderr.rstrip()}"
    return f"{raw}\n{usage_line(transcript)}" if raw else usage_line(transcript)


def _exec_error(raw: str, error: str) -> str:
    marker = json.dumps({"type": "exec_error", "backend": OPENCODE_BACKEND, "error": error})
    return f"{raw.rstrip()}\n{marker}" if raw.strip() else marker


def build_trace_summary(raw: str, response: str) -> str:
    """Compact per-rollout summary for the optimizer's reflection prompts."""
    tools: list[str] = []
    errors: list[str] = []
    for event in _iter_events(raw):
        kind = str(event.get("type") or "")
        part = event.get("part") if isinstance(event.get("part"), dict) else {}
        if kind in ("tool_use", "tool", "tool-invocation"):
            state = part.get("state") if isinstance(part.get("state"), dict) else {}
            name = str(part.get("tool") or part.get("name") or "?")
            tools.append(name)
            if str(state.get("status") or "") == "error":
                errors.append(f"{name}: {str(state.get('error') or '').strip()[:200]}")
        elif kind == "error":
            errors.append(_error_text(event)[:200])
        elif kind == "exec_error":
            errors.append(str(event.get("error") or "")[:200])
    answer_format = "missing"
    if "<answer>" in (response or "").lower():
        answer_format = "tagged"
    elif (response or "").strip():
        answer_format = "plain_text"
    parts = [
        "OpenCode Trace Summary",
        f"- tool calls: {', '.join(tools) if tools else 'none'}",
        f"- tool/model errors: {' | '.join(errors[:3]) if errors else 'none'}",
        f"- final answer format: {answer_format}",
        f"- final response chars: {len(response or '')}",
    ]
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Running the CLI
# ---------------------------------------------------------------------------


def _persist(work_dir: str, raw: str, response: str) -> None:
    from skillopt.model.codex_harness import _persist_artifacts

    _persist_artifacts(
        work_dir=work_dir,
        raw=raw,
        response=response,
        prefix=ARTIFACT_PREFIX,
        summary_builder=build_trace_summary,
    )


def _run_once(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    allow_file_edits: bool,
    images: list[str] | None = None,
    home: str | None = None,
) -> tuple[str, str]:
    config = get_opencode_exec_config()
    work_dir = os.path.abspath(work_dir)
    prepare_work_dir(work_dir)
    config_path = write_opencode_config(
        work_dir, build_opencode_config(model=model, allow_file_edits=allow_file_edits),
        home=home,
    )
    cmd = build_command(
        path=str(config["path"]), model=model, prompt=prompt, work_dir=work_dir, images=images
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(config_path, work_dir=work_dir, home=home),
            stdin=subprocess.DEVNULL,  # spike finding 1: the CLI blocks on a non-TTY stdin
        )
    except subprocess.TimeoutExpired as exc:
        stdout = str(exc.stdout or "")
        transcript = parse_events(stdout)
        raw = normalize_raw(stdout=stdout, stderr=str(exc.stderr or ""), transcript=transcript)
        return "", _exec_error(raw, f"timed out after {timeout} seconds")
    stdout = proc.stdout or ""
    transcript = parse_events(stdout)
    raw = normalize_raw(stdout=stdout, stderr=proc.stderr or "", transcript=transcript)
    if transcript.errors:
        # API/CLI errors end the run without a final answer; mark them so the
        # caller's empty-response retry loop engages and the reason is visible.
        return "", _exec_error(raw, "; ".join(transcript.errors))
    if proc.returncode != 0 and not transcript.final_message:
        detail = (proc.stderr or "").strip() or "no diagnostic output"
        return "", _exec_error(raw, f"process exited with code {proc.returncode}: {detail}")
    return transcript.final_message, raw


def _run_judge(
    *, work_dir: str, prompt: str, model: str, timeout: int, policy: dict[str, Any],
    home: str | None = None,
) -> tuple[str, str]:
    """Restricted judge call: no built-in tools, only the Artifact MCP, trusted system
    prompt as a primary agent, no project instructions. Errors raise — the judge has
    no retry loop and an empty verdict would only fail later with the cause hidden."""
    config = get_opencode_exec_config()
    work_dir = os.path.abspath(work_dir)
    prepare_work_dir(work_dir)
    config_path = write_opencode_config(
        work_dir, build_opencode_config(model=model, allow_file_edits=False, policy=policy),
        home=home,
    )
    cmd = build_command(
        path=str(config["path"]), model=model, prompt=prompt, work_dir=work_dir, agent=JUDGE_AGENT
    )
    try:
        proc = subprocess.run(
            cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=subprocess_env(config_path, work_dir=work_dir, home=home),
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"opencode judge client timed out after {timeout}s") from exc
    stdout = proc.stdout or ""
    transcript = parse_events(stdout)
    raw = normalize_raw(stdout=stdout, stderr=proc.stderr or "", transcript=transcript)
    if transcript.errors:
        raise RuntimeError(
            f"opencode judge client reported an error: {'; '.join(transcript.errors)[:500]}"
        )
    if proc.returncode != 0:
        raise RuntimeError(
            f"opencode judge client exited {proc.returncode}: {(proc.stderr or stdout)[:2000]}"
        )
    return transcript.final_message, raw


def run_opencode_exec(
    *,
    work_dir: str,
    prompt: str,
    model: str,
    timeout: int,
    images: list[str] | None = None,
    data_dirs: list[str] | None = None,
    allowed_tools: list[str] | str | None = None,
    permission_mode: str | None = None,
    allow_file_edits: bool = False,
    policy: dict[str, Any] | None = None,
    home: str | None = None,
) -> tuple[str, str]:
    """Execute one task with the OpenCode CLI; returns ``(response, raw)``.

    ``home`` (worker-side only, N10v4-bug1) places the per-job OpenCode home — XDG dirs,
    TMPDIR, ``opencode.json`` — somewhere other than beside the work_dir; the remote-runner
    path ignores it (the worker picks its own local-disk home).

    Keyword surface kept from the executor it replaces so every call site is
    unchanged: ``allowed_tools`` / ``permission_mode`` are expressed through the
    permission block (edits on/off); ``images`` ride ``-f``; ``data_dirs`` outside the
    work_dir are refused by the remote runner and ignored here (OpenCode confines its
    tools to the project dir — ``external_directory=deny``).
    """
    del allowed_tools, permission_mode, data_dirs
    if policy is not None:
        if not (isinstance(policy, dict) and policy.get("judge") is True):
            raise ValueError("policy must be a judge backend policy dict")
        return _run_judge(
            work_dir=work_dir, prompt=prompt, model=model, timeout=timeout, policy=policy,
            home=home,
        )

    from skillopt.model import agentcore_runner

    if agentcore_runner.is_enabled():
        response, raw = agentcore_runner.run_remote_exec(
            backend=OPENCODE_BACKEND,
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            images=images,
            data_dirs=None,
            exec_kwargs={"allow_file_edits": allow_file_edits},
            exec_config=get_opencode_exec_config(),
        )
        _persist(work_dir, raw, response)
        return response, raw

    from skillopt.model.codex_harness import _exec_prompt, _retry_prompt

    retries = int(get_opencode_exec_config().get("empty_response_retries", 0) or 0)
    last_response = ""
    all_raw: list[str] = []
    for attempt in range(retries + 1):
        attempt_prompt = _retry_prompt(
            _exec_prompt(prompt, allow_file_edits=allow_file_edits), attempt
        )
        response, raw = _run_once(
            work_dir=work_dir,
            prompt=attempt_prompt,
            model=model,
            timeout=timeout,
            allow_file_edits=allow_file_edits,
            images=images,
            home=home,
        )
        all_raw.append(f"===== OPENCODE ATTEMPT {attempt + 1} =====\n{raw}")
        last_response = response
        if response.strip():
            break
    combined = "\n\n".join(all_raw)
    _persist(work_dir, combined, last_response)
    return last_response, combined


# ---------------------------------------------------------------------------
# The executor seam
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutorBackend(Protocol):
    """What the vendored pipelines need from an exec agent CLI. One implementation
    (OpenCode); a future CLI implements this protocol in its own single module."""

    name: str

    def run(
        self,
        *,
        work_dir: str,
        prompt: str,
        model: str,
        timeout: int,
        allow_file_edits: bool = False,
        policy: dict[str, Any] | None = None,
    ) -> tuple[str, str]: ...

    def exec_config(self) -> dict[str, Any]: ...

    def cli_binary(self) -> str: ...


class OpenCodeBackend:
    name = OPENCODE_BACKEND

    def run(
        self,
        *,
        work_dir: str,
        prompt: str,
        model: str,
        timeout: int,
        allow_file_edits: bool = False,
        policy: dict[str, Any] | None = None,
    ) -> tuple[str, str]:
        return run_opencode_exec(
            work_dir=work_dir,
            prompt=prompt,
            model=model,
            timeout=timeout,
            allow_file_edits=allow_file_edits,
            policy=policy,
        )

    def exec_config(self) -> dict[str, Any]:
        return get_opencode_exec_config()

    def cli_binary(self) -> str:
        return str(get_opencode_exec_config()["path"])


BACKEND: ExecutorBackend = OpenCodeBackend()

_USAGE_LINE_RE = re.compile(r'^\{"type": "usage"', re.MULTILINE)


def has_usage_line(raw: str) -> bool:
    return bool(_USAGE_LINE_RE.search(raw or ""))
