"""Owner-bound model providers (N17-obs203, user decision 2026-10-21).

"launchpad 和模型相对独立：无论管理员还是普通成员都可以增加、删除、修改自己加的模型提供方
（管理员可以修改或删除普通成员的）" — every signed-in member registers their OWN providers
(`scope: personal`), reads / changes / deletes them, adds models, probes and registers keys; an
administrator does everything, shares a provider (`scope: workspace`) and touches members' rows.
The two gates of users-and-ownership.md §7–§8 apply in their order: another member's personal
provider is INVISIBLE (404 `model_provider.not_found` on every route, identical to a missing
id), a visible row the caller did not create is 403 `model_provider.forbidden`; both hold in
`shared` and `isolated` workspaces alike (the registry is hub-global). A personal provider's
credential references are owner-derived (`launchpad-user-<uid>-<name>-api-key`, Identity
`launchpad-user-<uid>-<name>`, no host-env fallback) so a member can never point their own
endpoint at a parameter or variable holding someone else's key. Legacy rows (a username in
`created_by`, no `scope`) are re-pointed once at startup like the other Family B tables.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base, SessionLocal, assert_no_schema_drift
from app.models.model_registry import ModelCatalogChange, ModelProvider
from app.schemas import model_catalog
from app.services import model_registry, visibility
from tests import test_visibility as tv

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

PREFIX = "/api/settings/model-providers"
URL = "https://api.ppinfra.com/v3/openai"
pytestmark = pytest.mark.siliconflow


@pytest.fixture(autouse=True)
def _hermetic():
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()


def _create(client, name="ppio", **extra):
    body = {"name": name, "kind": "openai_compatible", "label": name.title(), "base_url": URL,
            **extra}
    res = client.post(PREFIX, json=body)
    assert res.status_code == 201, res.text
    return res.json()


def _names(client):
    res = client.get(PREFIX)
    assert res.status_code == 200, res.text
    return [p["name"] for p in res.json()["providers"]]


# ── creation ─────────────────────────────────────────────────────────────────────────
def test_a_member_creates_a_personal_provider_with_owner_scoped_credential_references(ada):
    ada_id, client = ada
    row = _create(client)
    assert row["scope"] == "personal" and row["owned"] is True
    assert row["created_by"] == ada_id and row["created_by_name"] == "ada"
    # owner-derived references: SSM parameter + Identity provider carry the owner's id, and
    # there is NO host-env fallback (the variable name could collide with a real key's)
    assert row["credential_ssm_parameter"] == f"launchpad-user-{ada_id}-ppio-api-key"
    assert row["credential_provider"] == f"launchpad-user-{ada_id}-ppio"
    assert row["credential_env"] is None and "credential" not in row  # N17-obs283: flat only
    assert row["configured"] is False and row["credential_source"] == "ssm"
    # the derived name still satisfies the ops role's `launchpad-*-api-key` grant pattern
    assert model_registry.SSM_PARAMETER_RE.fullmatch(row["credential_ssm_parameter"])
    # a member passing `scope: personal` explicitly is the same thing; `workspace` is refused
    assert _create(client, name="ppio-two", scope="personal")["scope"] == "personal"
    refused = client.post(PREFIX, json={"name": "shared", "kind": "openai_compatible",
                                        "label": "S", "base_url": URL, "scope": "workspace"})
    assert refused.status_code == 403 and refused.json()["code"] == "model_provider.forbidden"
    assert refused.json()["detail"] == {"scope": "workspace", "action": "scope"}
    # a member cannot choose the parameter (= cannot point at another provider's key)
    hijack = client.post(PREFIX, json={"name": "evil", "kind": "openai_compatible",
                                       "label": "E", "base_url": URL,
                                       "credential_ssm_parameter":
                                           "launchpad-global-bedrock-api-key"})
    assert hijack.status_code == 422
    assert hijack.json()["code"] == "model_provider.invalid_ssm_parameter"
    assert hijack.json()["detail"]["expected"] == f"launchpad-user-{ada_id}-evil-api-key"
    assert "evil" not in _names(client)


def test_an_administrator_creates_workspace_scope_by_default_and_may_pick_personal(admin):
    shared = _create(admin, name="shared")
    assert shared["scope"] == "workspace" and shared["owned"] is False  # config admin: no row
    assert shared["created_by"] is None and shared["created_by_name"] == tv.ADMIN["username"]
    assert shared["credential_ssm_parameter"] == "launchpad-shared-api-key"
    assert shared["credential_env"] == "LAUNCHPAD_SHARED_API_KEY"
    own = _create(admin, name="adminown", scope="personal")
    assert own["scope"] == "personal"
    # the config admin has no users.id: the personal names fall back to the plain ones
    assert own["credential_ssm_parameter"] == "launchpad-adminown-api-key"
    bad = admin.post(PREFIX, json={"name": "x", "kind": "openai_compatible", "label": "X",
                                   "base_url": URL, "scope": "team"})
    assert bad.status_code == 422 and bad.json()["code"] == "validation.invalid_request"


# ── visibility (404 first) and mutation (403 second), both modes ─────────────────────
@pytest.mark.parametrize("mode", [visibility.SHARED, visibility.ISOLATED])
def test_another_members_personal_provider_is_invisible_in_both_modes(admin, ada, bob, mode):
    tv._isolate(value=mode)
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    row = _create(ada_client)
    model = ada_client.post(f"{PREFIX}/{row['id']}/models",
                            json={"api_model_name": "deepseek/deepseek-v4", "probe": False})
    assert model.status_code == 201, model.text
    # ada and the administrator see it; bob does not — not in the list, 404 by id and by name
    assert "ppio" in _names(ada_client) and "ppio" in _names(admin)
    assert "ppio" not in _names(bob_client)
    bob_page = bob_client.get(PREFIX).json()
    assert all(m["provider_id"] != row["id"] for m in bob_page["models"])
    for ref in (row["id"], "ppio"):
        res = bob_client.get(f"{PREFIX}/{ref}")
        assert res.status_code == 404 and res.json()["code"] == "model_provider.not_found", ref
    for method, path in (("PUT", f"{PREFIX}/{row['id']}"), ("DELETE", f"{PREFIX}/{row['id']}"),
                         ("POST", f"{PREFIX}/{row['id']}/probe"),
                         ("POST", f"{PREFIX}/{row['id']}/credential"),
                         ("POST", f"{PREFIX}/{row['id']}/models"),
                         ("PUT", f"{PREFIX}/{row['id']}/models/{model.json()['id']}"),
                         ("DELETE", f"{PREFIX}/{row['id']}/models/{model.json()['id']}"),
                         ("POST", f"{PREFIX}/{row['id']}/models/{model.json()['id']}/probe")):
        res = bob_client.request(method, path, json={})
        assert res.status_code == 404, (method, path, res.text)
        assert res.json()["code"] == "model_provider.not_found"
    # the row is untouched
    assert ada_client.get(f"{PREFIX}/{row['id']}").json()["label"] == "Ppio"


@pytest.mark.parametrize("mode", [visibility.SHARED, visibility.ISOLATED])
def test_sharing_transfers_the_provider_to_the_workspace_and_members_read_it_only(
    admin, ada, bob, mode,
):
    """N17-obs225 (rev-r20 B1): a shared (workspace-scope) row is the WORKSPACE's — sharing
    moves `created_by` to NULL (= an administrator's), the former creator reads it like every
    member and may not change it; the journal row names the former creator."""
    tv._isolate(value=mode)
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    row = _create(ada_client)
    # sharing is the administrator's: ada cannot flip her own scope
    own_flip = ada_client.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"})
    assert own_flip.status_code == 403 and own_flip.json()["code"] == "model_provider.forbidden"
    shared = admin.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"})
    assert shared.status_code == 200 and shared.json()["scope"] == "workspace"
    # ownership transferred: no creator any more, nobody but an administrator "owns" it
    assert shared.json()["created_by"] is None and shared.json()["created_by_name"] is None
    assert shared.json()["owned"] is False
    flip_row = [c for c in admin.get(f"{PREFIX}/changes?target_id={row['id']}").json()["items"]
                if c["operation"] == "provider.update"][0]
    assert flip_row["before"]["created_by"] == ada_id and flip_row["after"]["created_by"] is None
    db = SessionLocal()
    try:  # the journal NOTE (a ledger column, not an API field) names the former creator
        note = db.get(ModelCatalogChange, flip_row["id"]).note
    finally:
        db.close()
    assert "ownership transferred" in note and ada_id in note and "ada" in note
    # bob AND ada now see it (list + GET) …
    for client in (bob_client, ada_client):
        assert "ppio" in _names(client)
        seen = client.get(f"{PREFIX}/{row['id']}").json()
        assert seen["owned"] is False and seen["created_by_name"] is None
    # … and neither may change it: 403 names the row, before any body check
    for client in (bob_client, ada_client):
        for method, path in (("PUT", f"{PREFIX}/{row['id']}"),
                             ("DELETE", f"{PREFIX}/{row['id']}"),
                             ("POST", f"{PREFIX}/{row['id']}/models"),
                             ("POST", f"{PREFIX}/{row['id']}/probe"),
                             ("POST", f"{PREFIX}/{row['id']}/credential")):
            res = client.request(method, path, json={"name": "x"})
            assert res.status_code == 403, (method, path, res.text)
            body = res.json()
            assert body["code"] == "model_provider.forbidden"
            assert body["detail"]["provider_id"] == row["id"]
            assert body["detail"]["created_by"] is None
    # the administrator edits it
    assert admin.put(f"{PREFIX}/{row['id']}", json={"label": "PPIO cloud"}).status_code == 200
    # unsharing leaves it an administrator's personal row: both members lose it (404)
    assert admin.put(f"{PREFIX}/{row['id']}", json={"scope": "personal"}).json()["scope"] == (
        "personal")
    assert bob_client.get(f"{PREFIX}/{row['id']}").status_code == 404
    assert ada_client.get(f"{PREFIX}/{row['id']}").status_code == 404
    assert admin.get(f"{PREFIX}/{row['id']}").status_code == 200


def test_the_owner_of_a_personal_row_cannot_move_the_platform_default(ada):
    """The `is_default` half of the retired shared-row test: members never move the default."""
    _ada_id, client = ada
    row = _create(client)
    model = client.post(f"{PREFIX}/{row['id']}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False}).json()
    default = client.put(f"{PREFIX}/{row['id']}/models/{model['id']}",
                         json={"is_default": True})
    assert default.status_code == 403 and default.json()["detail"]["action"] == "is_default"


def _legacy_shared_row_created_by(user_id: str, username: str, name: str = "legacy-vendor"):
    """A WORKSPACE-scope row whose `created_by` is a (non-admin) member — what the obs220
    re-point produces for a pre-obs203 row an operator's account created before being demoted,
    or what any pre-obs225 ledger holds for a shared member row."""
    db = SessionLocal()
    try:
        row = model_registry.create_provider(
            db, {"name": name, "kind": "openai_compatible", "label": "Legacy vendor",
                 "base_url": URL, "scope": "workspace"}, "system")
        row.created_by = user_id
        row.created_by_name = username
        db.commit()
        model_registry.refresh(db)
        return row.id
    finally:
        db.close()


def test_a_non_admin_creator_of_a_shared_row_keeps_label_enabled_and_models_only(ada, admin):
    """N17-obs225 (rev-r20 B1), the legacy-creator half: on a workspace-scope row the creator
    (not an administrator) may change label / enabled and the models, but NOT `base_url`, the
    SSM parameter (the Advanced path) or the key — each is 403 naming the field; an unchanged
    echo of `base_url` / the parameter (what the edit drawer sends on every save) is not a
    change. An administrator may change all of them."""
    ada_id, client = ada
    pid = _legacy_shared_row_created_by(ada_id, "ada")
    before = client.get(f"{PREFIX}/{pid}").json()
    assert before["scope"] == "workspace" and before["owned"] is True
    # allowed: label / enabled, the same base_url and parameter echoed back, models
    same = client.put(f"{PREFIX}/{pid}", json={"label": "Legacy", "enabled": True,
                                               "base_url": URL,
                                               "credential_ssm_parameter":
                                                   before["credential_ssm_parameter"]})
    assert same.status_code == 200, same.text
    assert client.put(f"{PREFIX}/{pid}", json={"base_url": URL + "/"}).status_code == 200
    model = client.post(f"{PREFIX}/{pid}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False})
    assert model.status_code == 201, model.text
    # forbidden: the endpoint, the parameter, the key (typed or read from the parameter)
    for body, action in (
        ({"base_url": "https://evil.example.com/v1"}, "base_url"),
        ({"credential_ssm_parameter": "launchpad-siliconflow-api-key"}, "credential_ssm_parameter"),
        ({"label": "x", "api_key": "sk-typed-0000"}, "api_key"),
    ):
        res = client.put(f"{PREFIX}/{pid}", json=body)
        assert res.status_code == 403, (body, res.text)
        assert res.json()["code"] == "model_provider.forbidden"
        assert res.json()["detail"]["action"] == action
        assert res.json()["detail"]["scope"] == "workspace"
    for body in (None, {"api_key": "sk-typed-0000"}):
        res = client.post(f"{PREFIX}/{pid}/credential", json=body)
        assert res.status_code == 403, res.text
        assert res.json()["detail"]["action"] == "credential"
    after = client.get(f"{PREFIX}/{pid}").json()
    assert after["base_url"] == URL and after["credential_ssm_parameter"] == (
        before["credential_ssm_parameter"])
    # an administrator changes the endpoint and the parameter on the same row
    adm = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.ppinfra.com/v4",
                                             "credential_ssm_parameter":
                                                 "launchpad-legacy-vendor-two-api-key"})
    assert adm.status_code == 200, adm.text
    assert adm.json()["base_url"] == "https://api.ppinfra.com/v4"


def test_the_built_in_bedrock_row_is_an_administrators(ada):
    """NULL creator = an administrator's (the CN rule): readable, never mutable by a member."""
    _ada_id, client = ada
    bedrock = client.get(f"{PREFIX}/bedrock")
    assert bedrock.status_code == 200
    assert bedrock.json()["scope"] == "workspace" and bedrock.json()["created_by"] is None
    assert bedrock.json()["created_by_name"] is None and bedrock.json()["owned"] is False
    res = client.put(f"{PREFIX}/bedrock", json={"label": "Mine now"})
    assert res.status_code == 403 and res.json()["code"] == "model_provider.forbidden"


# ── the owner's own writes ───────────────────────────────────────────────────────────
def test_the_owner_edits_adds_models_to_and_deletes_their_provider(ada, admin):
    ada_id, client = ada
    row = _create(client)
    edited = client.put(f"{PREFIX}/{row['id']}",
                        json={"label": "PPIO", "base_url": "https://api.ppinfra.com/v3",
                              "enabled": True})
    assert edited.status_code == 200, edited.text
    assert edited.json()["label"] == "PPIO" and edited.json()["owned"] is True
    # the parameter name is owner-derived: an attempt to repoint it is refused
    repoint = client.put(f"{PREFIX}/{row['id']}",
                         json={"credential_ssm_parameter": "launchpad-siliconflow-api-key"})
    assert repoint.status_code == 422
    assert repoint.json()["code"] == "model_provider.invalid_ssm_parameter"
    # sending the derived name itself is a no-op, not an error
    same = client.put(f"{PREFIX}/{row['id']}",
                      json={"credential_ssm_parameter": row["credential_ssm_parameter"]})
    assert same.status_code == 200
    model = client.post(f"{PREFIX}/{row['id']}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False})
    assert model.status_code == 201 and model.json()["model_id"] == "ppio.deepseek/deepseek-v4"
    assert client.put(f"{PREFIX}/{row['id']}/models/{model.json()['id']}",
                      json={"label": "DeepSeek V4"}).status_code == 200
    assert client.delete(f"{PREFIX}/{row['id']}/models/{model.json()['id']}").status_code == 204
    # the journal records the member as the actor — and is the administrator's to read
    assert client.get(f"{PREFIX}/changes").status_code == 403
    changes = admin.get(f"{PREFIX}/changes").json()["items"]
    mine = [c for c in changes if c["target_id"] == row["id"]]
    assert mine and {c["actor"] for c in mine} == {"ada"}
    assert mine[-1]["operation"] == "provider.create"
    assert mine[-1]["after"]["created_by"] == ada_id and mine[-1]["after"]["scope"] == "personal"
    assert client.delete(f"{PREFIX}/{row['id']}").status_code == 204
    assert client.get(f"{PREFIX}/{row['id']}").status_code == 404
    # an administrator deletes a member's provider too (the "admins may delete members'" half)
    other = _create(client, name="ppio-b")
    assert admin.delete(f"{PREFIX}/{other['id']}").status_code == 204


def test_two_members_may_use_the_same_provider_name_space_only_once(ada, bob):
    """`name` is unique hub-wide (it is the Launchpad id prefix); a second member picking a
    taken name gets the existing 409 — the owner-scoped parameter keeps their KEYS apart, not
    their names."""
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    _create(ada_client)
    res = bob_client.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                        "label": "Mine", "base_url": URL})
    assert res.status_code == 409 and res.json()["code"] == "model_provider.exists"
    # and bob still cannot see whose it is
    assert "ppio" not in _names(bob_client)


# ── the snapshot carries the owner / scope (pure data for the pickers) ───────────────
def test_the_published_snapshot_carries_owner_and_scope(ada):
    ada_id, client = ada
    row = _create(client)
    prow = model_catalog.current().providers["ppio"]
    assert prow.owner_id == ada_id and prow.scope == "personal" and prow.id == row["id"]
    assert model_catalog.current().providers["bedrock"].scope == "workspace"
    assert model_catalog.current().providers["bedrock"].owner_id is None
    # the legacy row is unchanged (the owner is not part of the pre-D21 shape)
    assert set(prow.legacy()) == set(model_catalog.LEGACY_PROVIDER_KEYS)


# ── the one-time legacy re-point ─────────────────────────────────────────────────────
NOW = "2026-01-01 00:00:00"


def test_legacy_provider_rows_are_re_pointed_and_default_to_workspace_scope(tmp_path):
    """A pre-obs203 ledger: `model_providers.created_by` holds the journal ACTOR (a username;
    `system` for the seed) and there is no `created_by_name` / `scope`. Startup moves the
    username to the display column, resolves the ownership key to the account (NULL for
    `system`, the config admin or a deleted account) and gives every existing row — all of
    them administrators' hub-wide registrations — `scope: workspace`. Idempotent."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX IF EXISTS ix_model_providers_created_by"))
        conn.execute(sa.text("ALTER TABLE model_providers DROP COLUMN created_by_name"))
        conn.execute(sa.text("ALTER TABLE model_providers DROP COLUMN scope"))
        conn.execute(sa.text(
            "INSERT INTO users (id, username, username_key, email, password_hash, role,"
            " status, created_by, login_count, created_at, updated_at) VALUES"
            f" ('u-ada', 'Ada', 'ada', 'ada@x.cn', 'h', 'admin', 'active', 'self', 0,"
            f" '{NOW}', '{NOW}')"))
        for pid, name, actor in (("mp-bed", "bedrock", "system"), ("mp-sf", "siliconflow", "ada"),
                                 ("mp-op", "acme", "operator"), ("mp-null", "nullp", None)):
            actor_sql = "NULL" if actor is None else f"'{actor}'"
            conn.execute(sa.text(
                "INSERT INTO model_providers (id, name, kind, label, credential_provider,"
                " credential_source, required, builtin, enabled, id_prefix, created_by,"
                " created_at, updated_at) VALUES"
                f" ('{pid}', '{name}', 'openai_compatible', '{name}', 'launchpad-{name}', 'ssm',"
                f" 0, 0, 1, '{name}.', {actor_sql}, '{NOW}', '{NOW}')"))
    db_module._migrate(engine)
    assert_no_schema_drift(engine)
    with engine.begin() as conn:
        rows = {r[0]: tuple(r[1:]) for r in conn.execute(sa.text(
            "SELECT id, created_by, created_by_name, scope FROM model_providers"))}
    assert rows == {
        "mp-bed": (None, None, "workspace"),  # the seed actor records no creator
        "mp-sf": ("u-ada", "ada", "workspace"),  # resolved to the account, case-insensitively
        "mp-op": (None, "operator", "workspace"),  # the config admin: name kept, no owner id
        "mp-null": (None, None, "workspace"),
    }
    db_module._migrate(engine)  # a second start is a no-op
    with engine.begin() as conn:
        again = {r[0]: tuple(r[1:]) for r in conn.execute(sa.text(
            "SELECT id, created_by, created_by_name, scope FROM model_providers"))}
    assert again == rows


def test_the_seed_records_no_creator_on_the_built_in_rows():
    db = SessionLocal()
    try:
        model_registry.ensure_seeded(db)
        bedrock = db.scalar(sa.select(ModelProvider).where(ModelProvider.name == "bedrock"))
        assert bedrock.created_by is None and bedrock.created_by_name is None
        assert bedrock.scope == "workspace" and bedrock.updated_by == "system"
    finally:
        db.close()


# ── delete (N17-obs207: delete must be reachable for everything but the hub Bedrock row) ──
def test_only_the_hub_bedrock_provider_is_undeletable_and_the_legacy_preset_is_unpinned(admin):
    """A pre-R13 ledger carried SiliconFlow as `builtin` (undeletable); the 2026-10-17 decision
    retired the preset, so startup (`init_db`'s migration funnel) flips the flag once and an
    administrator may delete it like any provider; the hub Bedrock row keeps 409
    `model_provider.builtin`."""
    db = SessionLocal()
    try:
        db.add(ModelProvider(
            name="siliconflow", kind="openai_compatible", label="SiliconFlow",
            base_url="https://api.siliconflow.cn/v1", credential_provider="launchpad-siliconflow",
            credential_ssm_parameter="launchpad-siliconflow-api-key", credential_source="ssm",
            credential_env="LAUNCHPAD_SILICONFLOW_API_KEY", required=False, builtin=True,
            enabled=False, id_prefix="siliconflow.", probe={"state": "failed", "error": "x"},
            scope="workspace"))
        db.commit()
        # the startup path — N17-obs282: the obs207 unpin is a migration in `init_db()` now
        from app.core.db import init_db

        init_db()
        db.expire_all()
        model_registry.ensure_seeded(db)
        row = db.scalar(sa.select(ModelProvider).where(ModelProvider.name == "siliconflow"))
        assert row.builtin is False and row.required is False
        changes = model_registry.list_changes(db, target_id=row.id)
        assert changes == []  # no journal row: the rendered catalog did not change
    finally:
        db.close()
    page = admin.get(PREFIX).json()
    by_name = {p["name"]: p for p in page["providers"]}
    assert by_name["siliconflow"]["builtin"] is False and by_name["bedrock"]["builtin"] is True
    assert admin.delete(f"{PREFIX}/siliconflow").status_code == 204
    assert admin.get(f"{PREFIX}/siliconflow").status_code == 404
    refused = admin.delete(f"{PREFIX}/bedrock")
    assert refused.status_code == 409 and refused.json()["code"] == "model_provider.builtin"


def test_a_referenced_provider_refuses_delete_naming_the_agents_and_roles(admin, ada):
    from app.models.ledger import Agent

    _ada_id, client = ada
    row = _create(client)
    model = client.post(f"{PREFIX}/{row['id']}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False}).json()
    assert admin.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"}).status_code == 200
    db = SessionLocal()
    try:
        db.add(Agent(name="member-agent", method="zip_runtime", status="active",
                     workspace_id="default",
                     spec={"name": "member-agent", "method": "zip_runtime",
                           "model_id": model["model_id"]}))
        db.add(Agent(name="preset-agent", method="harness", status="active",
                     workspace_id="default", system_key="eval-worker",
                     spec={"name": "preset-agent", "method": "harness",
                           "model_id": model["model_id"]}))
        db.commit()
    finally:
        db.close()
    assert admin.put("/api/settings/model-roles/judge",
                     json={"model_id": model["model_id"]}).status_code == 200
    # N17-obs225: sharing transferred the row to the workspace — the former creator may not
    # delete it any more (403 before the referrer check); the administrator gets the 409
    forbidden = client.delete(f"{PREFIX}/{row['id']}")
    assert forbidden.status_code == 403 and forbidden.json()["code"] == "model_provider.forbidden"
    for who in (admin,):
        res = who.delete(f"{PREFIX}/{row['id']}")
        assert res.status_code == 409 and res.json()["code"] == "model_provider.in_use"
        detail = res.json()["detail"]
        assert sorted((a["name"], a["system_key"]) for a in detail["agents"]) == [
            ("member-agent", None), ("preset-agent", "eval-worker")]
        assert [(r["role"], r["field"]) for r in detail["roles"]] == [
            ("judge", "eval_judge_model_id")]
        assert all({"id", "name", "workspace_id", "system_key"} <= set(a)
                   for a in detail["agents"])
        assert detail["referrers"]  # the console's existing list stays
    assert client.get(f"{PREFIX}/{row['id']}").status_code == 200  # nothing deleted


# ── N17-obs205: a disabled, unconfigured provider's stale probe is not a page warning ─────
def test_a_disabled_unconfigured_providers_failed_probe_is_not_a_page_warning(admin):
    db = SessionLocal()
    try:
        model_registry.ensure_seeded(db)
        stale = ModelProvider(
            name="legacy", kind="openai_compatible", label="Legacy", base_url=URL,
            credential_provider="launchpad-legacy",
            credential_ssm_parameter="launchpad-legacy-api-key",
            credential_source="ssm", credential_env="LAUNCHPAD_LEGACY_API_KEY", enabled=False,
            id_prefix="legacy.", scope="workspace",
            probe={"state": "failed", "error": "credential_unavailable", "checked_at": "t",
                   "latency_ms": 1, "models_seen": []})
        live = ModelProvider(
            name="live", kind="openai_compatible", label="Live", base_url=URL,
            credential_provider="launchpad-live",
            credential_ssm_parameter="launchpad-live-api-key",
            credential_source="ssm", credential_env="LAUNCHPAD_LIVE_API_KEY", enabled=True,
            id_prefix="live.", scope="workspace",
            probe={"state": "failed", "error": "timeout", "checked_at": "t", "latency_ms": 1,
                   "models_seen": []})
        db.add_all([stale, live])
        db.commit()
        names = [w["detail"]["name"] for w in model_registry.warnings(db)
                 if w["code"] == "probe_failed"]
        assert names == ["live"]  # the disabled + unconfigured row is left out …
        stale.enabled = True  # … an ENABLED row with a failed probe is a warning again
        db.commit()
        names = [w["detail"]["name"] for w in model_registry.warnings(db)
                 if w["code"] == "probe_failed"]
        assert sorted(names) == ["legacy", "live"]
        stale.enabled = False
        stale.key_version = 3  # disabled but configured (a registered key): still a warning
        db.commit()
        assert "legacy" in [w["detail"]["name"] for w in model_registry.warnings(db)]
    finally:
        db.close()
    page = admin.get(PREFIX).json()
    assert {p["name"] for p in page["providers"]} >= {"legacy", "live"}
    # the card still carries its own last probe
    legacy = next(p for p in page["providers"] if p["name"] == "legacy")
    assert legacy["probe"]["state"] == "failed"
