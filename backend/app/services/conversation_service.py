"""Studio local-debug conversation service — multi-turn chat against un-deployed
generated code.

Ported from strands_studio_ui backend/app/services/conversation_service.py
(origin/main). Sessions are in-memory (lost on backend reload) — acceptable for
local debug. Each session owns a temp dir with an ``agent.py`` that is re-run per
turn, replaying the whole conversation via ``--messages`` (the generated program
is stateless). Failed turns are marked in pairs so they never re-enter replay.

Launchpad adaptations vs upstream:
- Subprocess uses ``settings.studio_exec_python`` (dedicated exec interpreter),
  not ``uv run python`` — matches the execution service and removes upstream's
  sys.executable-vs-uv-run inconsistency.
- Env is built by ``local_exec.build_execution_env`` (adds AWS region +
  Bedrock key handling); skills are bundled into the session dir at init.
"""

import asyncio
import json
import logging
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from app.models.conversation import (
    ChatMessage,
    ChatResponse,
    ConversationHistoryResponse,
    ConversationListResponse,
    ConversationSession,
    CreateConversationRequest,
    MessageListResponse,
)
from app.services import local_exec

logger = logging.getLogger("launchpad.conversations")

NONSTREAM_TIMEOUT_S = 60


class ConversationService:
    def __init__(self) -> None:
        self.sessions: dict[str, ConversationSession] = {}
        self.messages: dict[str, list[ChatMessage]] = {}  # session_id -> messages
        self.agent_processes: dict[str, Any] = {}  # session_id -> agent info

    async def create_session(self, request: CreateConversationRequest) -> ConversationSession:
        session = ConversationSession(
            project_id=request.project_id,
            version=request.version,
            agent_config=request.flow_data,
            openai_api_key=request.openai_api_key,
            bedrock_api_key=request.bedrock_api_key,
        )
        self.sessions[session.session_id] = session
        self.messages[session.session_id] = []
        self._initialize_agent(session.session_id, request.generated_code)
        return session

    def _initialize_agent(self, session_id: str, generated_code: str) -> None:
        """Write the generated code to a persistent per-session temp dir and
        bundle any referenced skills next to it."""
        session_dir = Path(tempfile.mkdtemp(
            prefix=f"agent_session_{session_id}_",
            dir=local_exec.exec_workdir_base(),
        ))
        agent_file = session_dir / "agent.py"
        agent_file.write_text(generated_code, encoding="utf-8")
        local_exec.bundle_skills_for_workdir(generated_code, str(session_dir))
        local_exec.grant_workdir_to_exec_user(str(session_dir))
        self.agent_processes[session_id] = {
            "session_dir": session_dir,
            "agent_file": agent_file,
            "initialized": True,
        }

    async def update_session_code(
        self, session_id: str, generated_code: str
    ) -> ConversationSession:
        """Rewrite the session's agent code in place (session + messages kept).
        Backs the apply-fix path so subsequent turns run the fixed code."""
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        if session_id not in self.agent_processes:
            raise ValueError(f"Agent not initialized for session {session_id}")
        agent_info = self.agent_processes[session_id]
        agent_info["agent_file"].write_text(generated_code, encoding="utf-8")
        local_exec.bundle_skills_for_workdir(
            generated_code, str(agent_info["session_dir"])
        )
        # The rewritten file and any newly bundled skills are owned by the backend
        # user again, so re-hand them over.
        local_exec.grant_workdir_to_exec_user(str(agent_info["session_dir"]))
        session = self.sessions[session_id]
        session.updated_at = datetime.now()
        return session

    @staticmethod
    def _mark_turn_failed(user_message: ChatMessage, agent_message: ChatMessage) -> None:
        """Mark both messages of a failed turn so replay skips them as a pair
        (keeps user/assistant role alternation intact)."""
        for msg in (user_message, agent_message):
            msg.metadata = {**(msg.metadata or {}), "error": True}

    def _construct_messages_list(self, session_id: str) -> list[dict[str, Any]]:
        """Bedrock Converse-shaped history for --messages replay:
        [{"role": "user"|"assistant", "content": [{"text": ...}]}, ...].
        Error-marked (failed-turn) messages are excluded as pairs. The just-added
        user message is already in self.messages, so it is included here."""
        messages_list: list[dict[str, Any]] = []
        for message in self.messages.get(session_id, []):
            if message.metadata and message.metadata.get("error"):
                continue
            role = "user" if message.sender == "user" else "assistant"
            messages_list.append({"role": role, "content": [{"text": message.content}]})
        return messages_list

    @staticmethod
    def _build_invocation(
        agent_file: Path, session: ConversationSession, messages_json: str
    ) -> local_exec.ExecInvocation:
        """Backend-resolved argv/env for one replay turn of the session's
        agent.py (host interpreter or one-shot docker container)."""
        return local_exec.build_exec_invocation(
            str(agent_file),
            str(agent_file.parent),
            ["--messages", messages_json],
            session.openai_api_key,
            session.bedrock_api_key,
            extra_env={"PYTHONUNBUFFERED": "1"},
        )

    async def send_message(self, session_id: str, message: str) -> ChatResponse:
        """Non-streaming turn: append the user message, run the agent replaying
        the full history, store the reply (or the error, pair-marked)."""
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")

        user_message = ChatMessage(session_id=session_id, sender="user", content=message)
        self.messages[session_id].append(user_message)

        success, response_text = await self._execute_agent(session_id)
        agent_response = ChatMessage(
            session_id=session_id, sender="agent", content=response_text
        )
        error_text: str | None = None
        if not success:
            error_text = response_text
            self._mark_turn_failed(user_message, agent_response)
        self.messages[session_id].append(agent_response)

        session = self.sessions[session_id]
        session.message_count += 2
        session.updated_at = datetime.now()

        return ChatResponse(
            message_id=agent_response.message_id,
            content=agent_response.content if success else "",
            timestamp=agent_response.timestamp,
            streaming_complete=True,
            success=success,
            error=error_text,
        )

    async def _execute_agent(self, session_id: str) -> tuple[bool, str]:
        """Run the session's agent.py once, replaying history via --messages.
        Returns (success, stdout-or-error)."""
        if session_id not in self.agent_processes:
            raise ValueError(f"Agent not initialized for session {session_id}")
        problem = local_exec.runner_error()
        if problem:
            return False, problem

        agent_file = self.agent_processes[session_id]["agent_file"]
        session = self.sessions[session_id]
        messages_json = json.dumps(self._construct_messages_list(session_id))
        invocation = self._build_invocation(agent_file, session, messages_json)

        try:
            # Prompt history is one JSON argv value and never enters a command shell.
            result = subprocess.run(  # nosemgrep: dangerous-subprocess-use-audit
                invocation.argv,
                capture_output=True,
                text=True,
                timeout=NONSTREAM_TIMEOUT_S,
                cwd=agent_file.parent,
                env=invocation.env,
                **invocation.spawn_kwargs,
            )
            if result.returncode == 0:
                return True, result.stdout.strip()
            error_msg = result.stderr.strip() or "Agent execution failed"
            logger.error("agent execution error for session %s: %s", session_id, error_msg)
            return False, error_msg
        except subprocess.TimeoutExpired:
            # subprocess.run killed the child, but in docker mode that child is
            # only the CLI client — the container must die separately.
            local_exec.kill_container(invocation.container_name)
            return False, "Agent execution timed out"
        except Exception as exc:  # noqa: BLE001 — surfaced as a failed turn
            local_exec.kill_container(invocation.container_name)
            logger.error("exception during agent execution for %s: %s", session_id, exc)
            return False, str(exc)

    async def stream_message(
        self, session_id: str, message: str
    ) -> AsyncGenerator[str, None]:
        """Streaming turn. Yields raw stdout chunks, then a `[CHAT_ERROR:<json>]`
        sentinel on failure (JSON-encoded so multiline tracebacks stay one line),
        then always `[CHAT_COMPLETE:<agent_message_id>]`."""
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")

        user_message = ChatMessage(session_id=session_id, sender="user", content=message)
        self.messages[session_id].append(user_message)

        full_response = ""
        error_text: str | None = None
        agent_message_id = str(uuid.uuid4())

        try:
            async for kind, text in self._execute_agent_stream(session_id):
                if kind == "error":
                    error_text = text
                else:
                    full_response += text
                    yield text
        except Exception as exc:  # noqa: BLE001
            error_text = str(exc)

        agent_message = ChatMessage(
            message_id=agent_message_id,
            session_id=session_id,
            sender="agent",
            content=error_text if error_text is not None else full_response,
        )
        if error_text is not None:
            self._mark_turn_failed(user_message, agent_message)
        self.messages[session_id].append(agent_message)

        session = self.sessions[session_id]
        session.message_count += 2
        session.updated_at = datetime.now()

        if error_text is not None:
            yield f"[CHAT_ERROR:{json.dumps(error_text)}]"
        yield f"[CHAT_COMPLETE:{agent_message_id}]"

    async def _execute_agent_stream(
        self, session_id: str
    ) -> AsyncGenerator[tuple[str, str], None]:
        """Run the session's agent.py streaming stdout, yielding
        ("chunk", text) / ("error", text) tuples."""
        if session_id not in self.agent_processes:
            raise ValueError(f"Agent not initialized for session {session_id}")
        problem = local_exec.runner_error()
        if problem:
            yield ("error", problem)
            return

        agent_file = self.agent_processes[session_id]["agent_file"]
        session = self.sessions[session_id]
        messages_json = json.dumps(self._construct_messages_list(session_id))
        invocation = self._build_invocation(agent_file, session, messages_json)

        run: local_exec.ExecRun | None = None
        try:
            # Same isolation as local_exec.spawn_execution_subprocess: this is
            # the second place the platform runs caller-supplied code.
            process = await asyncio.create_subprocess_exec(
                *invocation.argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=agent_file.parent,
                env=invocation.env,
                **invocation.spawn_kwargs,
            )
            run = local_exec.ExecRun(process, None, invocation.container_name)

            while True:
                chunk = await process.stdout.read(1024)
                if not chunk:
                    break
                text_chunk = chunk.decode("utf-8", errors="ignore")
                if text_chunk:
                    yield ("chunk", text_chunk)

            await process.wait()

            if process.returncode != 0:
                stderr_output = await process.stderr.read()
                error_msg = (
                    stderr_output.decode("utf-8", errors="ignore").strip()
                    or "Agent execution failed"
                )
                yield ("error", error_msg)
        except Exception as exc:  # noqa: BLE001
            yield ("error", str(exc))
        finally:
            # Reached normally after EOF (no-op then), but also when the client
            # disconnects mid-stream and the generator is closed — without this
            # the run (and in docker mode the container) would outlive the turn.
            if run is not None:
                run.cleanup()

    async def get_sessions(self) -> ConversationListResponse:
        sessions = sorted(self.sessions.values(), key=lambda s: s.updated_at, reverse=True)
        return ConversationListResponse(sessions=sessions)

    async def get_session_history(self, session_id: str) -> ConversationHistoryResponse:
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        return ConversationHistoryResponse(
            session=self.sessions[session_id],
            messages=self.messages.get(session_id, []),
        )

    async def get_session_messages(self, session_id: str) -> MessageListResponse:
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        return MessageListResponse(messages=self.messages.get(session_id, []))

    async def delete_session(self, session_id: str) -> dict:
        if session_id not in self.sessions:
            raise ValueError(f"Session {session_id} not found")
        if session_id in self.agent_processes:
            session_dir = self.agent_processes[session_id].get("session_dir")
            if session_dir and Path(session_dir).exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            del self.agent_processes[session_id]
        del self.sessions[session_id]
        self.messages.pop(session_id, None)
        return {"message": f"Session {session_id} deleted successfully"}

    async def cleanup_expired_sessions(self, max_age_hours: int = 24) -> int:
        cutoff = datetime.now() - timedelta(hours=max_age_hours)
        expired = [sid for sid, s in self.sessions.items() if s.updated_at < cutoff]
        for session_id in expired:
            try:
                await self.delete_session(session_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("failed to clean up session %s: %s", session_id, exc)
        return len(expired)


# Global singleton (in-memory, ephemeral).
conversation_service = ConversationService()
