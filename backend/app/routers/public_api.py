"""Public /v1 API — the system-integration entrance.

Same invoke chain as the Chat playground (services.chat / services.invoke);
auth is an X-Api-Key header checked against hashed keys in the ledger.
"""

import time
from typing import Any

from fastapi import APIRouter, Depends, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ApiKey
from app.routers.apikeys import hash_key
from app.services.chat import chat_stream, sse_encode
from app.services.invoke import invoke_agent_text
from app.services.memory import scoped_actor
from app.services.runtime_discovery import invoke_capability, require_invoke_capability

router = APIRouter(prefix="/v1", tags=["public-v1"])


def require_api_key(
    x_api_key: str | None = Header(default=None), db: Session = Depends(get_db)
) -> ApiKey:
    if not x_api_key:
        raise AppError("auth.missing_api_key", "X-Api-Key header required", status_code=401)
    key = db.query(ApiKey).filter(ApiKey.key_hash == hash_key(x_api_key)).first()
    if key is None or not key.enabled:
        raise AppError("auth.invalid_api_key", "invalid or disabled API key", status_code=401)
    return key


class InvokeV1Request(BaseModel):
    prompt: str = Field(min_length=1, max_length=100000)
    session_id: str | None = None
    actor_id: str = "api"


def _active_agent(db: Session, agent_id: str) -> Agent:
    agent = db.get(Agent, agent_id)
    if agent is None or agent.status == "deleted":
        raise NotFoundError("agent.not_found", "agent not found")
    require_invoke_capability(agent)
    return agent


@router.get("/agents", summary="List active agents")
def v1_list_agents(
    db: Session = Depends(get_db), _key: ApiKey = Depends(require_api_key)
) -> dict[str, Any]:
    agents = [
        agent
        for agent in db.query(Agent).filter(Agent.status == "active").all()
        if invoke_capability(agent)["eligible"]
    ]
    return {
        "agents": [
            {"id": a.id, "name": a.name, "method": a.method, "version": a.version}
            for a in agents
        ]
    }


@router.post("/agents/{agent_id}/invoke", summary="Invoke an agent (sync)")
def v1_invoke(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    _key: ApiKey = Depends(require_api_key),
) -> dict[str, Any]:
    agent = _active_agent(db, agent_id)
    started = time.monotonic()
    result = invoke_agent_text(
        agent, req.prompt, session_id=req.session_id,
        actor_id=scoped_actor(agent.id, req.actor_id),
    )
    return {
        "agent": agent.name,
        "text": result["text"],
        "session_id": result["session_id"],
        "latency_ms": int((time.monotonic() - started) * 1000),
    }


@router.post("/agents/{agent_id}/invoke-stream", summary="Invoke an agent (SSE stream)")
def v1_invoke_stream(
    agent_id: str,
    req: InvokeV1Request,
    db: Session = Depends(get_db),
    _key: ApiKey = Depends(require_api_key),
) -> StreamingResponse:
    agent = _active_agent(db, agent_id)

    mem_actor = scoped_actor(agent.id, req.actor_id)

    def generate():
        for event in chat_stream(
            agent, req.prompt, session_id=req.session_id, actor_id=mem_actor
        ):
            yield sse_encode(event)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
