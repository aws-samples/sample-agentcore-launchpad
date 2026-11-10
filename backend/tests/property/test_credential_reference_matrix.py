"""E08 — outbound credentials: the delete-guard matrix as a property.

Round 16, channel A (BUG-DISCOVERY-PLAN §E08 / §4). Credentials (api_key / oauth2) ×
referrer kinds (an agent's `model_credential` pin — own / another member's / the
administrator's / a deleted agent's —, a model registry provider's `credential_provider`,
a tool auth `connection`, nothing) × caller roles (administrator, member holding
`perm:identity.manage`, member without it) × workspace visibility (shared / isolated) ×
order (delete; repoint then delete; rename the provider then delete). Expected answers come
from `docs/cn/interfaces/model-credential.md` "Deletion guard (N13-obs22)", `docs/api.md`
(`DELETE /api/identity/connections/{kind}/{name}`), `users-and-ownership.md` §7–§8 and
`core/route_policy.py`; the ledger scan (`tests/property/ledger_invariants.py`) runs after
every example. The vault is the `MagicMock` control plane of `tests/test_identity_connections`.
"""

from __future__ import annotations

import copy
from itertools import count
from typing import Any

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, IdentityProvider
from app.models.model_registry import ModelProvider
from app.routers import identity as identity_router
from app.services import users as users_service
from tests import test_visibility as tv
from tests.property.ledger_invariants import assert_ledger_invariants
from tests.test_identity_connections import API_KEY, SECRET, make_control
from tests.test_visibility import _isolate

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob  # noqa: F811 — pytest fixtures by name

_SEQ = count()
HYP = settings(
    max_examples=120,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
REFERRERS = ("pin_own", "pin_other", "pin_admin", "pin_deleted", "provider", "tool_auth")
scenario_st = st.fixed_dictionaries(
    {
        "kind": st.sampled_from(["api_key", "oauth2"]),
        "referrers": st.lists(st.sampled_from(REFERRERS), max_size=5),
        "role": st.sampled_from(["admin", "manager", "plain"]),
        "isolated": st.booleans(),
        "order": st.sampled_from(["delete", "repoint", "rename_provider"]),
        "external": st.booleans(),  # the provider exists in the vault only (no ledger row)
    }
)


@pytest.fixture
def control(monkeypatch):
    stub = make_control()
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: stub)
    return stub


def _perm(user_id: str, granted: bool) -> None:
    db = SessionLocal()
    try:
        user = db.get(users_service.User, user_id)
        # users.AGENT_PERMISSIONS keys (route_policy prefixes them with `perm:`)
        user.permissions = None if granted else {"identity.manage": False}
        db.commit()
    finally:
        db.close()


def _reset() -> None:
    db = SessionLocal()
    try:
        db.query(Agent).delete()
        db.query(IdentityProvider).delete()
        db.query(ModelProvider).filter(ModelProvider.name.like("prop-%")).delete(
            synchronize_session=False
        )
        db.commit()
    finally:
        db.close()


def _agent(
    name: str,
    *,
    created_by: str | None,
    pin: str | None = None,
    tool_auth: str | None = None,
    status: str = "active",
) -> str:
    spec: dict[str, Any] = {"name": name, "model_id": "global.zai.glm-5.3"}
    if pin:
        spec["model_credential"] = pin
    if tool_auth:
        spec["tools"] = [{"type": "mcp", "name": "crm", "auth": {"connection": tool_auth}}]
    db = SessionLocal()
    try:
        row = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID,
            name=name,
            method="zip_runtime",
            status=status,
            created_by=created_by,
            created_by_name="someone" if created_by else None,
            spec=spec,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _provider(name: str, credential: str) -> str:
    db = SessionLocal()
    try:
        row = ModelProvider(
            name=name,
            kind="openai_compatible",
            label=name,
            base_url="https://api.example/v1",
            credential_provider=credential,
            id_prefix=f"{name}.",
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _create_connection(admin_client, control, kind: str, name: str) -> None:
    if kind == "api_key":
        res = admin_client.post(
            "/api/identity/connections/api-key", json={"name": name, "api_key": API_KEY}
        )
    else:
        res = admin_client.post(
            "/api/identity/connections/oauth2",
            json={
                "name": name,
                "vendor": "CustomOauth2",
                "template": "custom_oidc",
                "client_id": "cid",
                "client_secret": SECRET,
                "discovery_url": "https://idp.example/.well-known/openid-configuration",
            },
        )
    assert res.status_code == 201, res.text


def _vault(control, kind: str, names: list[str]) -> None:
    listing = {"credentialProviders": [{"name": n} for n in names]}
    if kind == "api_key":
        control.list_api_key_credential_providers.return_value = listing
        control.list_oauth2_credential_providers.return_value = {"credentialProviders": []}
    else:
        control.list_oauth2_credential_providers.return_value = listing
        control.list_api_key_credential_providers.return_value = {"credentialProviders": []}


@HYP
@given(scenario=scenario_st)
def test_delete_guard_matrix(admin, ada, bob, app, control, scenario):
    _reset()
    control.reset_mock()
    control.get_api_key_credential_provider.side_effect = None
    control.get_oauth2_credential_provider.side_effect = None
    ada_id, ada_client = ada
    bob_id, _bob_client = bob
    name = f"prop-{next(_SEQ)}"
    kind = scenario["kind"]
    _isolate(value=tv.visibility.ISOLATED if scenario["isolated"] else tv.visibility.SHARED)
    if scenario["external"]:
        _vault(control, kind, [name])  # vault-only: Launchpad never recorded it
    else:
        _create_connection(admin, control, kind, name)
        _vault(control, kind, [name])
    control.reset_mock()

    # referrers ------------------------------------------------------------------
    pins: list[tuple[str, str, str | None]] = []  # (agent id, agent name, creator)
    providers: list[tuple[str, str]] = []
    tool_refs: list[tuple[str, str, str | None]] = []
    for i, ref in enumerate(scenario["referrers"]):
        label = f"{ref}-{i}"
        if ref == "pin_own":
            pins.append((_agent(label, created_by=ada_id, pin=name), label, ada_id))
        elif ref == "pin_other":
            pins.append((_agent(label, created_by=bob_id, pin=name), label, bob_id))
        elif ref == "pin_admin":
            pins.append((_agent(label, created_by=None, pin=name), label, None))
        elif ref == "pin_deleted":
            _agent(label, created_by=ada_id, pin=name, status="deleted")
        elif ref == "provider":
            providers.append((_provider(f"prop-{label}", name), f"prop-{label}"))
        elif ref == "tool_auth":
            tool_refs.append((_agent(label, created_by=bob_id, tool_auth=name), label, bob_id))

    # the caller -------------------------------------------------------------------
    role = scenario["role"]
    client = admin if role == "admin" else ada_client
    _perm(ada_id, granted=role != "plain")
    try:
        # order: repoint lifts every reference; rename keeps it (the credential NAME is the
        # reference, not the provider's display name) — model-credential.md: "Repointing the
        # agents / providers lifts the guard"
        if scenario["order"] == "repoint":
            db = SessionLocal()
            try:
                for aid, _n, _c in pins + tool_refs:
                    row = db.get(Agent, aid)
                    spec = copy.deepcopy(row.spec)  # a new object: the JSON column sees the change
                    if "model_credential" in spec:
                        spec["model_credential"] = "other-credential"
                    for tool in spec.get("tools") or []:
                        tool["auth"] = {"connection": "other-connection"}
                    row.spec = spec
                for pid, _n in providers:
                    db.get(ModelProvider, pid).credential_provider = "other-credential"
                db.commit()
            finally:
                db.close()
            pins, tool_refs, providers = [], [], []
        elif scenario["order"] == "rename_provider" and providers:
            db = SessionLocal()
            try:
                renamed = []
                for pid, old in providers:
                    row = db.get(ModelProvider, pid)
                    row.name = row.label = f"{old}-renamed"
                    renamed.append((pid, row.name))
                db.commit()
            finally:
                db.close()
            providers = renamed

        res = client.delete(f"/api/identity/connections/{kind}/{name}")
        body = res.json()

        # what the caller may see (users-and-ownership.md §7): everything for an admin or
        # in a shared workspace; in an isolated one a member sees own / NULL-creator /
        # admin-created rows only
        def visible(creator: str | None) -> bool:
            return role == "admin" or not scenario["isolated"] or creator in (None, ada_id)

        if role == "plain":
            # core/route_policy.py: DELETE is `perm:identity.manage`; a member without it is
            # refused before any guard — nothing is deleted, no vault call
            assert res.status_code == 403, res.text
        elif tool_refs:
            # identity_providers.delete_connection: the tool-auth / gateway-target guard first
            assert (res.status_code, body["code"]) == (409, "identity.connection_referenced"), body
            expected = [
                {"type": "agent", "id": aid, "name": n} for aid, n, c in tool_refs if visible(c)
            ]
            assert body["detail"]["referenced_by"] == expected, body["detail"]
        elif pins or providers:
            # model-credential.md "Deletion guard": 409 identity.in_use before the external
            # rule; referenced_by {type, id, name}, total and truncated FOLLOW THE MASKED LIST
            assert (res.status_code, body["code"]) == (409, "identity.in_use"), body
            expected = [{"type": "agent", "id": aid, "name": n} for aid, n, c in pins if visible(c)]
            expected += [
                {"type": "model_provider", "id": pid, "name": n}
                for pid, n in sorted(providers, key=lambda p: p[1])
            ]
            detail = body["detail"]
            assert detail["referenced_by"] == expected, detail
            assert detail["total"] == len(expected) and detail["truncated"] is False, detail
            # a member never learns that hidden agents pin it: the delete is refused even when
            # the masked list is empty (api.md: "referenced_by: [], total: 0")
        elif scenario["external"]:
            assert (res.status_code, body["code"]) == (409, "identity.external_connection"), body
        else:
            assert res.status_code == 200 and body == {"deleted": True}, body
            deleter = (
                control.delete_api_key_credential_provider
                if kind == "api_key"
                else control.delete_oauth2_credential_provider
            )
            deleter.assert_called_once_with(name=name)
            _vault(control, kind, [])  # the stub vault forgets it too — the real one did
            gone = ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "x"}}, "Get"
            )
            control.get_api_key_credential_provider.side_effect = gone
            control.get_oauth2_credential_provider.side_effect = gone
            assert client.get(f"/api/identity/connections/{kind}/{name}").status_code == 404
            db = SessionLocal()
            try:
                assert db.query(IdentityProvider).filter_by(name=name).count() == 0
            finally:
                db.close()

        if res.status_code != 200:
            control.delete_api_key_credential_provider.assert_not_called()
            control.delete_oauth2_credential_provider.assert_not_called()
            # the connection (when Launchpad recorded it) is still listed
            if not scenario["external"] and role != "plain":
                listed = client.get("/api/identity/connections").json()["connections"]
                assert name in {c["name"] for c in listed}
        assert API_KEY not in res.text and SECRET not in res.text
        assert_ledger_invariants(SessionLocal)
    finally:
        _perm(ada_id, granted=True)
        _isolate(value=tv.visibility.SHARED)


def test_in_use_list_is_bounded_and_the_total_is_real_for_an_administrator(admin, control):
    """api.md: `referenced_by[≤20]`, `total`, `truncated`."""
    _reset()
    name = f"prop-{next(_SEQ)}"
    _create_connection(admin, control, "api_key", name)
    _vault(control, "api_key", [name])
    for i in range(23):
        _agent(f"pin-{i:02d}", created_by=None, pin=name)
    res = admin.delete(f"/api/identity/connections/api_key/{name}")
    assert res.status_code == 409 and res.json()["code"] == "identity.in_use"
    detail = res.json()["detail"]
    assert (
        len(detail["referenced_by"]) == 20 and detail["total"] == 23 and detail["truncated"] is True
    )
    assert_ledger_invariants(SessionLocal)


def test_a_member_in_an_isolated_workspace_cannot_count_hidden_pins_through_total(
    admin, ada, bob, control
):
    """api.md: "isolation-hidden agents are dropped from the list and total / truncated follow
    the masked list, so a member never learns how many hidden agents pin it"."""
    _reset()
    ada_id, ada_client = ada
    bob_id, _ = bob
    name = f"prop-{next(_SEQ)}"
    _create_connection(admin, control, "api_key", name)
    _vault(control, "api_key", [name])
    _isolate()
    try:
        for i in range(5):
            _agent(f"bobs-{i}", created_by=bob_id, pin=name)
        mine = _agent("adas", created_by=ada_id, pin=name)
        res = ada_client.delete(f"/api/identity/connections/api_key/{name}")
        assert res.status_code == 409 and res.json()["code"] == "identity.in_use"
        detail = res.json()["detail"]
        assert detail == {
            "referenced_by": [{"type": "agent", "id": mine, "name": "adas"}],
            "total": 1,
            "truncated": False,
        }
        # nothing visible left, still refused, still no count of the hidden pins
        db = SessionLocal()
        try:
            db.get(Agent, mine).status = "deleted"
            db.commit()
        finally:
            db.close()
        res = ada_client.delete(f"/api/identity/connections/api_key/{name}")
        assert res.status_code == 409 and res.json()["code"] == "identity.in_use"
        assert res.json()["detail"] == {"referenced_by": [], "total": 0, "truncated": False}
    finally:
        _isolate(value=tv.visibility.SHARED)


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs38: with more than 20 pins the bounded list is "
    "masked but `total` only subtracts hidden ids INSIDE the first 20 — a member "
    "reads total > len(referenced_by) and infers hidden pins (rev-r11 residual, "
    "now pinned against api.md's 'never learns how many hidden agents pin it')",
)
def test_total_never_exceeds_what_an_isolated_member_may_see(admin, ada, bob, control):
    _reset()
    ada_id, ada_client = ada
    bob_id, _ = bob
    name = f"prop-{next(_SEQ)}"
    _create_connection(admin, control, "api_key", name)
    _vault(control, "api_key", [name])
    _isolate()
    try:
        for i in range(20):
            _agent(f"adas-{i:02d}", created_by=ada_id, pin=name)
        for i in range(4):
            _agent(f"bobs-{i}", created_by=bob_id, pin=name)  # sorted after ada's by created_at
        res = ada_client.delete(f"/api/identity/connections/api_key/{name}")
        detail = res.json()["detail"]
        assert len(detail["referenced_by"]) == 20
        assert detail["total"] == len(detail["referenced_by"]), detail
        assert detail["truncated"] is False
    finally:
        _isolate(value=tv.visibility.SHARED)
