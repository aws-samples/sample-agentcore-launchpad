"""Settings → Model Providers (D21, plan §3): `/api/settings/model-providers`.

**N17-obs203 (user decision 2026-10-21): model providers are OWNER-BOUND resources.** Every
signed-in member may register a provider of their own (`scope: personal` — visible to them and
to administrators only), read / change / delete it, add models to it, probe it and register its
key; an administrator may do everything, set or flip `scope: workspace` (share a provider with
every member) and change or delete members' rows. The route policy (`core/route_policy.py`)
classifies the CRUD `MEMBER` and keeps the journal and the per-workspace flag matrix `ADMIN`;
this router adds the two owner gates of `docs/cn/interfaces/users-and-ownership.md` §7–§8 in
their order — a provider the caller may not SEE answers `404 model_provider.not_found` first,
one they may see but not CHANGE `403 model_provider.forbidden` second — through
`model_registry.require_provider_visible` / `require_provider_mutable`.

The handlers stay thin: validation and every write live in `services/model_registry.py`, which
re-publishes the snapshot after each change so `/api/capabilities` and every picker see the
edit on their next load.

Probes and the credential flow (M1-b, `services/model_probe.py` / `model_credential.py`)
plug in through `model_registry.set_probe_hooks`; until registered those routes answer
**501** `model_providers.probe_unavailable` / `model_providers.credential_unavailable` (both
recognised by the console's `isProbeUnavailable`) with
the plan's body shapes declared on them, so the console can code against the contract
before the implementation lands.

No response carries a key: `ProviderOut.credential` names the Identity provider and the SSM
parameter only (spec C4, plan §9.2).
"""

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.db import SQLITE_MAX_INT, get_db
from app.core.errors import AppError
from app.routers.auth import Identity, require_identity
from app.schemas.model_providers import (
    ChangesOut,
    CredentialOut,
    CredentialRegisterIn,
    ModelCreate,
    ModelOut,
    ModelProbeIn,
    ModelProbeOut,
    ModelUpdate,
    ProviderCreate,
    ProviderOut,
    ProviderProbeOut,
    ProviderUpdate,
    RegistryOut,
    WorkspaceFlagsIn,
    WorkspaceFlagsOut,
)
from app.services import creators
from app.services import model_registry as registry

router = APIRouter(prefix="/api/settings/model-providers", tags=["model-providers"])


def _actor(identity: Identity) -> str:
    return identity.username


def require_member(request: Request) -> Identity:
    """FastAPI dependency: the signed-in caller (`require_identity` takes an optional
    `settings` argument FastAPI would otherwise read as a body field). The route policy has
    already rejected anonymous callers (401) and admin-only routes."""
    return require_identity(request)


def _provider_out(db: Session, row, identity: Identity | None = None) -> dict[str, Any]:
    return registry.provider_dict(row, registry.model_counts(db).get(row.id, 0),
                                  identity=identity)


def _visible(db: Session, provider_id: str, identity: Identity):
    """The provider, or the kind's 404 when it does not exist OR the caller may not see it
    (another member's personal provider) — users-and-ownership.md §7."""
    return registry.require_provider_visible(registry.get_provider(db, provider_id), identity)


def _action_of(request: Request) -> str:
    path, method = request.url.path, request.method.upper()
    if path.endswith("/credential"):
        return "credential"
    if "/models" in path:
        return "models"
    if path.endswith("/probe"):
        return "probe"
    return "delete" if method == "DELETE" else "update"


def mutable_provider(provider_id: str, request: Request, db: Session = Depends(get_db)):
    """FastAPI dependency for every WRITE under `/{provider_id}`: the row, after the visibility
    404 and the creator-or-admin 403 (users-and-ownership.md §7–§8 order). A dependency rather
    than a call inside the handler so the gates answer BEFORE the body is validated — a
    forbidden caller never learns a request schema (the D21-obs8 precedent)."""
    identity = require_identity(request)
    return registry.require_provider_mutable(registry.get_provider(db, provider_id), identity,
                                             _action_of(request))


def _require_admin_for_scope(identity: Identity, scope: str | None) -> None:
    """Only an administrator may create a `workspace`-scope provider or flip a scope."""
    if scope is not None and not identity.is_admin:
        raise AppError(registry.E_PROVIDER_FORBIDDEN,
                       "only an administrator may share a provider with the workspace "
                       "(scope: workspace) or change a provider's scope",
                       {"scope": scope, "action": "scope"}, status_code=403)


PROVIDER_IMMUTABLE_FIELDS = ("name", "kind")


async def _reject_unknown_provider_update_fields(request: Request) -> None:
    """D21-obs8: name the offending keys of a `PUT /{id}` body. `ProviderUpdate` forbids unknown
    keys (the contract), but the generic 422 `validation.invalid_request` only says "Request
    validation failed" — the console toasted that for every enable / disable while it sent
    `name` / `kind`. Runs after the route-policy dependency (the route is MEMBER) and before
    body validation (FastAPI solves dependencies first; the JSON body is already read and
    cached by then); a malformed body is left to the body parser."""
    try:
        payload = await request.json()
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    extra = sorted(set(payload) - set(ProviderUpdate.model_fields))
    if extra:
        immutable = [f for f in extra if f in PROVIDER_IMMUTABLE_FIELDS]
        what = (f"{', '.join(immutable)} cannot be changed after create" if immutable
                else "unknown field(s)")
        raise AppError(registry.E_PROVIDER_INVALID_UPDATE,
                       f"Provider update rejected: {what} ({', '.join(extra)}); "
                       f"allowed: {', '.join(ProviderUpdate.model_fields)}",
                       {"fields": extra, "immutable": immutable,
                        "allowed": list(ProviderUpdate.model_fields)},
                       status_code=422)


# ── page / collection ────────────────────────────────────────────────────────────────
@router.get("", response_model=RegistryOut)
def list_registry(db: Session = Depends(get_db), identity: Identity = Depends(require_member)
                  ) -> dict[str, Any]:
    """One read for the page: providers, models, per-workspace flags, warnings — narrowed to
    what the caller may see (N17-obs203: a member gets the workspace-scope providers plus
    their own personal ones; the flag matrix is an administrator's)."""
    registry.ensure_seeded(db)  # a ledger reset between requests must not show an empty page
    return registry.registry_dict(db, identity)


@router.post("", status_code=201, response_model=ProviderOut)
def create_provider(body: ProviderCreate, db: Session = Depends(get_db),
                    identity: Identity = Depends(require_member)) -> dict[str, Any]:
    """Register a model source. Validates name / kind / https + egress guard / SSM name
    pattern; touches neither SSM nor Identity (`configured` stays false until
    `POST /{id}/credential`). N17-obs203: a member's provider is `personal` (the body may
    say so or omit `scope`; `workspace` is 403); an administrator's defaults to `workspace`."""
    data = body.model_dump(exclude={"api_key"})
    api_key = body.api_key  # write-only; lives in this frame until `store_api_key` drops it
    if identity.is_admin:
        data["scope"] = data.get("scope") or registry.SCOPE_WORKSPACE
    else:
        if data.get("scope") not in (None, registry.SCOPE_PERSONAL):
            _require_admin_for_scope(identity, data.get("scope"))
        data["scope"] = registry.SCOPE_PERSONAL
    row = registry.build_provider(db, data, _actor(identity),
                                  creator=creators.Creator.of(identity))
    if api_key is not None:
        # N17-obs203 §3: probe FIRST with the typed key — a failure stores nothing at all
        hooks = registry.require_probe_hooks("credential")
        hooks.probe_api_key(row, api_key)
    row = registry.insert_provider(db, row, _actor(identity))
    if api_key is not None:
        hooks.store_api_key(db, row, api_key, _actor(identity))
        del api_key
    return _provider_out(db, row, identity)


@router.get("/changes", response_model=ChangesOut)
def list_changes(limit: int = Query(default=50, ge=1, le=200),
                 before: int | None = Query(default=None, ge=1, le=SQLITE_MAX_INT),
                 target_id: str | None = Query(default=None, max_length=200),
                 db: Session = Depends(get_db)
                 ) -> dict[str, Any]:
    """The journal, newest first (`before` = the id to page from; `target_id` = one
    provider's / model's audit trail — N17-obs203 §4). Administrators only: every row names
    the console user who acted, the row, the action and the time; never key material."""
    return {"items": [registry.change_dict(c)
                      for c in registry.list_changes(db, limit=limit, before=before,
                                                     target_id=target_id)]}


@router.put("/workspaces/{workspace_id}/models", response_model=WorkspaceFlagsOut)
def set_workspace_flags(workspace_id: str, body: WorkspaceFlagsIn,
                        db: Session = Depends(get_db),
                        identity: Identity = Depends(require_member)) -> dict[str, Any]:
    """Per-workspace enable flags (plan §2.4): the full list of models members of
    ``workspace_id`` may NOT pick; absent = enabled."""
    return registry.set_workspace_flags(db, workspace_id, body.disabled_models,
                                        _actor(identity))


# ── one provider ─────────────────────────────────────────────────────────────────────
@router.get("/{provider_id}", response_model=ProviderOut)
def get_provider(provider_id: str, db: Session = Depends(get_db),
                 identity: Identity = Depends(require_member)) -> dict[str, Any]:
    return _provider_out(db, _visible(db, provider_id, identity), identity)


@router.put("/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: str, body: ProviderUpdate, db: Session = Depends(get_db),
                    identity: Identity = Depends(require_member),
                    row: Any = Depends(mutable_provider),
                    _: None = Depends(_reject_unknown_provider_update_fields)) -> dict[str, Any]:
    """`name` / `kind` are immutable: a body carrying them (or any other unknown key) answers
    422 `model_provider.invalid_update {fields, immutable, allowed}` (D21-obs8). Creator-or-admin
    (N17-obs203); `scope` is an administrator's field, and so are `base_url` /
    `credential_ssm_parameter` / `api_key` on a workspace-scope row (N17-obs225). Sharing a
    personal row (`scope: personal → workspace`) transfers it to the workspace: `created_by`
    becomes NULL and the former creator loses mutation rights (the journal row names them).
    N17-obs228: a `base_url` change on a row holding a registered key needs `api_key` in the
    same body, otherwise the stored key is cleared (SSM if console-written, Identity, hub
    record, cache — best-effort) and the row reads 「未配置」 until a key is entered again; the
    journal note names the old and the new host either way."""
    data = body.model_dump(exclude_unset=True)
    api_key = data.pop("api_key", None)  # write-only; never reaches the registry / journal
    if "scope" in data:
        _require_admin_for_scope(identity, data.get("scope"))
    # N17-obs225 (rev-r20 B1): on a shared row the endpoint, the parameter and the key are an
    # administrator's — checked before the typed-key probe, so a forbidden caller sends nothing
    registry.require_admin_for_credential_fields(row, identity, data, api_key=api_key is not None)
    if api_key is not None:
        # N17-obs203 §3 "replace key": probe the (possibly new) endpoint with the typed key
        # before anything is written — on a failure neither the key nor the edit lands
        hooks = registry.require_probe_hooks("credential")
        hooks.probe_api_key(_probe_target(row, data), api_key)
    # N17-obs228: a `base_url` change on a row that holds a registered key either comes with
    # the key re-entered (probed against the NEW host above) or clears the stored key — the
    # next probe / model call must never carry the old host's key to the new one
    note: str | None = None
    clear = False
    snapshot = None
    if registry._changes_field(row, "base_url", data) and registry.hub_holds_key(
            row, host_env=False):
        old_host, new_host = registry.host_of(row.base_url), registry.host_of(data["base_url"])
        if api_key is not None:
            note = f"base_url changed from {old_host} to {new_host}; key re-entered"
        else:
            clear = True
            # N17-obs230: the row's credential facts BEFORE the write — the hook needs them
            # (which parameter, whether the console wrote it) once the row has been reset
            snapshot = _probe_target(row, {})
            snapshot.key_entered_at = row.key_entered_at
            note = (f"base_url changed from {old_host} to {new_host}; the stored key was "
                    "cleared — enter the key again")
    # N17-obs230: the row write (and every validation / refusal inside it — 422
    # invalid_base_url, 409 in_use on a disable in the same body) comes FIRST; a rejected body
    # must leave the stored key exactly where it was. Only after the commit does the hook
    # remove the old host's key (its deletes are independent of the row write).
    row = registry.update_provider(db, row, data, _actor(identity), note=note,
                                   clear_credential=clear)
    if clear:
        hooks_now = registry.probe_hooks()
        if hooks_now is None:
            # N17-obs231: without the probe module nothing reaches SSM / Identity, but the
            # workspace records must not keep saying `configured` (they are each workspace's
            # availability; the hub's SSM read is gated on the row since N17-obs280) — the
            # local ledger half runs regardless and the journal says what was left in place
            count = registry.unconfigure_credential_record(db, snapshot.name,
                                                           cleared_on_host_change=True)
            residue: dict[str, Any] = {"ssm_parameter": snapshot.credential_ssm_parameter,
                                       "ssm_deleted": False, "identity_deleted": False,
                                       "record_unconfigured": count,
                                       "error": "no probe module registered"}
        else:
            try:
                residue = hooks_now.clear_api_key(db, snapshot, _actor(identity))
            except registry.CredentialClearError as exc:
                # N17-obs231: every step ran; the residue row carries the error, then the
                # caller learns the clearing was partial (the row write itself stands)
                registry.record_cleared_key(db, row, _actor(identity),
                                            note=_cleared_note(exc.report))
                raise
        registry.record_cleared_key(db, row, _actor(identity), note=_cleared_note(residue))
    if api_key is not None:
        hooks.store_api_key(db, row, api_key, _actor(identity))
        del api_key
    return _provider_out(db, row, identity)


def _cleared_note(residue: dict[str, Any]) -> str:
    """The `provider.credential` journal note of a key cleared on a host change — the residue
    report (names only, never a value) plus the `error` field when a step failed (N17-obs231)."""
    return (f"stored key cleared on host change: SSM parameter {residue.get('ssm_parameter')} "
            f"{'deleted' if residue.get('ssm_deleted') else 'kept'}, Identity provider "
            f"{'deleted' if residue.get('identity_deleted') else 'kept'}"
            + (f", error: {residue['error']}" if residue.get("error") else ""))


def _probe_target(row: Any, data: dict[str, Any]) -> Any:
    """A transient copy of ``row`` with the update's `base_url` applied (validated), for the
    typed-key probe; never added to the session."""
    from app.models.model_registry import ModelProvider

    base_url = row.base_url
    if "base_url" in data:
        base_url = registry.validate_base_url(data["base_url"], row.kind)
    return ModelProvider(id=row.id, name=row.name, kind=row.kind, label=row.label,
                         base_url=base_url, model_region=row.model_region,
                         credential_provider=row.credential_provider,
                         credential_ssm_parameter=row.credential_ssm_parameter,
                         credential_source=row.credential_source, scope=row.scope)


@router.delete("/{provider_id}", status_code=204, response_class=Response)
def delete_provider(provider_id: str, db: Session = Depends(get_db),
                    identity: Identity = Depends(require_member),
                    row: Any = Depends(mutable_provider)) -> Response:
    """409 `model_provider.builtin` for the built-in rows; 409 `model_provider.in_use` (with
    the referrers) while any of its models is referenced. The Identity credential provider
    is deliberately not deleted (rotation history). Creator-or-admin (N17-obs203). A key the
    CONSOLE wrote (§3) is deleted from SSM best-effort after the refusals and before the row;
    the residue report lands in the `provider.delete` journal note."""
    registry.assert_provider_deletable(db, row)
    note: str | None = None
    hooks = registry.probe_hooks()
    if hooks is not None and row.key_entered_at is not None:
        residue = hooks.forget_api_key(db, row, _actor(identity))
        note = (f"SSM parameter {residue.get('ssm_parameter')} "
                + ("deleted" if residue.get("deleted")
                   else f"left behind ({residue.get('error') or residue.get('skipped')})"))
    if hooks is not None:
        # N17-obs229: the hub / spoke credential record must not outlive the row either
        hooks.forget_credential_record(db, row)
    registry.delete_provider(db, row, _actor(identity), note=note)
    return Response(status_code=204)


@router.post("/{provider_id}/credential", response_model=CredentialOut)
def register_credential(provider_id: str, body: CredentialRegisterIn | None = None,
                        db: Session = Depends(get_db),
                        identity: Identity = Depends(require_member),
                        row: Any = Depends(mutable_provider)) -> dict[str, Any]:
    """Plan §4 (M1-b): read the SSM parameter named on the row → create / rotate the
    Identity API-key provider → record configured-ness on the hub config and every
    workspace. Never returns or logs the value. 501 until M1-b registers its hooks.
    Creator-or-admin (N17-obs203). With a body `{api_key}` (§3 "replace key") the typed key
    is probed, written to the row's parameter by the console host and then registered.
    N17-obs225: on a workspace-scope row both paths are an administrator's — a body-less call
    reads the OPERATOR's parameter and registers it for the row's Identity provider, a typed
    key overwrites that parameter."""
    registry.require_admin_for_credential_fields(row, identity, action="credential")
    hooks = registry.require_probe_hooks("credential")
    api_key = body.api_key if body is not None else None
    if api_key is None:
        return hooks.register_credential(db, row, _actor(identity))
    hooks.probe_api_key(row, api_key)
    result = hooks.store_api_key(db, row, api_key, _actor(identity))
    del api_key
    return result


@router.post("/{provider_id}/probe", response_model=ProviderProbeOut)
def probe_provider(provider_id: str, db: Session = Depends(get_db),
                   identity: Identity = Depends(require_member),
                   row: Any = Depends(mutable_provider)) -> dict[str, Any]:
    """Plan §7.1 (M1-b): connectivity + auth + `models_seen`; stored into `probe`.
    501 until M1-b registers its hooks. Creator-or-admin (N17-obs203: a probe sends the
    row's key to its endpoint and writes the row)."""
    hooks = registry.require_probe_hooks("probe")
    return hooks.probe_provider(db, row, _actor(identity))


# ── models of a provider ─────────────────────────────────────────────────────────────
@router.post("/{provider_id}/models", status_code=201, response_model=ModelOut)
def create_model(provider_id: str, body: ModelCreate, db: Session = Depends(get_db),
                 identity: Identity = Depends(require_member),
                 provider: Any = Depends(mutable_provider)) -> dict[str, Any]:
    """Launchpad id = `id_prefix + api_model_name` (409 `model.exists`). With ``probe: true``
    (the default) the §7.2/7.3 probes run BEFORE the insert and fill the capabilities
    (`source: probed`, per-field `capability_sources`); ``overrides`` apply after (`manual`).
    ``probe: false`` inserts the kind defaults as `manual`. Until M1-b registers its hooks a
    probing create answers 501 — re-submit with ``probe: false`` for a manual row.
    Creator-or-admin on the provider (N17-obs203)."""
    data = body.model_dump()
    probed: dict[str, Any] | None = None
    if body.probe:
        hooks = registry.require_probe_hooks("probe")
        result = hooks.probe_model(db, provider, body.api_model_name, body.kind, None,
                                   apply=False, actor=_actor(identity))
        probed = dict(result.get("findings") or {})
        errors = dict(result.get("errors") or {})
        # plan §7.4 / rev-r8 M3: a probe that cannot find the model does not insert it; a
        # partial probe (exists, some fields unsettled) does, with those fields `unknown`
        if probed.get("exists") is False or (errors and probed.get("exists") is not True):
            raise AppError(registry.E_MODEL_PROBE_FAILED,
                           f"the probe could not reach {body.api_model_name!r} on "
                           f"{provider.label} — re-submit with probe: false for a manual row",
                           {"findings": probed, "errors": errors}, status_code=422)
        data["probe_record"] = result.get("record")
    row = registry.create_model(db, provider, data, _actor(identity), probed=probed)
    return registry.model_dict(row, provider)


@router.put("/{provider_id}/models/{model_ref:path}", response_model=ModelOut)
def update_model(provider_id: str, model_ref: str, body: ModelUpdate,
                 db: Session = Depends(get_db),
                 identity: Identity = Depends(require_member),
                 provider: Any = Depends(mutable_provider)) -> dict[str, Any]:
    """``model_ref`` = the row id (`md_…`) or the Launchpad model id. Every capability field
    sent here flips its `capability_sources[field]` to `manual`; `is_default` for a chat row
    requires enabled + tools + a hub-configured provider; for an embedding row it goes
    through the §6 reindex guard once M1-d registers it. Creator-or-admin on the provider
    (N17-obs203); `is_default` moves the platform default and stays an administrator's."""
    if body.is_default is not None and not identity.is_admin:
        raise AppError(registry.E_PROVIDER_FORBIDDEN,
                       "only an administrator may change the default model",
                       {"provider_id": provider.id, "action": "is_default"}, status_code=403)
    row = registry.get_model(db, provider, model_ref)
    row = registry.update_model(db, provider, row, body.model_dump(exclude_unset=True),
                                _actor(identity))
    return registry.model_dict(row, provider)


@router.delete("/{provider_id}/models/{model_ref:path}", status_code=204,
               response_class=Response)
def delete_model(provider_id: str, model_ref: str, db: Session = Depends(get_db),
                 identity: Identity = Depends(require_member),
                 provider: Any = Depends(mutable_provider)) -> Response:
    """409 `model.in_use` (referrers; suggests *disable*), 409 `model.builtin` for seeded rows
    (disable instead), 409 `model.is_default`. Creator-or-admin on the provider (N17-obs203)."""
    registry.delete_model(db, provider, registry.get_model(db, provider, model_ref),
                          _actor(identity))
    return Response(status_code=204)


@router.post("/{provider_id}/models/{model_ref:path}/probe", response_model=ModelProbeOut)
def probe_model(provider_id: str, model_ref: str, body: ModelProbeIn | None = None,
                db: Session = Depends(get_db),
                identity: Identity = Depends(require_member),
                provider: Any = Depends(mutable_provider)) -> dict[str, Any]:
    """Plan §7.4 (M1-b): re-probe → `{findings, diff}`; with ``apply: true`` probed values
    are written only into fields whose source is not `manual`. 501 until M1-b registers.
    Creator-or-admin on the provider (N17-obs203)."""
    row = registry.get_model(db, provider, model_ref)
    hooks = registry.require_probe_hooks("probe")
    return hooks.probe_model(db, provider, row.api_model_name, row.kind, row,
                             apply=bool(body.apply) if body else False,
                             actor=_actor(identity))
