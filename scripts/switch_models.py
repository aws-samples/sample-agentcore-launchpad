#!/usr/bin/env python3
"""Pick working Bedrock models for the platform's own model settings.

Some AWS accounts cannot invoke OpenAI or Anthropic models on Bedrock. Every
model the platform itself calls is a ``LAUNCHPAD_<FIELD>`` setting
(backend/app/core/config.py); this script probes the account with real, tiny
Converse calls (plus an AgentCore CreateEvaluator test for judge models), picks
the first working candidate per role and writes the result to a systemd
EnvironmentFile that the backend unit loads.

Usage (on the instance, from the backend venv)::

    cd backend && uv run python ../scripts/switch_models.py --dry-run
    cd backend && sudo -E uv run python ../scripts/switch_models.py --restart
    ... --prefer global.zai.glm-5.3          # try these right after each default
    ... --roles judge,actor --json           # subset + machine-readable result
    ... --update-evaluators                  # move stale ledger evaluators

It also runs from a laptop (any boto3 credentials for the target account);
point ``--env-file`` at a writable path there.

Behaviour:
* Candidates per role: the current value (env file, then process env), the
  built-in default, ``--prefer`` ids that fit the role's family constraint,
  then curated alternatives. Ids the Region does not offer are skipped.
* Claude-only roles (Claude Agent SDK, Studio AI-fix, Skill Lab claude target)
  only get Claude candidates, the codex target only OpenAI ones; when none
  works the role is reported UNAVAILABLE, its key is not written and it does
  not affect the exit code (that feature simply needs those models).
* The env file gets every role that resolved (not only the ones that differ
  from the default), so it records the full decision; unrelated lines are kept.
  ``LAUNCHPAD_PROMPT_OPT_MODELS`` is always written as a compact JSON array of
  the working reflection models, default first.
* Fields saved on the console's Models page (ledger table ``platform_settings``,
  precedence console > env > default) are reported: the env value written for
  them only takes effect once the console override is reset. The script never
  modifies that table.
* Launchpad-created custom evaluators (ledger table ``resource_creators``) whose
  judge model does not work are always listed; ``--update-evaluators`` moves
  them to the chosen judge model (full-replace UpdateEvaluator).

Region: --region, else LAUNCHPAD_REGION, else config/launchpad.yaml ``region``
(the backend's own order), else AWS_REGION / AWS_DEFAULT_REGION, else us-west-2
(the backend default).

Exit codes: 0 every unrestricted role resolved, 2 some role (or evaluator
update) failed, 1 usage / credential / write errors.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = "/etc/launchpad-models.env"
HEADER = "# switch_models.py"
_PREFIX = re.compile(r"^(us|eu|apac|global|jp|au|ca|us-gov)\.")

# Model ids reused across the candidate lists.
SONNET5, SONNET55 = "global.anthropic.claude-sonnet-5", "global.anthropic.claude-sonnet-5-5"
OPUS5, OPUS55 = "global.anthropic.claude-opus-5", "global.anthropic.claude-opus-5-5"
HAIKU55, SONNET46 = "global.anthropic.claude-haiku-5-5", "global.anthropic.claude-sonnet-4-6"
HAIKU45 = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
GPT6_SOL, GPT6_ASTRA = "global.openai.gpt-6-sol", "global.openai.gpt-6-astra"
GPT6_LUNA, GPT61_SOL = "global.openai.gpt-6-luna", "global.openai.gpt-6.1-sol"
# GPT-6.1 Terra: Converse + tools (auto and forced any) + reasoning.effort live-probed
# 2026-11-02 (global + us profiles, us-east-1 / us-west-2); not yet probed as an
# AgentCore judge, so it is only offered to roles that do not judge.
GPT61_TERRA = "global.openai.gpt-6.1-terra"
GLM = ["global.zai.glm-5.3", "us.zai.glm-5.3"]
KIMI = ["global.moonshotai.kimi-k3", "us.moonshotai.kimi-k3"]
NOVA_PRO = "us.amazon.nova-pro-v1:0"  # (nova-premier: end of life, 2026-10)
NOVA_LITE, DEEPSEEK = "global.amazon.nova-2-lite-v1:0", "deepseek.v3.2"
QWEN = "qwen.qwen3-next-80b-a3b"
# DeepSeek / Kimi ids that passed every check (system prompt, tools auto/any, streaming +
# tool, AgentCore judge acceptance) live on 2026-10-12; deepseek.v3-v1:0 is us-west-2 only
# (the Region filter drops it elsewhere). Kimi K3 / K2 Thinking emit reasoningContent.
DS_KIMI = [DEEPSEEK, *KIMI, "moonshotai.kimi-k2.5", "moonshot.kimi-k2-thinking",
           "deepseek.v3-v1:0"]
# Converse + judge accepted, but toolConfig refused ("This model doesn't support tool use")
# -> only for roles without tools (platform, judge, judge_fallback, prompt_opt).
NO_TOOL_MODELS = {"us.deepseek.r1-v1:0"}
R1 = "us.deepseek.r1-v1:0"
TOOL_KINDS = {"tools", "tool_any"}

ANY, CLAUDE, OPENAI, OPENAI_OR_CLAUDE = None, {"anthropic"}, {"openai"}, {"openai", "anthropic"}
# codex's amazon-bedrock-runtime provider also drives Kimi (live-verified 2026-11-02)
OPENAI_OR_KIMI = {"openai", "moonshotai"}


@dataclass
class Role:
    name: str
    field: str  # Settings field; env var = LAUNCHPAD_<FIELD_UPPER>
    default: str
    kind: str  # converse | tools (toolChoice auto) | tool_any (forced) | judge
    candidates: list[str]
    families: set[str] | None = ANY
    note: str = ""

    @property
    def env(self) -> str:
        return f"LAUNCHPAD_{self.field.upper()}"

    @property
    def restricted(self) -> bool:
        return self.families is not None


_JUDGES = [GPT6_SOL, SONNET55, SONNET5, *GLM, *DS_KIMI, NOVA_PRO, R1]
_CLAUDE_TOOLS = [SONNET5, SONNET55, HAIKU55, OPUS5, SONNET46, "us.anthropic.claude-sonnet-5"]
PROMPT_OPT_DEFAULT_LIST = [OPUS5, SONNET5, SONNET46, "us.openai.gpt-5.6-sol"]
ROLES = [
    Role("platform", "platform_model_id", SONNET5, "converse",
         [SONNET5, SONNET55, HAIKU55, GPT6_SOL, GPT6_LUNA, *GLM, *DS_KIMI, NOVA_PRO, NOVA_LITE,
          QWEN, R1], note="intent clustering + suggested questions"),
    Role("agent_default", "agent_default_model_id", GLM[0], "tools",
         [*GLM, *DS_KIMI, NOVA_PRO, QWEN, SONNET5, GPT6_SOL, GPT61_TERRA],
         note="default model of new agents"),
    Role("judge", "judge_model_id", GPT6_SOL, "judge", _JUDGES,
         note="LLM-as-judge of custom/derived evaluators"),
    Role("judge_fallback", "judge_fallback_model_id", SONNET55, "judge", [SONNET55, *_JUDGES],
         note="retry model when AgentCore refuses the judge; '' = none"),
    Role("assistant", "assistant_model_id", GPT6_SOL, "tools",
         [GPT6_SOL, SONNET55, SONNET5, *GLM, *DS_KIMI, NOVA_PRO, QWEN],
         note="default model of agents the architect proposes"),
    Role("architect", "architect_model_id", GPT6_ASTRA, "tools",
         [GPT6_ASTRA, GPT6_SOL, OPUS55, SONNET55, SONNET5, *GLM, *DS_KIMI, NOVA_PRO],
         note="system architect agent (new installs)"),
    Role("actor", "actor_model_id", GPT6_LUNA, "tool_any",
         [GPT6_LUNA, HAIKU55, SONNET5, HAIKU45, *GLM, NOVA_PRO, NOVA_LITE, *DS_KIMI, QWEN],
         note="simulated user; needs forced toolChoice any"),
    Role("studio_fallback", "studio_fallback_model_id", SONNET5, "tools",
         [SONNET5, SONNET55, GPT6_SOL, *GLM, *DS_KIMI, NOVA_PRO],
         note="Strands Studio model-less nodes"),
    Role("claude_sdk", "claude_sdk_model_id", SONNET5, "converse", _CLAUDE_TOOLS, CLAUDE,
         "Claude Agent SDK container method (Claude-only feature)"),
    Role("codegen", "codegen_model", SONNET5, "converse", _CLAUDE_TOOLS, CLAUDE,
         "Studio AI-fix via Claude Agent SDK (Claude-only feature)"),
    Role("prompt_opt", "prompt_opt_default_model_id", OPUS5, "converse",
         [OPUS5, OPUS55, SONNET55, SONNET5, SONNET46, "us.openai.gpt-5.6-sol", GPT6_SOL, *GLM,
          *DS_KIMI, NOVA_PRO, QWEN, R1], note="GEPA-lite reflection (+ PROMPT_OPT_MODELS)"),
    Role("skill_lab_judge", "skill_lab_judge_model_id", GPT61_SOL, "converse",
         [GPT61_SOL, GPT6_SOL, OPUS55, SONNET55, SONNET5], OPENAI_OR_CLAUDE,
         "agentic judge runs on the codex (openai) or claude CLI only"),
    Role("skill_lab_target", "skill_lab_target_model_id", OPUS55, "converse",
         [OPUS55, OPUS5, SONNET55, SONNET5], CLAUDE, "Skill Lab claude CLI target (Claude-only)"),
    Role("skill_lab_codex_target", "skill_lab_codex_target_model_id", GPT61_SOL, "converse",
         [GPT61_SOL, GPT61_TERRA, GPT6_ASTRA, GPT6_SOL, KIMI[0]], OPENAI_OR_KIMI,
         "Skill Lab codex CLI target (OpenAI or Kimi)"),
]
PROMPT_OPT_MODELS_ENV = "LAUNCHPAD_PROMPT_OPT_MODELS"
PROMPT_OPT_LIST_MAX = 4


def family(model_id: str) -> str:
    return base(model_id).split(".", 1)[0]


def base(model_id: str) -> str:
    """The model behind an id: global.zai.glm-5.3 and us.zai.glm-5.3 are one model."""
    return _PREFIX.sub("", model_id)


def _short(text: str, limit: int = 140) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


@dataclass
class Probe:
    ok: bool
    code: str = ""
    message: str = ""

    def describe(self) -> str:
        return "ok" if self.ok else f"{self.code}: {_short(self.message)}".rstrip(": ")


# ─── AWS access ──────────────────────────────────────────────────────────────


class Prober:
    """Region catalogue + cached Converse / evaluator probes."""

    def __init__(self, session: Any, region: str, simulate: set[str], log=print):
        from botocore.config import Config

        cfg = Config(connect_timeout=5, read_timeout=60, retries={"max_attempts": 2})
        self.region, self.simulate, self.log = region, simulate, log
        self.bedrock = session.client("bedrock", region_name=region, config=cfg)
        self.runtime = session.client("bedrock-runtime", region_name=region, config=cfg)
        self.control = session.client("bedrock-agentcore-control", region_name=region, config=cfg)
        self.cache: dict[tuple[str, str], Probe] = {}
        self.offered: set[str] | None = None
        self.leaked: list[str] = []

    def load_catalogue(self) -> None:
        offered: set[str] = set()
        try:
            token = None
            while True:
                kw = {"maxResults": 100, **({"nextToken": token} if token else {})}
                page = self.bedrock.list_inference_profiles(**kw)
                summaries = page.get("inferenceProfileSummaries", [])
                offered |= {p["inferenceProfileId"] for p in summaries}
                token = page.get("nextToken")
                if not token:
                    break
            models = self.bedrock.list_foundation_models(byInferenceType="ON_DEMAND")
            offered |= {m["modelId"] for m in models.get("modelSummaries", [])}
        except Exception as exc:  # catalogue is best effort
            self.log(f"warning: cannot list Bedrock models ({_err(exc)[0]}); "
                     "not filtering by Region")
            return
        self.offered = offered

    def is_offered(self, model_id: str) -> bool:
        return self.offered is None or model_id in self.offered

    def probe(self, model_id: str, kind: str) -> Probe:
        if kind == "judge":
            first = self.probe(model_id, "converse")
            return first if not first.ok else self._cached(model_id, "evaluator")
        return self._cached(model_id, kind)

    def _cached(self, model_id: str, kind: str) -> Probe:
        key = (model_id, kind)
        if key not in self.cache:
            if family(model_id) in self.simulate:
                result = Probe(False, "AccessDeniedException", "simulated (--simulate-unavailable)")
            else:
                result = self._evaluator(model_id) if kind == "evaluator" else self._converse(
                    model_id, kind)
            self.cache[key] = result
            self.log(f"  probe {kind:<9} {model_id}: {result.describe()}")
        return self.cache[key]

    def _converse(self, model_id: str, kind: str) -> Probe:
        kw: dict[str, Any] = {
            "modelId": model_id,
            "messages": [{"role": "user", "content": [{"text": "Reply with the word OK."}]}],
            "inferenceConfig": {"maxTokens": 16},
        }
        if kind in TOOL_KINDS:  # tools: toolChoice auto (= /api/model-probe "tool_use")
            kw["inferenceConfig"] = {"maxTokens": 256 if kind == "tool_any" else 64}
            kw["toolConfig"] = {
                "tools": [{"toolSpec": {
                    "name": "reply",
                    "description": "Send the reply.",
                    "inputSchema": {"json": {"type": "object",
                                             "properties": {"text": {"type": "string"}},
                                             "required": ["text"]}},
                }}],
                "toolChoice": {"any": {}} if kind == "tool_any" else {"auto": {}},
            }
        try:
            self.runtime.converse(**kw)
            return Probe(True)
        except Exception as exc:  # every failure is a probe result
            return Probe(False, *_err(exc))

    def _evaluator(self, model_id: str) -> Probe:
        """CreateEvaluator test-invokes the judge model; the probe is always deleted."""
        name = f"zz_probe_{uuid.uuid4().hex[:10]}"
        evaluator_id = ""
        try:
            created = self.control.create_evaluator(
                evaluatorName=name,
                description="switch_models.py probe (deleted immediately)",
                level="TRACE",
                evaluatorConfig={"llmAsAJudge": {
                    "instructions": "Rate the response {assistant_turn} given {context}.",
                    "ratingScale": {"numerical": [
                        {"value": 0.0, "label": "Bad", "definition": "bad"},
                        {"value": 1.0, "label": "Good", "definition": "good"},
                    ]},
                    "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": model_id}},
                }},
                clientToken=str(uuid.uuid4()),
            )
            evaluator_id = created.get("evaluatorId", "")
            return Probe(True)
        except Exception as exc:
            if not isinstance(getattr(exc, "response", None), dict):
                # no service answer (timeout, connection drop): the create may have landed
                evaluator_id = self._find_by_name(name)
            return Probe(False, *_err(exc))
        finally:
            if evaluator_id:
                self._delete_probe(evaluator_id)

    def _find_by_name(self, name: str) -> str:
        try:
            token = None
            while True:
                page = self.control.list_evaluators(
                    maxResults=100, **({"nextToken": token} if token else {}))
                for item in page.get("evaluators", []):
                    if item.get("evaluatorName") == name:
                        return item.get("evaluatorId", "")
                token = page.get("nextToken")
                if not token:
                    break
        except Exception as exc:
            self.log(f"  WARNING: cannot check for a leftover probe evaluator {name}: "
                     f"{_err(exc)[0]}")
        return ""

    def _delete_probe(self, evaluator_id: str) -> None:
        try:
            self.control.delete_evaluator(evaluatorId=evaluator_id)
        except Exception as exc:
            self.log(f"  WARNING: probe evaluator {evaluator_id} not deleted: {_err(exc)[0]}"
                     " -- delete it by hand")
            self.leaked.append(evaluator_id)


def _err(exc: Exception) -> tuple[str, str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict) and "Error" in response:
        return response["Error"].get("Code", type(exc).__name__), response["Error"].get(
            "Message", "")
    return type(exc).__name__, str(exc)


# ─── Choosing ────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    role: Role
    current: str
    chosen: str | None  # None = UNAVAILABLE
    why: str
    tried: list[dict[str, Any]] = field(default_factory=list)
    extra: dict[str, str] = field(default_factory=dict)  # extra env keys to write
    override: str | None = None  # console Models-page value (platform_settings)

    def as_dict(self) -> dict[str, Any]:
        return {"role": self.role.name, "env": self.role.env, "default": self.role.default,
                "current": self.current, "chosen": self.chosen, "why": self.why,
                "restricted": self.role.restricted, "tried": self.tried, "extra": self.extra,
                "console_override": self.override}


def candidates(role: Role, current: str, prefer: list[str]) -> list[str]:
    fits = [m for m in prefer if role.families is None or family(m) in role.families]
    out: list[str] = []
    for model in [current, role.default, *fits, *role.candidates]:
        if role.kind in TOOL_KINDS and model in NO_TOOL_MODELS:
            continue  # known to refuse toolConfig
        if model and model not in out:
            out.append(model)
    return out


def _try(prober: Prober, role: Role, models: list[str], skip: str | None = None,
         want: int = 1) -> tuple[list[str], list[dict[str, Any]]]:
    """First `want` working models (in order, one per base model) + every attempt.
    `skip`: a model whose base model is excluded (the fallback must differ from the judge)."""
    working: list[str] = []
    tried: list[dict[str, Any]] = []
    for model in models:
        if (skip and base(model) == base(skip)) or base(model) in map(base, working):
            continue
        if not prober.is_offered(model):
            tried.append({"model": model, "result": f"not offered in {prober.region}"})
            continue
        probe = prober.probe(model, role.kind)
        tried.append({"model": model, "result": probe.describe(), "ok": probe.ok})
        if probe.ok:
            working.append(model)
            if len(working) >= want:
                break
    return working, tried


def _why(current: str, default: str, chosen: str, tried: list[dict[str, Any]]) -> str:
    if chosen == current:
        return "default works" if current == default else "current env value works"
    first = next((t["result"] for t in tried if t["model"] == current), "not probed")
    return f"{current} unavailable: {first}; first working alternative"


def decide(prober: Prober, roles: list[Role], current: dict[str, str],
           prefer: list[str]) -> list[Decision]:
    decisions: list[Decision] = []
    chosen_judge: str | None = None
    for role in roles:
        cur = current.get(role.env, role.default)
        models = candidates(role, cur, prefer)
        if role.name == "judge_fallback":
            judge = chosen_judge or current.get("LAUNCHPAD_JUDGE_MODEL_ID", GPT6_SOL)
            found, tried = _try(prober, role, models, skip=judge)
            pick = found[0] if found else ""
            why = (_why(cur, role.default, pick, tried) if pick
                   else "no working judge besides the chosen judge: no fallback")
            decisions.append(Decision(role, cur, pick, why, tried))
            continue
        want = PROMPT_OPT_LIST_MAX if role.name == "prompt_opt" else 1
        if role.name == "prompt_opt":
            listed = _json_list(current.get(PROMPT_OPT_MODELS_ENV)) or PROMPT_OPT_DEFAULT_LIST
            models = candidates(role, cur, [*listed, *prefer])
        found, tried = _try(prober, role, models, want=want)
        if not found:
            why = "no candidate works" + (f" ({role.note})" if role.restricted else "")
            decisions.append(Decision(role, cur, None, why, tried))
            continue
        decision = Decision(role, cur, found[0], _why(cur, role.default, found[0], tried), tried)
        if role.name == "judge":
            chosen_judge = found[0]
        if role.name == "prompt_opt":
            decision.extra[PROMPT_OPT_MODELS_ENV] = json.dumps(found, separators=(",", ":"))
        decisions.append(decision)
    return decisions


def _json_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "")
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


# ─── Env file ────────────────────────────────────────────────────────────────


def read_env_file(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        print(f"warning: cannot read {path} ({exc.strerror}); ignoring it", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def render_env_file(existing: str, values: dict[str, str], region: str) -> str:
    """Existing lines minus our keys / old header, then a fresh managed block."""
    kept = []
    for line in existing.splitlines():
        key = line.split("=", 1)[0].strip()
        if line.startswith(HEADER) or ("=" in line and key in values):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    block = [f"{HEADER} -- platform models probed {stamp} in {region}",
             f"{HEADER} -- re-run scripts/switch_models.py to refresh; edits to these keys"
             " are replaced"]
    block += [f"{k}={v}" for k, v in values.items()]
    return "\n".join([*kept, *([""] if kept else []), *block]) + "\n"


def write_env_file(path: Path, values: dict[str, str], region: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    content = render_env_file(existing, values, region)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ─── Ledger evaluators ───────────────────────────────────────────────────────


def database_url(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("LAUNCHPAD_DATABASE_URL"):
        return os.environ["LAUNCHPAD_DATABASE_URL"]
    try:  # optional: the backend's own resolution (yaml + env); not needed otherwise
        sys.path.insert(0, str(REPO_ROOT / "backend"))
        from app.core.config import get_settings

        return get_settings().database_url
    except Exception:  # app not importable (laptop)
        return f"sqlite:///{REPO_ROOT / 'data' / 'launchpad.db'}"


def _query(url: str, sql: str, params: dict[str, str]) -> list[tuple]:
    """Read-only ledger query (stdlib sqlite3; SQLAlchemy for other databases)."""
    if url.startswith("sqlite:///"):
        import sqlite3

        path = url[len("sqlite:///"):]
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
            return conn.execute(sql, params).fetchall()
    from sqlalchemy import create_engine, text

    with create_engine(url).connect() as conn:
        return [tuple(r) for r in conn.execute(text(sql), params).fetchall()]


def ledger_evaluator_ids(url: str, account: str, region: str) -> list[str]:
    """Evaluator ids the console created in (account, region) -- resource_creators."""
    rows = _query(url, "SELECT rc.resource_id FROM resource_creators rc JOIN workspaces w"
                  " ON w.id = rc.workspace_id WHERE rc.kind = 'evaluator'"
                  " AND w.account_id = :a AND w.region = :r ORDER BY rc.resource_id",
                  {"a": account, "r": region})
    return [r[0] for r in rows if r[0] and not str(r[0]).startswith("Builtin.")]


def console_overrides(url: str) -> dict[str, str]:
    """Settings field -> value saved on the console's Models page (platform_settings).

    Precedence there is console > env > default, so an env value this script writes
    does not take effect for such a field until the override is reset. Read only."""
    return {str(k): str(v) for k, v in _query(url, "SELECT key, value FROM platform_settings",
                                              {}) if v not in (None, "")}


def _judge_model(detail: dict[str, Any]) -> tuple[str, str]:
    config = detail.get("evaluatorConfig") or {}
    for kind in ("llmAsAJudge", "derived"):
        if kind in config:
            model = ((config[kind].get("modelConfig") or {}).get("bedrockEvaluatorModelConfig")
                     or {}).get("modelId", "")
            return kind, model
    return "", ""


def check_evaluators(prober: Prober, ids: list[str], target: str | None,
                     apply: bool) -> tuple[list[dict[str, Any]], bool]:
    rows, failed = [], False
    for evaluator_id in ids:
        try:
            detail = prober.control.get_evaluator(evaluatorId=evaluator_id)
        except Exception as exc:
            rows.append({"id": evaluator_id, "status": f"get failed: {_err(exc)[0]}"})
            continue
        kind, model = _judge_model(detail)
        row = {"id": evaluator_id, "name": detail.get("evaluatorName", ""), "kind": kind,
               "before": model, "after": model}
        if not model:
            row["status"] = "no judge model (code-based); skipped"
        elif prober.is_offered(model) and prober.probe(model, "judge").ok:
            row["status"] = "judge model works"
        elif not target:
            row["status"] = "STALE; no working judge to move to"
            failed = True
        elif not apply:
            row["status"] = f"STALE; --update-evaluators would move it to {target}"
        else:
            config = json.loads(json.dumps(detail["evaluatorConfig"]))
            config[kind]["modelConfig"] = {"bedrockEvaluatorModelConfig": {
                **config[kind]["modelConfig"]["bedrockEvaluatorModelConfig"], "modelId": target}}
            kw: dict[str, Any] = {"evaluatorId": evaluator_id, "evaluatorConfig": config,
                                  "clientToken": str(uuid.uuid4())}
            for key in ("description", "level"):
                if detail.get(key):
                    kw[key] = detail[key]
            try:
                prober.control.update_evaluator(**kw)
                row.update(after=target, status="updated")
            except Exception as exc:
                row["status"] = f"update failed: {' '.join(_err(exc))}"
                failed = True
        rows.append(row)
    return rows, failed


# ─── CLI ─────────────────────────────────────────────────────────────────────


def resolve_region(explicit: str | None) -> str:
    if explicit:
        return explicit
    if os.environ.get("LAUNCHPAD_REGION"):
        return os.environ["LAUNCHPAD_REGION"]
    yaml_file = REPO_ROOT / "config" / "launchpad.yaml"
    if yaml_file.is_file():
        match = re.search(r"^region:\s*['\"]?([a-z0-9-]+)", yaml_file.read_text("utf-8"), re.M)
        if match:
            return match.group(1)
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-west-2"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Probe Bedrock and pick working models for the platform's model settings.")
    p.add_argument("--region", help="AWS Region (default: LAUNCHPAD_REGION, launchpad.yaml, "
                   "AWS_REGION, us-west-2)")
    p.add_argument("--dry-run", action="store_true", help="probe and print; change nothing")
    p.add_argument("--prefer", default="", help="comma-separated model ids to try right after "
                   "each role's default (if they fit the role's family)")
    p.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                   help=f"systemd EnvironmentFile to write (default {DEFAULT_ENV_FILE})")
    p.add_argument("--restart", action="store_true",
                   help="systemctl restart launchpad-backend after writing")
    p.add_argument("--update-evaluators", action="store_true",
                   help="move ledger evaluators with a stale judge model to the chosen judge")
    p.add_argument("--roles", default="", help="comma-separated subset of: "
                   + ",".join(r.name for r in ROLES))
    p.add_argument("--json", action="store_true", help="also print a JSON result")
    p.add_argument("--database-url", help="ledger URL (default: the backend's database_url)")
    p.add_argument("--simulate-unavailable", default="", help=argparse.SUPPRESS)
    return p.parse_args(argv)


def _split(raw: str) -> list[str]:
    return [x.strip() for x in raw.split(",") if x.strip()]


def restart_backend() -> str:
    cmd = ["systemctl", "restart", "launchpad-backend"]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n", *cmd]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"restart failed: {exc}"
    if done.returncode:
        return f"restart failed ({done.returncode}): {_short(done.stderr or done.stdout)}"
    return "launchpad-backend restarted"


def main(argv: list[str] | None = None, session: Any = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as exc:  # argparse: 0 for --help, 2 for usage errors -> 1
        return 0 if exc.code in (0, None) else 1
    names = {r.name for r in ROLES}
    wanted = _split(args.roles)
    if unknown := set(wanted) - names:
        print(f"error: unknown role(s) {', '.join(sorted(unknown))}", file=sys.stderr)
        return 1
    roles = [r for r in ROLES if not wanted or r.name in wanted]
    region = resolve_region(args.region)
    env_path = Path(args.env_file)
    from_file = read_env_file(env_path)
    keys = {r.env for r in ROLES} | {PROMPT_OPT_MODELS_ENV}
    current = {k: v for k in keys if (v := from_file.get(k, os.environ.get(k))) is not None}

    import boto3

    session = session or boto3.Session()
    try:
        account = session.client("sts", region_name=region).get_caller_identity()["Account"]
    except Exception as exc:
        print(f"error: no usable AWS credentials ({' '.join(_err(exc))})", file=sys.stderr)
        return 1
    simulate = set(_split(args.simulate_unavailable))
    print(f"Account {account}, Region {region}, env file {env_path}"
          + (" [dry run]" if args.dry_run else "")
          + (f" [simulating no access to: {', '.join(sorted(simulate))}]" if simulate else ""))
    prober = Prober(session, region, simulate)
    prober.load_catalogue()
    decisions = decide(prober, roles, current, _split(args.prefer))

    url = database_url(args.database_url)
    try:
        overrides = console_overrides(url)
    except Exception as exc:  # no ledger / no table yet: nothing overrides env
        overrides = {}
        print(f"(console overrides not checked: {_short(str(exc), 90)})")
    for d in decisions:
        d.override = overrides.get(d.role.field)

    print(f"\n{'role':<23} {'env var':<41} {'current':<34} {'chosen':<34} why")
    for d in decisions:
        chosen = "UNAVAILABLE" if d.chosen is None else (d.chosen or '""')
        shown = d.current or '""'
        print(f"{d.role.name:<23} {d.role.env:<41} {shown:<34} {chosen:<34} {d.why}")
        for key, value in d.extra.items():
            print(f"{'':<23} {key:<41} {current.get(key, '(default)'):<34} {value}")

    shadowed = [d for d in decisions if d.override is not None]
    if shadowed:
        print("\nConsole overrides (Models page, ledger platform_settings) take precedence over"
              " the env file:")
        for d in shadowed:
            works = (prober.probe(d.override, d.role.kind).describe()
                     if prober.is_offered(d.override) else f"not offered in {region}")
            print(f"  {d.role.name}: console={d.override} ({works}); the env value "
                  f"{d.chosen if d.chosen is not None else '(none)'} takes effect only after"
                  " the override is reset (admin -> 配置 -> 模型设置)")

    values: dict[str, str] = {}
    for d in decisions:
        if d.chosen is not None:
            values[d.role.env] = d.chosen
            values.update(d.extra)
    unavailable = [d.role.name for d in decisions if d.chosen is None and not d.role.restricted]
    result: dict[str, Any] = {"account": account, "region": region, "env_file": str(env_path),
                              "dry_run": args.dry_run, "roles": [d.as_dict() for d in decisions],
                              "values": values, "written": False, "restart": None,
                              "evaluators": None, "leaked_probe_evaluators": prober.leaked}
    exit_code = 2 if unavailable else 0

    # Ledger evaluators: always reported when reachable; updated only on request.
    judge = next((d.chosen for d in decisions if d.role.name == "judge"), None)
    judge = judge or current.get("LAUNCHPAD_JUDGE_MODEL_ID")
    try:
        ids = ledger_evaluator_ids(url, account, region)
    except Exception as exc:
        print(f"\nLedger evaluators: skipped (ledger not reachable: {_short(str(exc), 90)})")
    else:
        rows, failed = check_evaluators(prober, ids, judge,
                                        args.update_evaluators and not args.dry_run)
        result["evaluators"] = rows
        print(f"\nLedger evaluators in {account}/{region}: {len(rows)}")
        for row in rows:
            moved = f" -> {row['after']}" if row.get("after") != row.get("before") else ""
            print(f"  {row['id']} {row.get('name', '')}: {row.get('before', '')}{moved}"
                  f"  [{row['status']}]")
        exit_code = 2 if failed else exit_code

    if args.dry_run:
        print("\nDry run: nothing written, restarted or updated.")
    elif values:
        try:
            write_env_file(env_path, values, region)
        except OSError as exc:
            print(f"\nerror: cannot write {env_path}: {exc.strerror or exc}. Re-run with sudo "
                  "(e.g. sudo -E uv run python ../scripts/switch_models.py) or pass --env-file.",
                  file=sys.stderr)
            return 1
        result["written"] = True
        print(f"\nWrote {len(values)} keys to {env_path} (mode 0600). The backend unit must load"
              f" it: EnvironmentFile=-{env_path}")
        if args.restart:
            result["restart"] = restart_backend()
            print(result["restart"])
    if unavailable:
        print(f"\nUNAVAILABLE (no working candidate): {', '.join(unavailable)}")
    if args.json:
        result["exit_code"] = exit_code
        print(json.dumps(result, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
