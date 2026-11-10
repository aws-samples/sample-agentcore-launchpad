"""The audit trail of a model provider (N17-obs203 §4).

Every provider create / update (a label, a `base_url` = host change, a scope flip, a key replace)
/ delete writes one `model_catalog_changes` row — the existing journal of the registry, the same
mechanism `GET /changes` and the console's 「变更记录 / Changes」 list read — carrying the console
user (`actor`), the provider (`target_id`), the action (`operation`), the time (`created_at`) and
the before / after dicts (the host inside `base_url`, the scope, the key's last four characters);
never key material. `GET /changes?target_id=` narrows the journal to one provider's trail for the
administrator's per-provider list; members never read the journal.
"""

from __future__ import annotations

import json

import pytest

from tests import test_model_provider_keys as keys
from tests import test_visibility as tv

PREFIX, TYPED, URL = keys.PREFIX, keys.TYPED, keys.URL
# the fixtures, re-exported under their own names (pytest finds module-level fixtures)
app, admin, ada, world, _hermetic = tv.app, tv.admin, tv.ada, keys.world, keys._hermetic

pytestmark = pytest.mark.siliconflow


def _trail(client, provider_id: str) -> list[dict]:
    res = client.get(f"{PREFIX}/changes", params={"target_id": provider_id, "limit": 50})
    assert res.status_code == 200, res.text
    return list(reversed(res.json()["items"]))  # oldest first


def test_every_provider_write_leaves_one_row_with_user_provider_host_action_and_time(
    admin, ada, world,
):
    ada_id, member = ada
    created = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible",
                                        "label": "PPIO", "base_url": URL, "api_key": TYPED}).json()
    pid = created["id"]
    assert member.put(f"{PREFIX}/{pid}", json={"label": "PPIO cloud"}).status_code == 200
    assert member.put(f"{PREFIX}/{pid}", json={"base_url": "https://api.ppinfra.com/v3"}
                      ).status_code == 200
    assert admin.put(f"{PREFIX}/{pid}", json={"scope": "workspace"}).status_code == 200
    world["endpoint"].accepted.add(TYPED[:-4] + "NEW1")
    assert admin.post(f"{PREFIX}/{pid}/credential",
                      json={"api_key": TYPED[:-4] + "NEW1"}).status_code == 200
    assert admin.delete(f"{PREFIX}/{pid}").status_code == 204

    trail = _trail(admin, pid)
    assert [(c["operation"], c["actor"]) for c in trail] == [
        ("provider.create", "ada"),
        ("provider.credential", "ada"),  # the typed key reached Identity
        ("provider.key", "ada"),  # … and SSM — last4 only
        ("provider.update", "ada"),  # label
        ("provider.update", "ada"),  # host
        ("provider.credential", "ada"),  # N17-obs228: the stored key cleared on the host change
        ("provider.update", tv_admin_name()),  # scope flip: the administrator
        ("provider.credential", tv_admin_name()),  # key replace: Identity rotated
        ("provider.key", tv_admin_name()),
        ("provider.delete", tv_admin_name()),
    ]
    assert all(c["target_id"] == pid and c["created_at"] for c in trail)
    create, _cred, key, label, host, cleared, scope, _cred2, key2, delete = trail
    # the create row names the owner (users.id), the scope and the host
    assert create["after"]["created_by"] == ada_id and create["after"]["scope"] == "personal"
    assert create["after"]["base_url"] == URL
    # the host change is visible as before/after base_url
    assert host["before"]["base_url"] == URL
    assert host["after"]["base_url"] == "https://api.ppinfra.com/v3"
    assert cleared["after"] == {"configured": False, "cleared": True}
    assert label["before"]["label"] == "PPIO" and label["after"]["label"] == "PPIO cloud"
    # the scope flip
    assert scope["before"]["scope"] == "personal" and scope["after"]["scope"] == "workspace"
    # the key rows carry the parameter name, last4 and version — nothing more
    assert key["after"] == {"ssm_parameter": f"launchpad-user-{ada_id}-ppio-api-key",
                            "last4": TYPED[-4:], "key_version": 1}
    assert key2["after"]["last4"] == "NEW1" and key2["after"]["key_version"] == 2
    # the delete keeps the full before dict and the SSM residue note
    assert delete["after"] is None and delete["before"]["name"] == "ppio"
    # no key material anywhere in the trail (nor in the unfiltered journal)
    everything = json.dumps(trail) + admin.get(f"{PREFIX}/changes").text
    for secret in (TYPED, TYPED[:-4] + "NEW1"):
        assert secret not in everything and secret[:-4] not in everything


def test_the_target_filter_isolates_one_providers_trail_and_members_cannot_read_it(admin, ada):
    _ada_id, member = ada
    one = member.post(PREFIX, json={"name": "one", "kind": "openai_compatible", "label": "One",
                                    "base_url": URL}).json()
    two = member.post(PREFIX, json={"name": "two", "kind": "openai_compatible", "label": "Two",
                                    "base_url": URL}).json()
    assert member.put(f"{PREFIX}/{two['id']}", json={"label": "Two!"}).status_code == 200
    assert {c["target_id"] for c in _trail(admin, one["id"])} == {one["id"]}
    assert [c["operation"] for c in _trail(admin, two["id"])] == ["provider.create",
                                                                  "provider.update"]
    unfiltered = admin.get(f"{PREFIX}/changes").json()["items"]
    assert {c["target_id"] for c in unfiltered} >= {one["id"], two["id"]}
    # the journal is an administrator's — the owner of the rows cannot read it
    res = member.get(f"{PREFIX}/changes", params={"target_id": one["id"]})
    assert res.status_code == 403 and res.json()["code"] == "auth.forbidden"
    # paging + filter compose
    newest = admin.get(f"{PREFIX}/changes", params={"target_id": two["id"], "limit": 1}).json()
    assert [c["operation"] for c in newest["items"]] == ["provider.update"]
    older = admin.get(f"{PREFIX}/changes", params={"target_id": two["id"],
                                                   "before": newest["items"][0]["id"]}).json()
    assert [c["operation"] for c in older["items"]] == ["provider.create"]


def tv_admin_name() -> str:
    from tests.test_visibility import ADMIN

    return ADMIN["username"]
