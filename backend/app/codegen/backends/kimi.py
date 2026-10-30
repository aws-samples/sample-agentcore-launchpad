"""Kimi K3 (Global Bedrock, OpenAI-compatible Chat Completions) — the AI-fix backend.

Replaces the Claude Agent SDK backend (spec C5: Claude paths are deleted). Where Claude
ran an agentic editing session, Kimi answers ONE structured request per round: the fix
workspace (`CLAUDE.md` task brief, `contract_spec.md`, `flow_semantics.md`, `flow.json`,
`error.txt`, `generated_agent.py`) is inlined into the prompt and the model returns a
JSON object ``{"diagnosis": {...}, "generated_agent": "<full file>" | null}``. The
backend writes ``diagnosis.json`` and, when a file came back, ``generated_agent.py`` —
the two artifacts ``service._run_fix`` reads. Repair rounds add the validation errors.

Transport is stdlib ``urllib`` (no new runtime dependency) with the Global Bedrock API
key as the bearer (``AWS_BEARER_TOKEN_BEDROCK``, spec C4) against
``aws_clients.global_bedrock_openai_base_url(settings.model_region)``. Only the
message ``content`` is read; Kimi's ``reasoning_content`` is ignored (spec C3).
"""

import asyncio
import json
import os
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

from app.codegen import config
from app.codegen.backends.base import CodingAgentBackend, GenerationError, GenerationTask
from app.core.config import get_settings
from app.services.aws_clients import GLOBAL_BEDROCK_BEARER_ENV, global_bedrock_openai_base_url

DIAGNOSIS_FILENAME = "diagnosis.json"
WORKSPACE_FILES = (
    "CLAUDE.md",
    "contract_spec.md",
    "flow_semantics.md",
    "flow.json",
    "error.txt",
    config.GENERATED_FILENAME,
)
MAX_FILE_CHARS = 60_000  # per file; the tail is what matters for error.txt
REQUEST_TIMEOUT_S = 120.0
MAX_OUTPUT_TOKENS = 16384  # ≥ 256 (spec C3); a full generated_agent.py fits

SYSTEM_PROMPT = (
    "You repair Strands Agent SDK Python programs. You receive the files of a fix "
    "workspace. CLAUDE.md defines the task, the diagnosis categories and when editing "
    "generated_agent.py is allowed; contract_spec.md lists the rules the file must keep "
    "satisfying. Respond with ONE JSON object and nothing else: "
    '{"diagnosis": {"category": "code" | "config" | "environment", "summary": string, '
    '"suggestions": [{"node_label": string, "property": string, "action": string}]}, '
    '"generated_agent": the complete corrected generated_agent.py as a string, or null '
    "when no code change is allowed or needed}. Never put secrets into code."
)


def _read(workspace: Path, name: str) -> str:
    path = workspace / name
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return text if len(text) <= MAX_FILE_CHARS else text[-MAX_FILE_CHARS:]


def build_user_prompt(workspace: Path, task: GenerationTask) -> str:
    parts = []
    for name in WORKSPACE_FILES:
        body = _read(workspace, name)
        if body:
            parts.append(f"### {name}\n```\n{body}\n```")
    if task.validation_errors:
        errors = "\n".join(f"- {error}" for error in task.validation_errors)
        parts.append(
            "### validation errors\nThe previous generated_agent.py failed validation. "
            f"Fix these while keeping every contract rule:\n{errors}"
        )
    return "\n\n".join(parts)


def build_request(model: str, user_prompt: str) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }


def parse_reply(content: str) -> tuple[dict, str | None]:
    """(diagnosis, generated_agent or None) from the model's text, fences tolerated."""
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise GenerationError("Kimi returned no JSON object")
    try:
        payload = json.loads(content[start : end + 1])
    except json.JSONDecodeError as exc:
        raise GenerationError(f"Kimi reply is not valid JSON: {exc}") from exc
    diagnosis = payload.get("diagnosis")
    code = payload.get("generated_agent")
    return (
        diagnosis if isinstance(diagnosis, dict) else {},
        code if isinstance(code, str) and code.strip() else None,
    )


class KimiBackend(CodingAgentBackend):
    name = "kimi"

    def __init__(self) -> None:
        self._settings = get_settings()

    async def check_available(self) -> tuple[bool, str]:
        if not os.environ.get(GLOBAL_BEDROCK_BEARER_ENV, "").strip():
            return False, (
                f"{GLOBAL_BEDROCK_BEARER_ENV} is not set in the backend environment "
                "(the Global Bedrock API key the hub uses for Kimi K3)"
            )
        return True, ""

    def _endpoint(self) -> str:
        return global_bedrock_openai_base_url(self._settings.model_region) + "/chat/completions"

    def _post(self, body: dict) -> dict:
        bearer = os.environ.get(GLOBAL_BEDROCK_BEARER_ENV, "").strip()
        if not bearer:
            raise GenerationError(
                f"{GLOBAL_BEDROCK_BEARER_ENV} is not set — the hub reaches Global Bedrock "
                "(GLM 5.3 / Kimi K3) with a Bedrock API key, never with its aws-cn credentials"
            )
        request = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {bearer}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise GenerationError(f"Kimi request failed: HTTP {exc.code} {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GenerationError(f"Kimi request failed: {exc}") from exc

    async def generate(
        self,
        workspace: Path,
        task: GenerationTask,
        on_progress: Callable[[str], Awaitable[None]],
    ) -> None:
        await on_progress("Sending the fix workspace to Kimi K3")
        body = build_request(config.get_model(), build_user_prompt(workspace, task))
        reply = await asyncio.to_thread(self._post, body)
        try:
            content = reply["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise GenerationError("Kimi reply carried no message content") from exc
        diagnosis, code = parse_reply(content if isinstance(content, str) else json.dumps(content))
        (workspace / DIAGNOSIS_FILENAME).write_text(json.dumps(diagnosis), encoding="utf-8")
        if code is not None:
            (workspace / config.GENERATED_FILENAME).write_text(code, encoding="utf-8")
            await on_progress("Kimi returned a corrected generated_agent.py")
        else:
            await on_progress("Kimi returned a diagnosis without a code change")
        summary = diagnosis.get("summary")
        if isinstance(summary, str) and summary:
            await on_progress(summary)
