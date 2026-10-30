"""A model-provider API key typed in the console (N17-obs203 §3, user decision 2026-10-21:
"API key 直接在界面里输入").

The key exists in the request body and in memory until the console host has (1) probed the
endpoint with it — a failure is 422 `model_provider.probe_failed` and NOTHING is stored —,
(2) written it to the row's SSM SecureString itself (`PutParameter Overwrite=True` on the hub's
`ssm` client, owner-scoped name for a personal provider) and (3) run the existing
SSM → Identity → hub-record registration. The ledger keeps the parameter NAME and the key's last
four characters only; the response carries `key_last4`; no log record, journal row,
ledger column or response body ever carries the value (the no-leak scan below plants a
recognisable key and greps everything). "Replace key" is the same path through `PUT /{id}` or
`POST /{id}/credential {api_key}`; deleting the provider deletes a console-written parameter
best-effort and journals the residue. Hermetic: httpx MockTransport, fake SSM / Identity, a fake
resolver — no AWS, no DNS (C8).
"""

from __future__ import annotations

import json
import logging

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.models.ledger import Workspace
from app.models.model_registry import ModelCatalogChange, ModelProvider
from app.schemas import model_catalog
from app.services import aws_clients, model_registry
from tests import test_visibility as tv
from tests.test_model_probe import (
    CHAT,
    KEY,
    PUBLIC_IP,
    FakeControl,
    FakeEndpoint,
    FakeSsm,
    _hooks,
)

app, admin, ada = tv.app, tv.admin, tv.ada

PREFIX = "/api/settings/model-providers"
URL = "https://api.ppinfra.com/v3/openai"
TYPED = "sk-typed-LEAKED-9f8e7d6c5b4a3210wxyz"  # what the user types; must never surface
pytestmark = pytest.mark.siliconflow


class WritableSsm(FakeSsm):
    """`FakeSsm` + the two writes the console host now issues."""

    def __init__(self, params=None, *, error=None, write_error=None, delete_error=None,
                 tag_error=None, tagged_create_error=None) -> None:
        super().__init__(params, error=error)
        self.write_error = write_error
        self.delete_error = delete_error
        self.tag_error = tag_error  # AddTagsToResource refused with this code (N17-obs232)
        self.tagged_create_error = tagged_create_error  # a create WITH Tags refused with this code
        self.puts: list[dict] = []
        self.tags: list[dict] = []
        self.deletes: list[str] = []
        self.parameter_tags: dict[str, list[dict]] = {}  # what the parameter carries
        self.version = 0

    def put_parameter(self, **kwargs):
        self.puts.append(dict(kwargs))
        if self.write_error:
            raise ClientError({"Error": {"Code": self.write_error, "Message": "denied"}},
                              "PutParameter")
        assert kwargs["Type"] == "SecureString"
        name = kwargs["Name"]
        if kwargs.get("Overwrite"):
            # the real API refuses Tags together with Overwrite=True
            assert "Tags" not in kwargs, "Tags cannot be combined with Overwrite=True"
        else:
            if name in self.params:
                raise ClientError({"Error": {"Code": "ParameterAlreadyExists",
                                             "Message": "exists"}}, "PutParameter")
            if "Tags" in kwargs and self.tagged_create_error:
                raise ClientError({"Error": {"Code": self.tagged_create_error,
                                             "Message": "denied"}}, "PutParameter")
            if "Tags" in kwargs:
                self.parameter_tags[name] = list(kwargs["Tags"])
        self.version += 1
        self.params[name] = (kwargs["Value"], self.version)
        return {"Version": self.version}

    def add_tags_to_resource(self, **kwargs):
        self.tags.append(dict(kwargs))
        if self.tag_error:
            raise ClientError({"Error": {"Code": self.tag_error, "Message": "no identity-based "
                                         "policy allows"}}, "AddTagsToResource")
        self.parameter_tags.setdefault(kwargs["ResourceId"], []).extend(kwargs["Tags"])
        return {}

    def delete_parameter(self, Name):  # noqa: N803
        self.deletes.append(Name)
        if self.delete_error:
            raise ClientError({"Error": {"Code": self.delete_error, "Message": "denied"}},
                              "DeleteParameter")
        self.params.pop(Name, None)
        return {}


class TypedEndpoint(FakeEndpoint):
    """Accepts the keys in ``accepted`` (the TYPED one by default — the hub has no key of its
    own for this provider); `FakeEndpoint` itself checks for `tests.test_model_probe.KEY`."""

    def __init__(self, **flags) -> None:
        super().__init__(**flags)
        self.accepted: set[str] = {TYPED}
        self.sent_keys: list[str] = []  # the bearer of every request, as sent

    def __call__(self, request):
        sent = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.sent_keys.append(sent)
        if sent in self.accepted:
            request.headers["authorization"] = f"Bearer {KEY}"  # let the base check pass
        return super().__call__(request)


@pytest.fixture(autouse=True)
def _hermetic():
    aws_clients.invalidate_provider_api_key()
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()
    aws_clients.invalidate_provider_api_key()


@pytest.fixture
def world(caplog):
    """Real hooks on fakes: a typed-key endpoint, a writable SSM, an Identity control plane."""
    caplog.set_level(logging.DEBUG)
    endpoint = TypedEndpoint()
    ssm = WritableSsm()
    control = FakeControl()
    hooks = _hooks(endpoint, ssm_factory=lambda ctx: ssm, control_factory=lambda ctx: control,
                   resolver=lambda host: [PUBLIC_IP])
    model_registry.set_probe_hooks(hooks)
    return {"endpoint": endpoint, "ssm": ssm, "control": control, "hooks": hooks,
            "caplog": caplog}


def _everything(db, caplog, *responses) -> str:
    """Every place the key could land: responses, log, journal, ledger rows, resource maps."""
    parts = [json.dumps(r) for r in responses]
    parts.append(caplog.text)
    parts.append(json.dumps([{"op": c.operation, "b": c.before, "a": c.after, "n": c.note}
                             for c in db.scalars(sa.select(ModelCatalogChange))]))
    parts.append(json.dumps([{c.name: getattr(p, c.name) for c in ModelProvider.__table__.columns
                              if c.name not in ("created_at", "updated_at", "key_modified_at",
                                                "key_entered_at")}
                             for p in db.scalars(sa.select(ModelProvider))]))
    parts.append(json.dumps([w.resources for w in db.scalars(sa.select(Workspace))]))
    return "\n".join(parts)


# ── create with a key ─────────────────────────────────────────────────────────────────
def test_admin_creates_a_provider_with_a_typed_key_probe_then_ssm_then_identity(admin, world):
    res = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                   "base_url": URL, "api_key": TYPED})
    assert res.status_code == 201, res.text
    body = res.json()
    # (1) the probe ran FIRST with the typed key, (2) then SSM was written, (3) then Identity
    assert [r.url.path for r in world["endpoint"].requests] == ["/v3/openai/models"]
    assert world["endpoint"].sent_keys == [TYPED]  # the typed key, not a hub key
    [put] = world["ssm"].puts
    # N17-obs232: a NEW parameter is created with its tags in the one PutParameter call
    # (Overwrite=False + Tags) — no separate AddTagsToResource to be refused
    assert put == {"Name": "launchpad-ppio-api-key", "Value": TYPED, "Type": "SecureString",
                   "Overwrite": False,
                   "Tags": [{"Key": "launchpad:managed", "Value": "true"},
                            {"Key": "launchpad:provider", "Value": "ppio"}]}
    assert world["ssm"].tags == []
    assert world["ssm"].parameter_tags["launchpad-ppio-api-key"] == put["Tags"]
    assert world["ssm"].calls == ["launchpad-ppio-api-key"]  # read back once for Identity
    assert world["control"].calls == [("get", "launchpad-ppio"), ("create", "launchpad-ppio")]
    # the response: the reference, the last four characters, configured — never the key
    assert body["key_last4"] == TYPED[-4:] and body["configured"] is True
    assert body["key_version"] == 1 and "credential" not in body  # N17-obs283: flat only
    assert body["credential_ssm_parameter"] == "launchpad-ppio-api-key"
    db = SessionLocal()
    try:
        row = db.scalar(sa.select(ModelProvider).where(ModelProvider.name == "ppio"))
        assert row.key_last4 == TYPED[-4:] and row.key_entered_at is not None
        ops = [c.operation for c in db.scalars(sa.select(ModelCatalogChange).where(
            ModelCatalogChange.target_id == row.id).order_by(ModelCatalogChange.id))]
        assert ops == ["provider.create", "provider.credential", "provider.key"]
        key_row = db.scalar(sa.select(ModelCatalogChange).where(
            ModelCatalogChange.operation == "provider.key"))
        assert key_row.after == {"ssm_parameter": "launchpad-ppio-api-key",
                                 "last4": TYPED[-4:], "key_version": 1}
        # the audit rows never move the catalog version
        assert model_registry.stamp(db) == key_row.id - 2
        everything = _everything(db, world["caplog"], body, admin.get(PREFIX).json(),
                                 admin.get(f"{PREFIX}/changes").json())
    finally:
        db.close()
    assert TYPED not in everything
    assert TYPED[:-4] not in everything  # not even a prefix of it
    assert f"…{TYPED[-4:]}" in world["caplog"].text  # the log names the parameter + last4 only
    # the hub record says `source: ssm` — what `aws_clients.provider_api_key` reads SSM on
    db = SessionLocal()
    try:
        record = db.get(Workspace, "default").resources["model_credentials"]["ppio"]
    finally:
        db.close()
    assert record["configured"] is True and record["source"] == "ssm"
    assert record["key_version"] == 1 and record["name"] == "launchpad-ppio"


def test_a_failed_probe_stores_nothing(admin, world):
    world["endpoint"].auth_ok = False  # the endpoint says 401 to this key
    res = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                   "base_url": URL, "api_key": TYPED})
    assert res.status_code == 422, res.text
    assert res.json()["code"] == "model_provider.probe_failed"
    assert "auth_failed" in res.json()["detail"]["reason"]
    assert world["ssm"].puts == [] and world["control"].calls == []
    assert admin.get(f"{PREFIX}/ppio").status_code == 404  # no row either
    assert TYPED not in res.text and TYPED not in world["caplog"].text
    # a transport failure (blocked host) is the same 422 with the sanitised reason, no row
    blocked = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": "https://models.corp.local/v1",
                                       "api_key": TYPED})
    assert blocked.status_code == 422 and blocked.json()["code"] == "model_provider.probe_failed"
    assert "SSRF guard" in blocked.json()["detail"]["reason"]
    assert world["ssm"].puts == [] and admin.get(f"{PREFIX}/ppio").status_code == 404
    # a `/models` endpoint that answers 404 is NOT a failure (some gateways have no list)
    world["endpoint"].auth_ok = True
    world["endpoint"].models_endpoint = False
    ok = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                  "base_url": URL, "api_key": TYPED})
    assert ok.status_code == 201 and ok.json()["key_last4"] == TYPED[-4:]


def test_a_bedrock_provider_takes_no_key_and_a_bad_key_value_is_never_echoed(admin, world):
    res = admin.post(PREFIX, json={"name": "xbed", "kind": "bedrock", "label": "X",
                                   "api_key": TYPED})
    assert res.status_code == 422 and res.json()["code"] == "model_provider.api_key_unsupported"
    assert admin.get(f"{PREFIX}/xbed").status_code == 404 and world["ssm"].puts == []
    # validation errors under `api_key` never echo the input (core/errors.is_sensitive_field)
    empty = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "P",
                                     "base_url": URL, "api_key": ""})
    assert empty.status_code == 422 and empty.json()["code"] == "validation.invalid_request"
    assert "input" not in json.dumps(empty.json()["detail"])
    too_long = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                        "label": "P", "base_url": URL, "api_key": "k" * 70000})
    assert too_long.status_code == 422 and "kkkk" not in too_long.text
    assert TYPED not in world["caplog"].text


def test_an_ssm_write_failure_is_a_502_naming_the_iam_statement(admin, world):
    world["ssm"].write_error = "AccessDeniedException"
    res = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                   "base_url": URL, "api_key": TYPED})
    assert res.status_code == 502, res.text
    assert res.json()["code"] == "model_provider.ssm_write_failed"
    assert res.json()["detail"]["statement"] == "SsmWriteLaunchpadSecretParameters"
    assert "ssm:PutParameter" in res.json()["message"]
    assert world["control"].calls == []  # Identity never saw anything
    # the row exists without a key (the probe passed) — the card shows "not configured"
    row = admin.get(f"{PREFIX}/ppio").json()
    assert row["configured"] is False and row["key_last4"] is None
    assert TYPED not in res.text and TYPED not in world["caplog"].text


# ── replace key ──────────────────────────────────────────────────────────────────────
def test_replace_key_through_put_and_through_credential_rotates_in_place(admin, world):
    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    second = TYPED[:-4] + "NEW1"
    # the fake endpoint accepts TYPED only — a replacement that the endpoint refuses is 422
    refused = admin.put(f"{PREFIX}/{created['id']}", json={"api_key": second})
    assert refused.status_code == 422 and refused.json()["code"] == "model_provider.probe_failed"
    assert len(world["ssm"].puts) == 1  # nothing written
    # make the endpoint accept the new key and replace through PUT (with an edit in the same body)
    world["endpoint"].accepted.add(second)
    replaced = admin.put(f"{PREFIX}/{created['id']}", json={"api_key": second, "label": "PPIO 2"})
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["label"] == "PPIO 2"
    assert replaced.json()["key_last4"] == "NEW1" and replaced.json()["key_version"] == 2
    assert world["ssm"].puts[-1]["Value"] == second
    assert world["control"].calls[-1] == ("update", "launchpad-ppio")  # rotated in place
    # … and through POST /credential {api_key} (the card's "Replace key" button)
    third = TYPED[:-4] + "NEW2"
    world["endpoint"].accepted.add(third)
    cred = admin.post(f"{PREFIX}/{created['id']}/credential", json={"api_key": third})
    assert cred.status_code == 200, cred.text
    assert cred.json()["last4"] == "NEW2" and cred.json()["rotated"] is True
    assert cred.json()["key_version"] == 3
    # an empty body keeps the D21 behaviour: read the parameter, nothing to rotate
    same = admin.post(f"{PREFIX}/{created['id']}/credential", json={})
    assert same.status_code == 200 and same.json()["rotated"] is False
    assert same.json()["last4"] is None  # no key typed on this call
    db = SessionLocal()
    try:
        everything = _everything(db, world["caplog"], replaced.json(), cred.json(), same.json())
    finally:
        db.close()
    for secret in (TYPED, second, third):
        assert secret not in everything, secret[-4:]
        assert secret[:-4] not in everything


# ── delete ───────────────────────────────────────────────────────────────────────────
def test_deleting_the_provider_deletes_a_console_written_parameter_best_effort(admin, world):
    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    other = admin.post(PREFIX, json={"name": "manual", "kind": "openai_compatible",
                                     "label": "Manual", "base_url": URL}).json()
    assert admin.delete(f"{PREFIX}/{created['id']}").status_code == 204
    assert world["ssm"].deletes == ["launchpad-ppio-api-key"]
    assert "launchpad-ppio-api-key" not in world["ssm"].params
    # a provider whose key the operator created (nothing typed) keeps its parameter
    assert admin.delete(f"{PREFIX}/{other['id']}").status_code == 204
    assert world["ssm"].deletes == ["launchpad-ppio-api-key"]
    db = SessionLocal()
    try:
        notes = [c.note for c in db.scalars(sa.select(ModelCatalogChange).where(
            ModelCatalogChange.operation == "provider.delete").order_by(ModelCatalogChange.id))]
    finally:
        db.close()
    assert notes[-2].endswith("SSM parameter launchpad-ppio-api-key deleted")
    assert "SSM parameter" not in notes[-1]


def test_a_refused_delete_leaves_the_parameter_and_a_failed_delete_is_reported(admin, world):
    from app.models.ledger import Agent

    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    model = admin.post(f"{PREFIX}/{created['id']}/models",
                       json={"api_model_name": CHAT, "probe": False}).json()
    db = SessionLocal()
    try:
        db.add(Agent(name="uses-ppio", method="zip_runtime", status="active",
                     workspace_id="default",
                     spec={"name": "uses-ppio", "method": "zip_runtime",
                           "model_id": model["model_id"]}))
        db.commit()
    finally:
        db.close()
    refused = admin.delete(f"{PREFIX}/{created['id']}")
    assert refused.status_code == 409 and refused.json()["code"] == "model_provider.in_use"
    assert world["ssm"].deletes == []  # the refusal ran before any SSM call
    db = SessionLocal()
    try:
        db.query(Agent).delete()
        db.commit()
    finally:
        db.close()
    world["ssm"].delete_error = "AccessDeniedException"
    assert admin.delete(f"{PREFIX}/{created['id']}").status_code == 204  # best-effort
    assert world["ssm"].deletes == ["launchpad-ppio-api-key"]
    db = SessionLocal()
    try:
        note = db.scalar(sa.select(ModelCatalogChange).where(
            ModelCatalogChange.operation == "provider.delete").order_by(
            ModelCatalogChange.id.desc())).note
    finally:
        db.close()
    assert "left behind (AccessDeniedException)" in note
    assert "left behind" in world["caplog"].text and TYPED not in world["caplog"].text


# ── a member's personal provider ─────────────────────────────────────────────────────
def test_a_member_key_lands_in_the_owner_scoped_parameter(ada, world):
    ada_id, client = ada
    res = client.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL, "api_key": TYPED})
    assert res.status_code == 201, res.text
    name = f"launchpad-user-{ada_id}-ppio-api-key"
    assert res.json()["credential_ssm_parameter"] == name
    assert [p["Name"] for p in world["ssm"].puts] == [name]
    assert world["control"].calls == [("get", f"launchpad-user-{ada_id}-ppio"),
                                      ("create", f"launchpad-user-{ada_id}-ppio")]
    assert res.json()["key_last4"] == TYPED[-4:] and res.json()["configured"] is True
    assert TYPED not in res.text and TYPED not in world["caplog"].text
    db = SessionLocal()
    try:
        record = db.get(Workspace, "default").resources["model_credentials"]["ppio"]
    finally:
        db.close()
    assert record["source"] == "ssm" and record["name"] == f"launchpad-user-{ada_id}-ppio"


def test_without_the_probe_module_a_typed_key_is_501_and_nothing_is_written(admin):
    model_registry.set_probe_hooks(None)
    res = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                   "base_url": URL, "api_key": TYPED})
    assert res.status_code == 501
    assert res.json()["code"] == "model_providers.credential_unavailable"
    assert admin.get(f"{PREFIX}/ppio").status_code == 404
    assert TYPED not in res.text


# ── N17-obs225 (rev-r20 B1): the shared-member-row exfiltration chain ────────────────────
OPERATOR_KEY = "sk-operator-LEAKED-siliconflow-0a1b2c3d4e5f"  # the operator's parameter value


def test_a_shared_member_row_cannot_be_repointed_at_the_operators_key(admin, ada, world):
    """The reviewer's reproduction, step by step. Pre-fix: member creates personal `ppio` →
    admin `PUT {scope: workspace}` → member `PUT {credential_ssm_parameter:
    launchpad-siliconflow-api-key, base_url: https://evil…}` 200 → member `POST /credential`
    (no body) → the hub READS the operator's parameter and registers it for the member's
    Identity provider → `POST /probe` sends the operator's key to the evil host → `POST
    /credential {api_key}` PutParameter-overwrites the operator's parameter. Now the chain
    stops at its first forbidden step (403 `model_provider.forbidden`), and every later step
    is 403 on its own too: nothing is read from, written to or sent anywhere."""
    ada_id, member = ada
    ssm, endpoint = world["ssm"], world["endpoint"]
    ssm.params["launchpad-siliconflow-api-key"] = (OPERATOR_KEY, 7)  # operator-created
    endpoint.accepted.add(OPERATOR_KEY)
    row = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    assert admin.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"}).status_code == 200
    evil = "https://evil.example.com/v1"
    # step 1 — the repoint: the FIRST forbidden step
    repoint = member.put(f"{PREFIX}/{row['id']}",
                         json={"credential_ssm_parameter": "launchpad-siliconflow-api-key",
                               "base_url": evil})
    assert repoint.status_code == 403, repoint.text
    assert repoint.json()["code"] == "model_provider.forbidden"
    assert repoint.json()["detail"]["provider_id"] == row["id"]
    after = admin.get(f"{PREFIX}/{row['id']}").json()
    assert after["base_url"] == URL
    assert after["credential_ssm_parameter"] == f"launchpad-user-{ada_id}-ppio-api-key"
    # step 2 — register from the parameter: refused before any SSM read
    cred = member.post(f"{PREFIX}/{row['id']}/credential")
    assert cred.status_code == 403 and cred.json()["code"] == "model_provider.forbidden"
    assert ssm.calls == [] and world["control"].calls == []
    # step 3 — probe: refused before any request leaves the host
    probe = member.post(f"{PREFIX}/{row['id']}/probe")
    assert probe.status_code == 403 and probe.json()["code"] == "model_provider.forbidden"
    assert endpoint.requests == [] and endpoint.sent_keys == []
    # step 4 — overwrite the operator's parameter with a typed key: refused, nothing written
    overwrite = member.post(f"{PREFIX}/{row['id']}/credential", json={"api_key": TYPED})
    assert overwrite.status_code == 403
    assert ssm.puts == [] and ssm.params["launchpad-siliconflow-api-key"] == (OPERATOR_KEY, 7)
    # the operator's key never left SSM and the member learned nothing about it
    everything = "\n".join([repoint.text, cred.text, probe.text, overwrite.text,
                            world["caplog"].text])
    assert OPERATOR_KEY not in everything and OPERATOR_KEY[:-4] not in everything
    # the gate is role-based, not creator-based: the administrator may repoint the shared row
    adm = admin.put(f"{PREFIX}/{row['id']}", json={"credential_ssm_parameter":
                                                   "launchpad-siliconflow-api-key"})
    assert adm.status_code == 200, adm.text


def _real_hub_resolver(monkeypatch, world):
    """Swap the `world` hooks' static fake key for the REAL hub resolver
    (`aws_clients.provider_api_key`: hub record → SSM read through the fake → cache) and make
    the hub context read the `default` workspace row (production mirrors that row into
    launchpad.yaml). Returns the hub-context callable."""
    from app.services import workspace as ws

    ssm, endpoint, control = world["ssm"], world["endpoint"], world["control"]

    def hub_ctx() -> ws.WorkspaceContext:
        db = SessionLocal()
        try:
            row = db.get(Workspace, "default")
            return ws.WorkspaceContext(id="default", account_id="111122223333",
                                       region="cn-northwest-1",
                                       resources=dict(row.resources or {}))
        finally:
            db.close()

    monkeypatch.setattr(ws, "default_workspace_context", hub_ctx)
    monkeypatch.setattr(aws_clients, "ssm_client", lambda ctx: ssm)
    hooks = _hooks(endpoint, ssm_factory=lambda ctx: ssm, control_factory=lambda ctx: control,
                   resolver=lambda host: [PUBLIC_IP], key_resolver=aws_clients.provider_api_key,
                   hub_context=hub_ctx)
    model_registry.set_probe_hooks(hooks)
    world["hooks"] = hooks
    return hub_ctx


# ── N17-obs226 / N17-obs229 (rev-r20 B2): a deleted provider's key must not survive its row ─
def test_a_deleted_providers_cached_key_never_reaches_a_same_named_successor(
    admin, ada, world, monkeypatch,
):
    """Pre-fix: `aws_clients._PROVIDER_API_KEYS` was keyed by provider NAME with a 600 s TTL and
    `delete_provider` never invalidated it, so: admin workspace provider `foo` with a cached key
    → admin deletes `foo` → a member creates personal `foo` (base_url evil) → the member's
    `POST /probe` sent the ADMIN's cached key to the evil host. The hub record
    `model_credentials.foo` (`configured: true, source: ssm`) survived the row too (obs229).
    Now the cache is keyed by (name, row id, parameter), dropped on insert / delete / host
    change, and the record is flipped to `configured: false` with the row."""
    ssm, endpoint = world["ssm"], world["endpoint"]
    hub_ctx = _real_hub_resolver(monkeypatch, world)
    created = admin.post(PREFIX, json={"name": "foo", "kind": "openai_compatible", "label": "Foo",
                                       "base_url": URL, "api_key": TYPED}).json()
    assert created["configured"] is True, created
    # a hub-side probe reads the key through the resolver and primes the cache for THIS row
    probe = admin.post(f"{PREFIX}/{created['id']}/probe")
    assert probe.status_code == 200 and probe.json()["state"] == "ok", probe.text
    assert endpoint.sent_keys[-1] == TYPED
    assert aws_clients.cached_provider_api_key_rows("foo") == [
        (created["id"], "launchpad-foo-api-key")]
    # the admin deletes foo: the cache entry is gone, the hub record is unconfigured
    assert admin.delete(f"{PREFIX}/{created['id']}").status_code == 204
    assert aws_clients.cached_provider_api_key_rows("foo") == []
    record = hub_ctx().resources["model_credentials"]["foo"]
    assert record["configured"] is False and record["deleted_provider_id"] == created["id"]
    reads_before = list(ssm.calls)
    # a member re-creates `foo` as a personal provider pointing at their own host …
    ada_id, member = ada
    mine = member.post(PREFIX, json={"name": "foo", "kind": "openai_compatible", "label": "Foo",
                                     "base_url": "https://evil.example.com/v1"})
    assert mine.status_code == 201, mine.text
    assert mine.json()["configured"] is False
    assert mine.json()["credential_ssm_parameter"] == f"launchpad-user-{ada_id}-foo-api-key"
    caps = member.get("/api/capabilities").json()["model_providers"]
    assert caps.get("foo", {}).get("configured", False) is False
    # … and probes it: the hub has NO key for this row — nothing is sent, nothing is read
    requests_before, sent_before = len(endpoint.requests), list(endpoint.sent_keys)
    probe = member.post(f"{PREFIX}/{mine.json()['id']}/probe")
    assert probe.status_code == 200, probe.text
    assert probe.json()["state"] == "failed"
    assert "credential_unavailable" in (probe.json()["error"] or "")
    assert len(endpoint.requests) == requests_before  # no request left the host
    assert endpoint.sent_keys == sent_before  # the admin's create + probe, nothing since
    assert ssm.calls == reads_before  # offline: the unconfigured record licenses no GetParameter
    assert aws_clients.cached_provider_api_key_rows("foo") == []
    # the admin's key never appears anywhere the member can read
    everything = "\n".join([mine.text, probe.text, member.get(PREFIX).text, world["caplog"].text])
    assert TYPED not in everything and TYPED[:-4] not in everything


# ── rev-r20 M2b: a failed probe on POST /{id}/credential {api_key} stores nothing ────────────
def test_a_failed_probe_on_the_credential_route_stores_nothing(admin, world):
    """`POST /{id}/credential {api_key}` (the card's "Enter / Replace key" dialog) probes BEFORE
    it stores — like create and PUT: a refused key is 422 `model_provider.probe_failed`, no
    PutParameter, no Identity call, the row's key facts (version, last4) unchanged, the typed
    value nowhere."""
    ssm, endpoint, control = world["ssm"], world["endpoint"], world["control"]
    # a provider WITHOUT a key yet (nothing typed): the first entry is refused by the endpoint
    bare = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    bad = TYPED[:-4] + "BAD1"  # the fake endpoint accepts TYPED only
    refused = admin.post(f"{PREFIX}/{bare['id']}/credential", json={"api_key": bad})
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "model_provider.probe_failed"
    assert "auth_failed" in refused.json()["detail"]["reason"]
    assert [r.url.path for r in endpoint.requests] == ["/v3/openai/models"]  # the probe only
    assert ssm.puts == [] and ssm.calls == [] and control.calls == []
    row = admin.get(f"{PREFIX}/{bare['id']}").json()
    assert row["configured"] is False and row["key_version"] is None
    assert row["key_last4"] is None
    # a provider WITH a registered key: a refused replacement leaves the current key in place
    endpoint.requests.clear()
    assert admin.post(f"{PREFIX}/{bare['id']}/credential",
                      json={"api_key": TYPED}).status_code == 200
    puts, reads, calls = len(ssm.puts), len(ssm.calls), len(control.calls)
    refused = admin.post(f"{PREFIX}/{bare['id']}/credential", json={"api_key": bad})
    assert refused.status_code == 422 and refused.json()["code"] == "model_provider.probe_failed"
    assert (len(ssm.puts), len(ssm.calls), len(control.calls)) == (puts, reads, calls)
    assert ssm.params["launchpad-ppio-api-key"][0] == TYPED  # the parameter still holds TYPED
    row = admin.get(f"{PREFIX}/{bare['id']}").json()
    assert row["key_version"] == 1 and row["key_last4"] == TYPED[-4:]
    db = SessionLocal()
    try:
        everything = _everything(db, world["caplog"], refused.json(), row)
    finally:
        db.close()
    assert bad not in everything and bad[:-4] not in everything


# ── N17-obs228 (rev-r20 Nit 4): a host change never carries the stored key along ────────────
def _trail_notes(pid: str) -> list[tuple[str, str | None]]:
    db = SessionLocal()
    try:
        return [(c.operation, c.note) for c in db.scalars(sa.select(ModelCatalogChange).where(
            ModelCatalogChange.target_id == pid).order_by(ModelCatalogChange.id))]
    finally:
        db.close()


def test_changing_the_host_of_a_keyed_provider_clears_the_key_unless_it_is_re_entered(
    admin, world, monkeypatch,
):
    """Pre-fix: `PUT {base_url}` on a row with a registered key silently redirected the stored
    key to the new host on the next probe / model call. Now a host change either carries
    `api_key` (probed against the NEW host first) or clears the credential — SSM parameter
    (console-written), Identity provider, hub record, cache — and the row reads 「未配置」 until a
    key is entered again; the journal note names the old and the new host either way."""
    ssm, endpoint, control = world["ssm"], world["endpoint"], world["control"]
    _real_hub_resolver(monkeypatch, world)  # the hub reads keys through SSM, as in production
    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    pid = created["id"]
    assert created["configured"] is True and created["key_last4"] == TYPED[-4:]
    assert admin.post(f"{PREFIX}/{pid}/probe").json()["state"] == "ok"  # the hub has the key
    assert aws_clients.cached_provider_api_key_rows("ppio") != []
    # an unchanged echo of the host (what the edit drawer sends on every save) keeps the key
    same = admin.put(f"{PREFIX}/{pid}", json={"label": "PPIO cloud", "base_url": URL + "/"})
    assert same.status_code == 200 and same.json()["configured"] is True
    assert same.json()["key_last4"] == TYPED[-4:] and ssm.deletes == []
    # a host change WITHOUT a key: the credential is cleared everywhere
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert body["base_url"] == "https://api.other-host.com/v1"
    assert body["configured"] is False
    assert body["key_last4"] is None and body["key_version"] is None
    assert body["key_modified_at"] is None
    assert ssm.deletes == ["launchpad-ppio-api-key"]  # console-written → deleted
    assert "launchpad-ppio-api-key" not in ssm.params
    assert control.calls[-1] == ("delete", "launchpad-ppio")  # Identity provider removed
    assert aws_clients.cached_provider_api_key_rows("ppio") == []
    db = SessionLocal()
    try:
        record = db.get(Workspace, "default").resources["model_credentials"]["ppio"]
        row = db.scalar(sa.select(ModelProvider).where(ModelProvider.id == pid))
        assert (row.key_version, row.key_last4, row.key_entered_at, row.key_modified_at) == (
            None, None, None, None)
    finally:
        db.close()
    assert record["configured"] is False and record["cleared_on_host_change"] is True
    notes = _trail_notes(pid)
    # the row write is journaled first (hosts), the clearing second (residue) — obs230 order
    assert [op for op, _ in notes[-2:]] == ["provider.update", "provider.credential"]
    update_note, cleared_note = notes[-2][1], notes[-1][1]
    assert "api.ppinfra.com" in update_note and "api.other-host.com" in update_note
    assert "stored key was cleared" in update_note
    assert "SSM parameter launchpad-ppio-api-key deleted" in cleared_note
    assert "Identity provider deleted" in cleared_note
    # the next probe has no key to send — nothing reaches the new host, no SSM read either
    requests_before, reads_before = len(endpoint.requests), list(ssm.calls)
    probe = admin.post(f"{PREFIX}/{pid}/probe").json()
    assert probe["state"] == "failed" and "credential_unavailable" in probe["error"]
    assert len(endpoint.requests) == requests_before and ssm.calls == reads_before
    # entering a key again configures the row for the new host
    again = admin.post(f"{PREFIX}/{pid}/credential", json={"api_key": TYPED})
    assert again.status_code == 200 and again.json()["last4"] == TYPED[-4:]
    assert admin.get(f"{PREFIX}/{pid}").json()["configured"] is True
    # a host change WITH the key in the same request: probed against the NEW host, stored, kept
    endpoint.requests.clear()
    second = TYPED[:-4] + "NEW1"
    endpoint.accepted.add(second)
    both = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.third-host.com/v1",
                                              "api_key": second})
    assert both.status_code == 200, both.text
    assert both.json()["configured"] is True and both.json()["key_last4"] == "NEW1"
    assert str(endpoint.requests[0].url).startswith("https://api.third-host.com/v1/models")
    assert endpoint.sent_keys[-1] == second
    assert ssm.params["launchpad-ppio-api-key"][0] == second
    notes = _trail_notes(pid)
    update_notes = [n for op, n in notes if op == "provider.update" and n]
    assert "api.third-host.com" in update_notes[-1] and "key re-entered" in update_notes[-1]
    # a refused key on a host change stores nothing and keeps the old host + key
    refused = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.fourth-host.com/v1",
                                                 "api_key": TYPED[:-4] + "BAD9"})
    assert refused.status_code == 422 and refused.json()["code"] == "model_provider.probe_failed"
    kept = admin.get(f"{PREFIX}/{pid}").json()
    assert kept["base_url"] == "https://api.third-host.com/v1"
    assert kept["configured"] is True and kept["key_last4"] == "NEW1"
    db = SessionLocal()
    try:
        everything = _everything(db, world["caplog"], moved.json(), both.json(), kept)
    finally:
        db.close()
    for secret in (TYPED, second):
        assert secret not in everything and secret[:-4] not in everything


def test_a_members_personal_row_clears_its_key_on_a_host_change_too(ada, world):
    ada_id, member = ada
    ssm = world["ssm"]
    created = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                        "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    moved = member.put(f"{PREFIX}/{created['id']}",
                       json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["configured"] is False and moved.json()["key_last4"] is None
    assert ssm.deletes == [f"launchpad-user-{ada_id}-ppio-api-key"]
    assert world["control"].calls[-1] == ("delete", f"launchpad-user-{ada_id}-ppio")


def test_an_operator_created_parameter_is_kept_when_the_host_changes(admin, world):
    """Nothing typed in the console (`key_entered_at` NULL): the operator's parameter stays —
    only the Identity provider, the hub record and the cache are cleared; the note says so."""
    ssm, control = world["ssm"], world["control"]
    ssm.params["launchpad-acme-api-key"] = (TYPED, 4)
    world["endpoint"].accepted.add(TYPED)
    created = admin.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                       "label": "Acme", "base_url": URL}).json()
    assert admin.post(f"{PREFIX}/{created['id']}/credential", json={}).status_code == 200
    assert admin.get(f"{PREFIX}/{created['id']}").json()["key_version"] == 4
    moved = admin.put(f"{PREFIX}/{created['id']}",
                      json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200 and moved.json()["configured"] is False
    assert ssm.deletes == [] and ssm.params["launchpad-acme-api-key"] == (TYPED, 4)
    assert control.calls[-1] == ("delete", "launchpad-acme")
    op, note = _trail_notes(created["id"])[-1]
    assert op == "provider.credential"
    assert "SSM parameter launchpad-acme-api-key kept" in note
    assert "Identity provider deleted" in note


# ── N17-obs230 (rev-r20 on 1848803b): a REJECTED host change must not destroy the key ──────
def test_a_rejected_host_change_leaves_the_stored_key_untouched(ada, admin, world):
    """Introduced by the obs228 commit: `clear_api_key` ran BEFORE `update_provider` validated
    the body, so a `PUT {base_url}` that was refused (422 `invalid_base_url`, 409 `in_use`) had
    already deleted the SSM parameter and the Identity provider while the row still said
    configured. Now the row write (with every refusal inside it) comes first; the clearing
    runs only after it committed."""
    ada_id, member = ada
    ssm, control = world["ssm"], world["control"]
    created = member.post(PREFIX, json={"name": "mine", "kind": "openai_compatible",
                                        "label": "Mine", "base_url": URL, "api_key": TYPED}).json()
    pid, param = created["id"], f"launchpad-user-{ada_id}-mine-api-key"
    assert created["configured"] is True and created["key_version"] == 1
    puts, deletes, calls = list(ssm.puts), list(ssm.deletes), list(control.calls)

    def intact(res):
        assert ssm.deletes == deletes and ssm.puts == puts and control.calls == calls, res.text
        assert ssm.params[param][0] == TYPED
        assert f"launchpad-user-{ada_id}-mine" in control.providers
        row = member.get(f"{PREFIX}/{pid}").json()
        assert row["base_url"] == URL and row["configured"] is True
        assert row["key_version"] == 1 and row["key_last4"] == TYPED[-4:]
        db = SessionLocal()
        try:
            record = db.get(Workspace, "default").resources["model_credentials"]["mine"]
            ops = [c.operation for c in db.scalars(sa.select(ModelCatalogChange).where(
                ModelCatalogChange.target_id == pid))]
        finally:
            db.close()
        assert record["configured"] is True
        assert ops == ["provider.create", "provider.credential", "provider.key"]  # no new row

    # (1) an invalid new host: 422 before anything happens
    bad = member.put(f"{PREFIX}/{pid}", json={"base_url": "http://plain.example.com/v1"})
    assert bad.status_code == 422 and bad.json()["code"] == "model_provider.invalid_base_url"
    intact(bad)
    # (2) a valid new host in a body that is refused later in the write (409 in_use: the
    # provider hosts a platform role's model and the body disables it)
    model = member.post(f"{PREFIX}/{pid}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False})
    assert model.status_code == 201, model.text
    assert admin.put(f"{PREFIX}/{pid}", json={"scope": "workspace"}).status_code == 200
    assert admin.put("/api/settings/model-roles/judge",
                     json={"model_id": model.json()["model_id"]}).status_code == 200
    puts, deletes, calls = list(ssm.puts), list(ssm.deletes), list(control.calls)
    refused = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1",
                                                 "enabled": False})
    assert refused.status_code == 409 and refused.json()["code"] == "model_provider.in_use"
    assert ssm.deletes == deletes and ssm.puts == puts and control.calls == calls
    assert ssm.params[param][0] == TYPED
    row = admin.get(f"{PREFIX}/{pid}").json()
    assert row["base_url"] == URL and row["configured"] is True and row["key_version"] == 1
    assert row["enabled"] is True and row["key_last4"] == TYPED[-4:]
    # (3) the happy path still clears everything and journals both notes
    assert admin.delete("/api/settings/model-roles/judge").status_code in (200, 204)
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["configured"] is False and moved.json()["key_version"] is None
    assert ssm.deletes == [param] and param not in ssm.params
    assert control.calls[-1] == ("delete", f"launchpad-user-{ada_id}-mine")
    ops_notes = _trail_notes(pid)
    assert [op for op, _ in ops_notes[-2:]] == ["provider.update", "provider.credential"]
    assert "api.other-host.com" in ops_notes[-2][1] and "deleted" in ops_notes[-1][1]


# ── N17-obs231 (rev-r20 on the obs228 hook): a half-finished clearing must not keep the key ──
def _hub_record(name: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Workspace, "default").resources["model_credentials"][name])
    finally:
        db.close()


def _keyed_provider(client, world) -> str:
    created = client.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                        "label": "PPIO", "base_url": URL, "api_key": TYPED})
    assert created.status_code == 201, created.text
    assert created.json()["configured"] is True and _hub_record("ppio")["configured"] is True
    aws_clients.invalidate_provider_api_key()
    return created.json()["id"]


def test_a_failing_ssm_step_still_unconfigures_the_record_and_deletes_the_identity_provider(
    admin, world, monkeypatch,
):
    """Pre-fix: `clear_api_key` ran SSM → Identity → record; a raise in step 2 (anything but the
    caught ClientError / BotoCoreError — rev-r20 reproduced it with a monkeypatched raise) left
    the row unconfigured while the hub record still said `configured: true` and the Identity
    provider survived, so `provider_api_key` still licensed the SSM read and the next probe sent
    the OLD key to the NEW host. Now the record is unconfigured FIRST, every step runs, the
    residue row carries the error and the caller gets one 502 summary."""
    ssm, control = world["ssm"], world["control"]
    _real_hub_resolver(monkeypatch, world)  # the hub reads keys through SSM, as in production
    hooks = model_registry.probe_hooks()  # the hooks the resolver swap registered
    pid = _keyed_provider(admin, world)
    assert admin.post(f"{PREFIX}/{pid}/probe").json()["state"] == "ok"  # the hub has the key

    def boom(db, provider, actor):
        raise RuntimeError("merge failed mid-way")

    monkeypatch.setattr(hooks, "forget_api_key", boom)
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 502, moved.text
    body = moved.json()
    assert body["code"] == "model_provider.credential_clear_failed"
    assert body["detail"]["failed_steps"] == ["ssm"]
    assert body["detail"]["record_unconfigured"] == 1 and body["detail"]["identity_deleted"]
    # the row write stands; the record is unconfigured; step 3 ran although step 2 raised
    row = admin.get(f"{PREFIX}/{pid}").json()
    assert row["base_url"] == "https://api.other-host.com/v1" and row["configured"] is False
    record = _hub_record("ppio")
    assert record["configured"] is False and record["cleared_on_host_change"] is True
    assert control.calls[-1] == ("delete", "launchpad-ppio")
    assert ssm.deletes == []  # the failing step did not reach the fake
    assert aws_clients.cached_provider_api_key_rows("ppio") == []
    # the residue row names the failed step
    op, note = _trail_notes(pid)[-1]
    assert op == "provider.credential" and "error: ssm: RuntimeError" in note
    assert "SSM parameter launchpad-ppio-api-key kept" in note
    # the next probe has no key to send to the new host — nothing is read from SSM either
    requests_before, reads_before = len(world["endpoint"].requests), list(ssm.calls)
    probe = admin.post(f"{PREFIX}/{pid}/probe").json()
    assert probe["state"] == "failed" and "credential_unavailable" in probe["error"]
    assert len(world["endpoint"].requests) == requests_before and ssm.calls == reads_before
    db = SessionLocal()
    try:
        everything = _everything(db, world["caplog"], body, row)
    finally:
        db.close()
    assert TYPED not in everything and TYPED[:-4] not in everything


def test_a_failing_record_step_still_deletes_the_parameter_and_the_identity_provider(
    admin, world, monkeypatch,
):
    """The local step itself may fail (a ledger write): SSM and Identity are still cleared,
    the error is surfaced, and the summary names the step."""
    ssm, control = world["ssm"], world["control"]
    pid = _keyed_provider(admin, world)

    def boom(db, provider_name, **extra):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(model_registry, "unconfigure_credential_record", boom)
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 502 and moved.json()["detail"]["failed_steps"] == ["record"]
    assert ssm.deletes == ["launchpad-ppio-api-key"] and "launchpad-ppio-api-key" not in ssm.params
    assert control.calls[-1] == ("delete", "launchpad-ppio")
    assert moved.json()["detail"]["ssm_deleted"] and moved.json()["detail"]["identity_deleted"]
    assert admin.get(f"{PREFIX}/{pid}").json()["configured"] is False
    assert "error: record: RuntimeError" in _trail_notes(pid)[-1][1]


def test_an_aws_refusal_in_one_step_is_reported_not_raised_and_the_next_step_runs(admin, world):
    """AWS refusals stay best-effort (as before): a denied `DeleteParameter` is in the report
    and the note, the Identity provider is still deleted, the request is a 200."""
    ssm, control = world["ssm"], world["control"]
    pid = _keyed_provider(admin, world)
    ssm.delete_error = "AccessDeniedException"
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["configured"] is False
    assert ssm.deletes == ["launchpad-ppio-api-key"] and "launchpad-ppio-api-key" in ssm.params
    assert control.calls[-1] == ("delete", "launchpad-ppio")
    assert _hub_record("ppio")["configured"] is False
    note = _trail_notes(pid)[-1][1]
    assert "SSM parameter launchpad-ppio-api-key kept" in note
    assert "Identity provider deleted" in note and "error: ssm: AccessDeniedException" in note


def test_without_a_probe_module_the_host_change_still_unconfigures_the_hub_record(admin, world):
    """`probe_hooks()` None (a build without the probe module): nothing can reach SSM /
    Identity, but the hub record — what licenses the SSM read — is flipped locally and the
    residue row says what was left in place."""
    ssm, control = world["ssm"], world["control"]
    pid = _keyed_provider(admin, world)
    model_registry.set_probe_hooks(None)
    deletes, calls = list(ssm.deletes), list(control.calls)
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["configured"] is False and moved.json()["key_last4"] is None
    assert ssm.deletes == deletes and control.calls == calls  # no AWS reachable, none called
    record = _hub_record("ppio")
    assert record["configured"] is False and record["cleared_on_host_change"] is True
    assert aws_clients.cached_provider_api_key_rows("ppio") == []
    note = _trail_notes(pid)[-1][1]
    assert "SSM parameter launchpad-ppio-api-key kept" in note
    assert "Identity provider kept" in note and "error: no probe module registered" in note


# ── N17-obs232 (deploy-ops-29 on t1): tags ride the create; a refused tag call never gates ──
def test_replacing_a_key_overwrites_the_parameter_and_retags_it_best_effort(admin, world):
    """The second write meets `ParameterAlreadyExists` (the API refuses Tags with Overwrite):
    the key is overwritten without tags and ONE `AddTagsToResource` follows."""
    ssm = world["ssm"]
    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    second = TYPED[:-4] + "NEW1"
    world["endpoint"].accepted.add(second)
    res = admin.post(f"{PREFIX}/{created['id']}/credential", json={"api_key": second})
    assert res.status_code == 200 and res.json()["last4"] == "NEW1"
    assert [(p["Overwrite"], "Tags" in p) for p in ssm.puts] == [
        (False, True),   # the create, tagged
        (False, True),   # the replacement tries the tagged create first → ParameterAlreadyExists
        (True, False),   # → overwrite without tags
    ]
    assert ssm.params["launchpad-ppio-api-key"][0] == second
    assert len(ssm.tags) == 1 and ssm.tags[0]["ResourceId"] == "launchpad-ppio-api-key"
    assert {t["Key"] for t in ssm.tags[0]["Tags"]} == {"launchpad:managed", "launchpad:provider"}


def test_a_refused_tag_call_on_the_overwrite_path_is_retried_once_and_never_a_gate(admin, world):
    """deploy-ops-29: `PutParameter` allowed, `AddTagsToResource` refused with
    AccessDeniedException twice. The key is stored, the row configured, the refusal is a
    WARNING naming the code (one retry on a fresh client), and no secret reaches any log line."""
    ssm, caplog = world["ssm"], world["caplog"]
    created = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                       "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    assert created["configured"] is True and ssm.tags == []  # the create needed no tag call
    ssm.tag_error = "AccessDeniedException"
    second = TYPED[:-4] + "NEW1"
    world["endpoint"].accepted.add(second)
    res = admin.put(f"{PREFIX}/{created['id']}", json={"api_key": second})
    assert res.status_code == 200, res.text
    assert res.json()["configured"] is True and res.json()["key_last4"] == "NEW1"
    assert ssm.params["launchpad-ppio-api-key"][0] == second
    assert len(ssm.tags) == 2  # the hub client, then ONE retry on a fresh client
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"
                and "could not tag SSM parameter" in r.getMessage()]
    assert len(warnings) == 2 and all("AccessDeniedException" in w for w in warnings)
    assert "retrying once on a fresh client" in warnings[0]
    assert "left untagged" in warnings[1] and "ssm:AddTagsToResource" in warnings[1]
    db = SessionLocal()
    try:
        everything = _everything(db, caplog, created, res.json())
    finally:
        db.close()
    for secret in (TYPED, second):
        assert secret not in everything and secret[:-4] not in everything


def test_a_refused_tagged_create_falls_back_to_an_untagged_create(admin, world):
    """A role allowed to PutParameter but not to tag on create: the key write still goes
    through (untagged), the tags are then best-effort."""
    ssm = world["ssm"]
    ssm.tagged_create_error = "AccessDeniedException"
    res = admin.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                   "label": "PPIO", "base_url": URL, "api_key": TYPED})
    assert res.status_code == 201, res.text
    assert res.json()["configured"] is True
    assert [(p["Overwrite"], "Tags" in p) for p in ssm.puts] == [(False, True), (False, False)]
    assert ssm.params["launchpad-ppio-api-key"][0] == TYPED
    assert len(ssm.tags) == 1 and ssm.parameter_tags["launchpad-ppio-api-key"]


def test_the_fresh_ssm_client_is_a_funnel_client_off_the_same_session(monkeypatch):
    """`aws_clients.ssm_client(ctx, fresh=True)` builds an uncached client through the funnel
    (same session, same credential resolver) — never a second boto3 session."""
    from app.services import workspace as ws

    aws_clients.reset_cache()
    ctx = ws.WorkspaceContext(id="default", account_id="111122223333", region="cn-northwest-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    cached = aws_clients.ssm_client(ctx)
    assert aws_clients.ssm_client(ctx) is cached
    fresh = aws_clients.ssm_client(ctx, fresh=True)
    assert fresh is not cached and fresh is not aws_clients.ssm_client(ctx, fresh=True)
    assert fresh.meta.region_name == "cn-northwest-1"
    assert len(aws_clients._SESSIONS) == 1  # one session for the hub target, both clients off it
    aws_clients.reset_cache()


# ── N17-obs254 (rev-r22 on obs231): the row's own key facts gate the SSM read ───────────────
def test_a_failed_record_step_with_a_surviving_parameter_still_cannot_send_the_old_key(
    admin, world, monkeypatch,
):
    """rev-r22's repro: step 1 (the record) raises AND the parameter survives (DeleteParameter
    refused — the same end state as an operator-created parameter, which is never deleted). The
    hub record still says `configured: true`, yet the next probe must not read the key: the
    row's key facts were reset in the SAME commit as the new `base_url`, and
    `aws_clients.provider_api_key` now requires them next to the record."""
    ssm, control, endpoint = world["ssm"], world["control"], world["endpoint"]
    _real_hub_resolver(monkeypatch, world)
    pid = _keyed_provider(admin, world)
    assert admin.post(f"{PREFIX}/{pid}/probe").json()["state"] == "ok"
    broken = {"on": True}
    real = model_registry.unconfigure_credential_record

    def flaky(db, provider_name, **extra):
        if broken["on"]:
            raise RuntimeError("ledger locked")
        return real(db, provider_name, **extra)

    monkeypatch.setattr(model_registry, "unconfigure_credential_record", flaky)
    ssm.delete_error = "AccessDeniedException"
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 502 and moved.json()["detail"]["failed_steps"] == ["record"]
    assert moved.json()["detail"]["ssm_deleted"] is False
    broken["on"] = False
    # the dangerous end state: record configured, parameter present, row re-pointed
    assert _hub_record("ppio")["configured"] is True
    assert ssm.params["launchpad-ppio-api-key"][0] == TYPED
    assert control.calls[-1] == ("delete", "launchpad-ppio")
    requests_before, reads_before = len(endpoint.requests), list(ssm.calls)
    probe = admin.post(f"{PREFIX}/{pid}/probe").json()
    assert probe["state"] == "failed" and "credential_unavailable" in probe["error"]
    assert "holds no registered key" in probe["error"]
    assert len(endpoint.requests) == requests_before  # zero outbound requests
    assert ssm.calls == reads_before  # the parameter was not even read
    assert not any(k == TYPED for k in endpoint.sent_keys[requests_before:])
    # entering the key again re-arms the row for the new host
    assert admin.post(f"{PREFIX}/{pid}/credential", json={"api_key": TYPED}).status_code == 200
    assert admin.post(f"{PREFIX}/{pid}/probe").json()["state"] == "ok"
    assert str(endpoint.requests[-1].url).startswith("https://api.other-host.com/v1")


def test_the_key_is_unreadable_from_the_row_commit_on_before_any_clearing_step_runs(
    admin, world, monkeypatch,
):
    """Success-path ordering: while `clear_api_key` is still running (the record still says
    configured, the parameter still exists), the hub can no longer read the key."""
    ssm = world["ssm"]
    _real_hub_resolver(monkeypatch, world)
    hooks = model_registry.probe_hooks()
    pid = _keyed_provider(admin, world)
    assert aws_clients.provider_api_key("ppio") == TYPED
    seen: dict = {}
    real_forget = hooks.forget_api_key

    def observing_forget(db, provider, actor):
        seen["record_during"] = _hub_record("ppio")["configured"]  # step 1 already ran
        seen["param_present"] = "launchpad-ppio-api-key" in ssm.params
        try:
            aws_clients.provider_api_key("ppio")
            seen["readable"] = True
        except RuntimeError as exc:
            seen["readable"] = False
            seen["why"] = str(exc)
        return real_forget(db, provider, actor)

    monkeypatch.setattr(hooks, "forget_api_key", observing_forget)
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    assert seen["param_present"] is True and seen["readable"] is False
    assert seen["record_during"] is False  # step 1 (the record) ran before step 2 (SSM)
    assert "launchpad-ppio-api-key" in seen["why"] and TYPED not in seen["why"]


# ── rev-r22 nit on obs232: a typed key over an operator-created parameter is not "console-written"
def test_a_typed_key_over_an_operator_created_parameter_never_deletes_that_parameter(admin,
                                                                                     world):
    ssm = world["ssm"]
    ssm.params["launchpad-acme-api-key"] = ("operator-key-0000", 4)  # the operator's parameter
    world["endpoint"].accepted.add(TYPED)
    created = admin.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                       "label": "Acme", "base_url": URL, "api_key": TYPED})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["configured"] is True and body["key_last4"] == TYPED[-4:]
    assert ssm.params["launchpad-acme-api-key"][0] == TYPED  # the value was replaced…
    assert [(p["Overwrite"], "Tags" in p) for p in ssm.puts] == [(False, True), (True, False)]
    db = SessionLocal()
    try:
        row = db.scalar(sa.select(ModelProvider).where(ModelProvider.name == "acme"))
        assert row.key_last4 == TYPED[-4:] and row.key_entered_at is None  # …not stamped
    finally:
        db.close()
    op, note = _trail_notes(body["id"])[-1]
    assert op == "provider.key" and "operator-created SSM parameter" in note
    assert admin.delete(f"{PREFIX}/{body['id']}").status_code == 204
    assert ssm.deletes == [] and "launchpad-acme-api-key" in ssm.params  # kept on delete


# ── rev-r22 nit (a) on obs254: the key cache is keyed by host too ───────────────────────────
def test_a_key_cached_for_the_old_host_is_never_served_for_the_new_one(admin, world, monkeypatch):
    """Even if the registry's invalidation on a host change were skipped, the cache entry is
    keyed by the row's `base_url`, so the re-pointed row is a miss — and then the row's reset
    key facts refuse the SSM read (obs254)."""
    _real_hub_resolver(monkeypatch, world)
    pid = _keyed_provider(admin, world)
    assert aws_clients.provider_api_key("ppio") == TYPED  # cached now
    assert aws_clients.cached_provider_api_key_rows("ppio") != []
    monkeypatch.setattr(model_registry, "_forget_cached_key", lambda name: None)  # defeated
    moved = admin.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.other-host.com/v1"})
    assert moved.status_code == 200, moved.text
    # the obs228 hook dropped the cache itself; put the OLD-host entry back to prove the key
    aws_clients._PROVIDER_API_KEYS[("ppio", pid, "launchpad-ppio-api-key", URL)] = (TYPED, 1e18)
    with pytest.raises(RuntimeError) as refused:  # a miss: the cached entry names the OLD host
        aws_clients.provider_api_key("ppio")
    assert TYPED not in str(refused.value) and "launchpad-ppio-api-key" in str(refused.value)
    aws_clients.invalidate_provider_api_key()
