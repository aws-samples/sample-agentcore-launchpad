"""front-desk — Registry-driven A2A routing agent (Launchpad demo).

Knows no business domain itself. For every question it:
1. DISCOVERs specialists via Registry semantic search (APPROVED A2A cards),
2. SELECTs one by the cards' skills/descriptions,
3. INVOKEs it over the transport the card declares — standard A2A JSON-RPC
   for a2a-jsonrpc cards, InvokeHarness / InvokeAgentRuntime otherwise,
4. RESPONDs citing the specialist.

Every step appends to TRACE; the entrypoint returns it as `a2a_trace`
alongside the answer so the demo sub-page can narrate the four stages.
Deployed as a zip_runtime code_bundle agent by scripts/deploy_frontdesk_agent.py.
"""

import hashlib
import json
import os
import uuid
from contextvars import ContextVar
from typing import Any
from urllib.parse import unquote

import boto3
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import (
    AgentCoreMemorySessionManager,
)
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from strands import Agent, tool

REGISTRY_ID = os.environ.get("LAUNCHPAD_REGISTRY_ID", "")
MEMORY_ID = os.environ.get("LAUNCHPAD_MEMORY_ID", "")
SELF_NAME = os.environ.get("FRONTDESK_NAME", "front-desk")
REGION = os.environ.get("AWS_REGION", "us-west-2")
MODEL_ID = "__FRONTDESK_MODEL_ID__"

SYSTEM_PROMPT = (
    "You are the enterprise front desk. You have NO business knowledge of your "
    "own. For EVERY user question: first call discover_agents with a short "
    "topical query; pick the specialist whose card skills/description best "
    "match; forward the question with call_agent (give your routing reason); "
    "then answer based on the specialist's reply, opening with the specialist's "
    "name in the form '[via <name>]'. If discovery returns no matching "
    "specialist, say so honestly and give only generic guidance — never invent "
    "domain answers."
)

_TRACE: ContextVar[list[dict[str, Any]]] = ContextVar("frontdesk_trace")
_LAST_HITS: ContextVar[dict[str, dict[str, Any]]] = ContextVar("frontdesk_hits")
_SESSION_ID: ContextVar[str] = ContextVar("frontdesk_session")


def new_session_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:8]


def derive_session(fd_session: str, agent_name: str) -> str:
    """Stable downstream runtimeSessionId (pure). Same front-desk session +
    same specialist → same downstream session, so the specialist keeps its
    short-term memory across repeat routings and its memory events can be
    located from the front-desk session id. Adhoc calls (no session) fall
    back to a random id; sha256 hex satisfies the ≥16-char constraint."""
    if not fd_session:
        return new_session_id()
    return hashlib.sha256(f"{fd_session}:{agent_name}".encode()).hexdigest()


def a2a_message(message: str, context_id: str) -> dict[str, Any]:
    """Build one stateful A2A message/send envelope."""
    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "messageId": uuid.uuid4().hex,
                "contextId": context_id,
                "parts": [{"kind": "text", "text": message}],
            }
        },
    }


def memory_session_manager(
    actor_id: str, session_id: str
) -> AgentCoreMemorySessionManager | None:
    if not MEMORY_ID:
        return None
    config = AgentCoreMemoryConfig(
        memory_id=MEMORY_ID,
        actor_id=actor_id,
        session_id=session_id,
    )
    return AgentCoreMemorySessionManager(config, region_name=REGION)


def _client():
    return boto3.client("bedrock-agentcore", region_name=REGION)


def _registry_client():
    return boto3.client("agent-registry", region_name=REGION)


def parse_card(record: dict[str, Any]) -> dict[str, Any] | None:
    """Registry record → routing card (pure; tested offline)."""
    try:
        card = json.loads(record["descriptors"]["a2aAgentCard"]["data"])
    except Exception:
        return None
    meta = card.get("metadata") or {}
    return {
        "name": card.get("name") or record.get("name") or "",
        "description": (card.get("description") or "")[:300],
        "skills": [
            {"name": s.get("name") or "", "description": (s.get("description") or "")[:200],
             "tags": s.get("tags") or []}
            for s in card.get("skills") or []
        ],
        "transport": meta.get("launchpad.transport", "agentcore-http"),
        "method": meta.get("launchpad.method", ""),
        "url": card.get("url") or "",
    }


def arn_from_url(url: str) -> str:
    """Card url → invokable ARN (pure). Handles data-plane invocation URLs
    (urlencoded arn in the /runtimes/ path segment) and plain ARNs."""
    if url.startswith("arn:"):
        return url
    marker = "/runtimes/"
    if marker in url:
        tail = url.split(marker, 1)[1]
        return unquote(tail.split("/", 1)[0])
    return ""


def _a2a_reply_text(result: dict[str, Any]) -> str:
    """message/send result → text (Task artifacts or Message parts; history
    is streaming fragments and must be ignored)."""
    if result.get("kind") == "message":
        parts = result.get("parts") or []
    else:
        parts = [p for a in result.get("artifacts") or [] for p in a.get("parts") or []]
    return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()


@tool
def discover_agents(query: str) -> str:
    """Search the enterprise agent registry for APPROVED specialist agents whose
    skills match the query. Always call this first. Returns a JSON list of
    agent cards: name, description, skills, transport."""
    resp = _registry_client().search_discoverable_registry_records(
        registryIds=[REGISTRY_ID], searchQuery=query[:900] or "specialist", maxResults=8
    )
    hits: list[dict[str, Any]] = []
    for rec in resp.get("registryRecords", []):
        if rec.get("recordType") != "AGENT" or rec.get("status") != "APPROVED":
            continue
        card = parse_card(rec)
        if not card or card["name"] == SELF_NAME:
            continue
        hits.append(card)
        _LAST_HITS.get()[card["name"]] = card
    _TRACE.get().append({"stage": "discover", "query": query, "hits": hits})
    return json.dumps(hits)


@tool
def call_agent(agent_name: str, message: str, reason: str) -> str:
    """Forward a message to a specialist found via discover_agents and return
    its reply. agent_name must match a discovered card name exactly; reason is
    one sentence on why this specialist was chosen."""
    card = _LAST_HITS.get().get(agent_name)
    if card is None:
        return f"unknown agent '{agent_name}' — call discover_agents first"
    session = derive_session(_SESSION_ID.get(), agent_name)
    client = _client()
    transport = card["transport"]
    request_excerpt = message[:300]
    try:
        if transport == "a2a-jsonrpc":
            payload = a2a_message(message, session)
            request_excerpt = json.dumps(payload)[:300]
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=arn_from_url(card["url"]),
                runtimeSessionId=session,
                payload=json.dumps(payload).encode(),
            )
            body = json.loads(resp["response"].read())
            if isinstance(body, dict) and body.get("error"):
                raise RuntimeError(str(body["error"])[:200])
            answer = _a2a_reply_text(body.get("result") or {})
        elif card["method"] == "harness":
            stream = client.invoke_harness(
                harnessArn=arn_from_url(card["url"]),
                runtimeSessionId=session,
                actorId="frontdesk",
                messages=[{"role": "user", "content": [{"text": message}]}],
            )
            parts: list[str] = []
            for event in stream["stream"]:
                delta = event.get("contentBlockDelta", {}).get("delta", {})
                if "text" in delta:
                    parts.append(delta["text"])
                if "runtimeClientError" in event or "internalServerException" in event:
                    raise RuntimeError("harness stream error")
            answer = "".join(parts).strip()
        else:
            resp = client.invoke_agent_runtime(
                agentRuntimeArn=arn_from_url(card["url"]),
                runtimeSessionId=session,
                payload=json.dumps({"prompt": message}).encode(),
            )
            body = json.loads(resp["response"].read())
            answer = str(body.get("result", "")) if isinstance(body, dict) else str(body)
    except Exception as exc:  # surfaced to the model — it reports honestly
        answer = f"[specialist call failed: {type(exc).__name__}: {exc}]"
    _TRACE.get().append({
        "stage": "invoke", "target": agent_name, "transport": transport,
        "session": session, "reason": reason, "request_excerpt": request_excerpt,
        "response_excerpt": answer[:400],
    })
    return answer


app = BedrockAgentCoreApp()


@app.entrypoint
def invoke(payload: dict[str, Any], context: Any = None) -> dict[str, Any]:
    prompt = str(payload.get("prompt", "")).strip()
    if not prompt:
        return {"error": "payload must include a non-empty 'prompt'"}
    actor_id = str(payload.get("actor_id", "frontdesk"))
    session_id = str(
        getattr(context, "session_id", "")
        or payload.get("session_id", "")
        or new_session_id()
    )
    trace: list[dict[str, Any]] = []
    session_token = _SESSION_ID.set(session_id)
    trace_token = _TRACE.set(trace)
    hits_token = _LAST_HITS.set({})
    try:
        kwargs: dict[str, Any] = {
            "model": MODEL_ID,
            "system_prompt": SYSTEM_PROMPT,
            "tools": [discover_agents, call_agent],
        }
        session_manager = memory_session_manager(actor_id, session_id)
        if session_manager is not None:
            kwargs["session_manager"] = session_manager
        result = Agent(**kwargs)(prompt)
        return {"result": str(result), "a2a_trace": list(trace)}
    finally:
        _LAST_HITS.reset(hits_token)
        _TRACE.reset(trace_token)
        _SESSION_ID.reset(session_token)


if __name__ == "__main__":
    app.run()
