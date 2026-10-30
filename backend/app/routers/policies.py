"""``/api/policies`` — RETIRED by the Global model (policy-engines contract, 2026-10-28).

Policies belong to a policy engine now: ``/api/policy-engines/{engine_id}/policies/…``
(``routers/policy_engines.py``). For one release every path under ``/api/policies`` answers
410 ``policy.moved`` with ``{engines_url}`` so a stale console or script learns where to go;
the workspace mode / switches / confirmation / evidence / apply routes have no successor
(the gateway attachment carries the mode: ``PUT /api/governance/gateways/{gid}/policy-engine``).
Mounted by ``app/main.py`` only when ``"policy"`` is in ``Settings.features`` (an off-feature
build answers 404 like any unknown route).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from app.core.errors import AppError

router = APIRouter(prefix="/api/policies", tags=["policies"])

ENGINES_URL = "/api/policy-engines"
MOVED_MESSAGE = ("policies belong to a policy engine now — use /api/policy-engines/{engine_id}/"
                 "policies (Global AgentCore Policy model)")


def moved(path: str = "") -> AppError:
    return AppError("policy.moved", MOVED_MESSAGE,
                    {"engines_url": ENGINES_URL, "path": path or None}, status_code=410)


@router.api_route("", methods=["GET", "POST"], include_in_schema=False)
def collection_moved(request: Request) -> dict[str, Any]:
    raise moved(request.url.path)


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "DELETE"],
                  include_in_schema=False)
def path_moved(rest: str, request: Request) -> dict[str, Any]:
    raise moved(request.url.path)
