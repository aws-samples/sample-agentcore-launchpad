"""Creator-or-admin — who may MUTATE a user-managed resource (Family B step 3, r13-g).

Visibility (`services/visibility.py`) decides who may *see* a row; this module decides
who may *change or destroy* one. The rule is the one `routers/agents.require_agent_creator`
established for agent redeploy / delete (N10v2-bug4, re-pointed by step 1 onto
`created_by`), generalised so every stamped kind gates its mutations the same way:

* an **administrator** may act on every row;
* the **member whose account created the row** (`created_by` == their `users.id`) may act
  on it;
* anyone else answers **403 `<kind>.forbidden`** before any side effect — the detail
  carries the row's id and its creator's display name so the console can say whose it is;
* a row with **no creator** (`created_by IS NULL`: the config admin's, legacy rows, system
  paths) is an **administrator's** — the CN ruling of rev-r11 (a NULL-creator API key is an
  administrator's; upstream #263 treated it as everyone's). `Creator.owns` already answers
  False for a NULL creator, so the rule needs no special case here.

Reads, invoke and chat are deliberately NOT gated — within a workspace they are the shared
member surface (`core/route_policy.py`); visibility alone governs them. The gate is applied
in the routers, after the kind's visibility lookup (a hidden row stays an indistinguishable
404) and before any stage / in-use check, so a forbidden caller learns nothing about the
row's state.

Routes gated through this module (the contract list lives in
docs/cn/interfaces/users-and-ownership.md §8):

* `POST /api/runtime-canaries/{id}/action` — every action except `verdict`
  (`canary.forbidden`);
* Skill Lab: job `cancel` / `resume` / `publish` / `DELETE` (publish also gates the SOURCE
  registry record — `registry.forbidden`, N17-obs263), task set `PUT` / `DELETE`,
  `apply-expansion` (the TARGET task set's creator) (`skill_lab.forbidden`);
* `PUT` / `DELETE /api/eval/datasets/{id}` (`dataset.forbidden`);
* `PUT` / `DELETE /api/eval/evaluators/{id}` (`evaluator.forbidden`);
* `PATCH` / `DELETE /api/eval/online/{id}`, `POST …/pause` / `…/resume` — online
  evaluation configs (N17-obs426, `online_config.forbidden`; gated inside
  `evaluation/online.py` after its visibility lookup);
* `PUT` / `DELETE /api/registry/records/{id}`, `POST …/reimport` — the local registry's
  MCP / skill records stay shared for READ (§7) but their content mutations are the
  creator's (N15-obs7, `registry.forbidden`); lifecycle `…/action` stays the review
  workflow (submit / approve / reject / disable are a reviewer's, not the creator's) and
  system skills keep `registry.system_skill_protected`;
* `POST /api/agents/{id}/redeploy`, `DELETE /api/agents/{id}` — through
  `require_agent_creator`, which adds the discovered-runtime and preset exceptions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.errors import AppError
from app.services import creators

if TYPE_CHECKING:
    from app.routers.auth import Identity


def may_mutate(row: Any, identity: Identity | None) -> bool:
    """Whether ``identity`` may change or destroy ``row``: an administrator, or the
    member whose account created it. ``None`` (no identity) may not; a NULL creator
    makes the row an administrator's."""
    if identity is None:
        return False
    if identity.is_admin:
        return True
    creator = creators.Creator.of(identity)
    return creator is not None and creator.owns(row)


def require_creator_or_admin(
    row: Any,
    identity: Identity | None,
    code: str,
    *,
    message: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Raise 403 ``code`` unless ``identity`` may mutate ``row`` (:func:`may_mutate`).

    ``detail`` is merged over the row's creator fields (``created_by`` /
    ``created_by_name``) so every forbidden response names whose row it is; pass the
    kind's id key there (``{"canary_id": row.id}``). ``message`` defaults to a generic
    creator-or-admin sentence.
    """
    if may_mutate(row, identity):
        return
    raise AppError(
        code,
        message or "only the resource's creator or an administrator may change or delete it",
        {**creators.fields(row), **(detail or {})},
        status_code=403,
    )
