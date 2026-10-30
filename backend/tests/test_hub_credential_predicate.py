"""N17-obs280 (design simplification s1, C3): the registry ROW is the hub's single source for
"is a key registered / may the hub read it".

Before, the hub answered that question three ways: `model_registry._hub_configured` (the row's
`key_version` or the host env), `model_registry.holds_registered_key` (the row's three key facts)
and `aws_clients.provider_api_key`, which gated its SSM read on the hub workspace's
`resources.model_credentials[<name>]` record (`source: ssm`, `configured`) AND the row
(obs254). obs222 / obs226 / obs229 / obs231 / obs254 were each "two of them disagreed". Now one
predicate, `ProviderRow.hub_holds_key(host_env=...)` (`model_registry.hub_holds_key` for a ledger
row), answers for the Settings card, the guards, the host-change clearing, the hub-side model
builder (`schemas.agent.provider_configured(resources=None)`) and the SSM read. The per-workspace
`model_credentials` records stay what a WORKSPACE's availability is read from (its own Identity
vault) — never a hub decision. Each case below pins the predicate, the SSM read it licenses and
what `/api/capabilities` says, on the real routes with fakes (C8: no AWS).
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.models.ledger import Workspace
from app.models.model_registry import ModelProvider
from app.schemas import agent as agent_schema
from app.schemas import model_catalog
from app.services import aws_clients, model_registry
from app.services.workspace import merge_workspace_resources, workspace_context
from tests import test_model_provider_keys as keys

app, admin, ada, world, _hermetic = keys.app, keys.admin, keys.ada, keys.world, keys._hermetic
PREFIX, URL, TYPED = keys.PREFIX, keys.URL, keys.TYPED
pytestmark = pytest.mark.siliconflow


def _row(name: str) -> ModelProvider:
    db = SessionLocal()
    try:
        row = db.scalar(sa.select(ModelProvider).where(ModelProvider.name == name))
        db.expunge(row)
        return row
    finally:
        db.close()


def _predicate(name: str) -> tuple[bool, bool]:
    """(hub_holds_key(), hub_holds_key(host_env=False)) on the ledger row AND the published
    snapshot row — the two entry points must agree."""
    ledger = _row(name)
    snap = model_catalog.current().providers[name]
    both = (model_registry.hub_holds_key(ledger), model_registry.hub_holds_key(ledger,
                                                                                host_env=False))
    assert both == (snap.hub_holds_key(), snap.hub_holds_key(host_env=False))
    return both


def _plant_hub_record(provider: str, **entry) -> None:
    """Write a `model_credentials[<name>]` record on the hub workspace row by hand — the
    record is NOT a hub input any more, so a lying record must change nothing on the hub."""
    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        existing = dict((row.resources or {}).get("model_credentials") or {})
        merge_workspace_resources(workspace_context(row),
                                  {"model_credentials": {**existing, provider: dict(entry)}})
    finally:
        db.close()


def _chat_model(client, pid: str) -> str:
    made = client.post(f"{PREFIX}/{pid}/models", json={
        "api_model_name": "m1", "kind": "chat", "probe": False, "overrides": {"tools": True}})
    assert made.status_code == 201, made.text
    return made.json()["model_id"]


# ── host_env: the built-in Bedrock row (deploy-ops-35: key_version NULL, source host_env) ──
def test_a_host_env_bedrock_row_is_configured_without_key_facts_and_never_reads_ssm(
    admin, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    bedrock = _row("bedrock")
    assert bedrock.key_version is None and bedrock.credential_source == "host_env"
    # configured from the host env, but the ROW holds no registered key
    assert _predicate("bedrock") == (True, False)
    assert agent_schema.provider_configured("bedrock") is True
    card = next(p for p in admin.get(PREFIX).json()["providers"] if p["name"] == "bedrock")
    assert card["configured"] is True and card["key_last4"] is None
    monkeypatch.setenv(aws_clients.GLOBAL_BEDROCK_BEARER_ENV, "bedrock-bearer-value")
    assert aws_clients.provider_api_key("bedrock") == "bedrock-bearer-value"
    assert world["ssm"].calls == []


def test_a_workspace_row_keyed_only_by_the_host_env_reads_the_variable_not_ssm(
    admin, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    made = admin.post(PREFIX, json={"name": "envy", "kind": "openai_compatible",
                                    "label": "Envy", "base_url": URL}).json()
    assert made["configured"] is False and _predicate("envy") == (False, False)
    assert agent_schema.provider_configured("envy") is False
    monkeypatch.setenv("LAUNCHPAD_ENVY_API_KEY", " host-key ")
    assert _predicate("envy") == (True, False)
    assert agent_schema.provider_configured("envy") is True
    assert aws_clients.provider_api_key("envy") == "host-key"
    assert world["ssm"].calls == []  # no key facts → no GetParameter, env is the source


# ── a console-written key ────────────────────────────────────────────────────────────────
def test_a_console_typed_key_makes_the_row_the_source_for_card_ssm_read_and_hub_judge(
    admin, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    made = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                    "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    assert made["configured"] is True and _predicate("ppio") == (True, True)
    aws_clients.invalidate_provider_api_key()
    reads = len(world["ssm"].calls)
    assert aws_clients.provider_api_key("ppio") == TYPED
    assert world["ssm"].calls[reads:] == ["launchpad-ppio-api-key"]
    # the hub-side model builder asks the same predicate: before obs280 its env-only branch
    # refused this provider (422 model.provider_unconfigured) although the hub held the key
    assert agent_schema.provider_configured("ppio") is True
    model_id = _chat_model(admin, made["id"])
    built = aws_clients.build_chat_model(model_id, region="cn-northwest-1")
    assert built is not None
    assert admin.get("/api/capabilities").json()["model_providers"]["ppio"]["configured"] is True


# ── an operator-created parameter, registered through POST /credential {} ─────────────────
def test_an_operator_created_parameter_registered_from_ssm_sets_the_row_facts(
    admin, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    world["ssm"].params["launchpad-acme-api-key"] = ("operator-key-0000", 3)
    world["endpoint"].accepted.add("operator-key-0000")
    made = admin.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                    "label": "Acme", "base_url": URL}).json()
    assert _predicate("acme") == (False, False)
    with pytest.raises(RuntimeError, match="holds no registered key"):
        aws_clients.provider_api_key("acme")
    assert world["ssm"].calls == []  # unregistered: offline, no GetParameter
    reg = admin.post(f"{PREFIX}/{made['id']}/credential", json={})
    assert reg.status_code == 200 and reg.json()["key_version"] == 3, reg.text
    row = _row("acme")
    assert row.key_version == 3 and row.key_entered_at is None  # operator-created
    assert _predicate("acme") == (True, True)
    aws_clients.invalidate_provider_api_key()
    assert aws_clients.provider_api_key("acme") == "operator-key-0000"


# ── a cleared key (host change without a re-entered key) and the base_url rule ────────────
def test_a_host_change_clears_the_row_and_the_row_alone_refuses_the_read(
    admin, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    made = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                    "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    assert aws_clients.provider_api_key("ppio") == TYPED
    moved = admin.put(f"{PREFIX}/{made['id']}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200 and moved.json()["configured"] is False, moved.text
    assert _predicate("ppio") == (False, False)
    # a record that still says configured (a clearing step that never ran) changes nothing:
    # the hub reads only the row
    _plant_hub_record("ppio", configured=True, source="ssm", name="launchpad-ppio")
    reads = list(world["ssm"].calls)
    with pytest.raises(RuntimeError, match="holds no registered key") as refused:
        aws_clients.provider_api_key("ppio")
    assert TYPED not in str(refused.value) and world["ssm"].calls == reads
    assert agent_schema.provider_configured("ppio") is False
    # re-entering the key re-arms the row for the NEW host; the cache key carries the host
    world["endpoint"].accepted.add(TYPED)
    again = admin.post(f"{PREFIX}/{made['id']}/credential", json={"api_key": TYPED})
    assert again.status_code == 200, again.text
    assert _predicate("ppio") == (True, True)
    assert aws_clients.provider_api_key("ppio") == TYPED
    assert aws_clients.cached_provider_api_key_rows("ppio") == [
        (made["id"], "launchpad-ppio-api-key")]
    assert [k[3] for k in aws_clients._PROVIDER_API_KEYS if k[0] == "ppio"] == [
        "https://api.other-host.com/v1"]


# ── a deleted provider and a same-named successor (obs226 / obs229) ───────────────────────
def test_a_same_named_successor_is_unregistered_whatever_the_hub_record_says(
    admin, ada, world, monkeypatch,
):
    keys._real_hub_resolver(monkeypatch, world)
    made = admin.post(PREFIX, json={"name": "foo", "kind": "openai_compatible", "label": "Foo",
                                    "base_url": URL, "api_key": TYPED}).json()
    assert aws_clients.provider_api_key("foo") == TYPED
    assert admin.delete(f"{PREFIX}/{made['id']}").status_code == 204
    _, member = ada
    mine = member.post(PREFIX, json={"name": "foo", "kind": "openai_compatible", "label": "Foo",
                                     "base_url": "https://evil.example.com/v1"})
    assert mine.status_code == 201 and mine.json()["configured"] is False, mine.text
    assert _predicate("foo") == (False, False)
    # the pre-obs229 end state, planted by hand: the predecessor's record says configured /
    # ssm on the hub row. The hub must still read nothing for the successor.
    _plant_hub_record("foo", configured=True, source="ssm", name="launchpad-foo")
    reads = list(world["ssm"].calls)
    with pytest.raises(RuntimeError, match="holds no registered key"):
        aws_clients.provider_api_key("foo")
    assert world["ssm"].calls == reads and aws_clients.cached_provider_api_key_rows("foo") == []
    probe = member.post(f"{PREFIX}/{mine.json()['id']}/probe").json()
    assert probe["state"] == "failed" and "credential_unavailable" in (probe["error"] or "")
    assert TYPED not in str(probe)


# ── rev-r20 nit on obs280: the row gates the cache as well as the SSM read ───────────────
def test_a_stale_cache_entry_is_never_served_for_a_row_without_key_facts(
    admin, world, monkeypatch,
):
    """A cache entry under the row's CURRENT key (same name, row id, parameter and host) —
    filled before the key facts were cleared, with the invalidation defeated — must not be
    served once the row holds no key: the row gate runs before the cache lookup."""
    keys._real_hub_resolver(monkeypatch, world)
    made = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                    "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    assert aws_clients.provider_api_key("ppio") == TYPED  # cached now
    # clear the key facts in the ledger without touching the cache (a missed invalidation)
    monkeypatch.setattr(model_registry, "_forget_cached_key", lambda name: None)
    db = SessionLocal()
    try:
        row = db.get(ModelProvider, made["id"])
        row.key_version = row.key_last4 = row.key_entered_at = row.key_modified_at = None
        db.commit()
        model_registry.refresh(db)
    finally:
        db.close()
    assert _predicate("ppio") == (False, False)
    stale = ("ppio", made["id"], "launchpad-ppio-api-key", URL)
    aws_clients._PROVIDER_API_KEYS[stale] = (TYPED, 1e18)  # live entry for the CURRENT key
    reads = list(world["ssm"].calls)
    with pytest.raises(RuntimeError, match="holds no registered key") as refused:
        aws_clients.provider_api_key("ppio")
    assert TYPED not in str(refused.value)
    assert world["ssm"].calls == reads  # no SSM read either
    aws_clients.invalidate_provider_api_key()
