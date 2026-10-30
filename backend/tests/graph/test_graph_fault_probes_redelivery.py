# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Round-8 fault probes (r17-r; testing.md §6.9 "Round 8") at two seams no graph edge reaches:

* the extractor Lambda's **SQS redelivery** — at-least-once delivery means a message the
  handler already processed may arrive again, and a message a fault interrupted WILL arrive
  again (`RetryableError` → `batchItemFailures`): the redelivery must converge to the same index
  the one clean run leaves (memory-backend.md "Merge strategy (v2)": one record per fact, the
  target retired in the same operation, the scan clean);
* the **Identity control plane** behind the connection delete (`identity_providers.
  delete_connection`: the vault delete first, the audit row second) — a vault fault must leave the
  row (and every user grant on it) in place and answer the mapped `aws.*` envelope (api.md
  "Errors"); a provider gone between the listing and the delete is the N16-obs87 class.
"""

from __future__ import annotations

import json

import pytest
from botocore.exceptions import ClientError
from sqlalchemy import select

from app.core.db import DEFAULT_WORKSPACE_ID
from app.core.errors import AWS_ERROR_MAP, aws_error_code
from app.models.ledger import IdentityProvider, UserGrant
from app.services import identity_providers as ip
from app.services import oauth_sessions
from tests.graph import faults
from tests.graph.fault_seams import MemoryFaults, make_client_dict
from tests.property.ledger_invariants import record_is_active, scan_memory_index
from tests.property.test_memory_merge_properties import (  # noqa: F401 — fixtures by name
    MEM,
    handler,
    with_cosine,
)
from tests.test_identity_connections import _create_oauth, db, make_control  # noqa: F401
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

# ---------------------------------------------------------------------------
# SQS redelivery — the extractor
# ---------------------------------------------------------------------------


@pytest.fixture
def memory_world(admin, ada, bob, handler):
    clients, _ = make_client_dict(admin, ada, bob)
    return MemoryFaults(clients, handler)


def _same_event_again(world: MemoryFaults, ops: list[dict]) -> dict:
    """The SAME ops the previous event carried, as the redelivery of that message would run them
    (the model's output is the message's content here, so it is byte-identical)."""
    return world.extract(ops)


def test_redelivery_of_a_processed_add_is_idempotent(memory_world):
    """At-least-once: a successfully processed `add` arriving again must leave ONE record (the
    record id derives from (store, namespace, text) — the second `put_doc` overwrites) and a
    clean scan; the duplicate is counted, never a second active fact."""
    w = memory_world
    graph_add = [{"op": "add", "text": "likes tea", "confidence": 0.9}]
    w.w.emb.assign("likes tea", w.base)
    first = w.extract(graph_add)
    docs_after_first = {rid: dict(d) for rid, d in w.w.docs.items()}
    second = _same_event_again(w, graph_add)
    assert first["counts"].get("facts", 0) == 1, first["counts"]
    assert len(w.w.docs) == len(docs_after_first) == 1, (first["counts"], second["counts"])
    assert scan_memory_index(w.w.docs) == []
    assert second["counts"].get("duplicates", 0) >= 1 or second["counts"].get("facts") == 1, (
        second["counts"])


def test_redelivery_of_a_processed_supersede_does_not_chain(memory_world):
    """A processed `supersede` arriving again: the target is already retired and the successor
    is the nearest active candidate — the redelivery must not retire the successor with a copy
    of itself (self-supersede guard) nor leave a dangling chain; the index equals the one clean
    run's (scan clean, exactly one active record)."""
    w = memory_world
    graph = w.graph
    from tests.graph.executor import GRAPHS

    g = GRAPHS[graph]
    w.step(g.edge("add"))
    w.step(g.edge("supersede"))
    before = {rid: dict(d) for rid, d in w.w.docs.items()}
    ops = [{"op": "supersede", "target_id": "c1", "text": "switched to coffee", "confidence": 0.9}]
    _same_event_again(w, ops)
    after = w.w.docs
    assert scan_memory_index(after) == [], scan_memory_index(after)
    active = [rid for rid, d in after.items() if record_is_active(d)]
    assert len(active) == 1, ("the redelivery left a second active fact", active)
    assert after[w.rid]["superseded_by"] == w.newer, after[w.rid]
    new_docs = set(after) - set(before)
    assert not new_docs or all(not record_is_active(after[r]) for r in new_docs), (
        "the redelivery added an active record", new_docs)


def test_redelivery_after_a_half_written_supersede_converges(memory_world):
    """N17-obs122 (fixed in cn/r17-n): the index holds a half-written pair — the successor
    (`supersedes=[original]`) and the ORIGINAL both active (the pre-fix two-write supersede
    interrupted between its writes, or a partial item failure inside the one bulk request). The
    redelivery of the same op dedupes against the successor (nearest at cosine 1.0) BEFORE the
    supersede branch; the duplicate branch now finishes the pair: the original is retired under
    the successor, exactly one record is active, the scan is clean. The pair is seeded directly
    because the fixed extractor can no longer produce it through an `update_doc` fault."""
    w = memory_world
    from tests.graph.executor import GRAPHS

    g = GRAPHS["memory_record"]
    w.step(g.edge("add"))
    target = w.rid
    text = "switched to coffee"
    before = set(w.w.docs)
    w.w.seed(None, text, with_cosine(w.current_vector(), 0.7, 200 + w.events),
             last_op="supersede", supersedes=[target])
    successor = next(rid for rid in w.w.docs if rid not in before)
    half = w.w.docs
    assert half[successor]["supersedes"] == [target] and record_is_active(half[target])
    assert record_is_active(half[successor])
    # the redelivery (same message, seam healthy) must retire the target
    w.extract([{"op": "supersede", "target_id": "c1", "text": text, "confidence": 0.9}])
    after = w.w.docs
    assert after[target].get("superseded_by") == successor, (
        "the redelivery did not retire the original", after[target])
    assert scan_memory_index(after) == [], scan_memory_index(after)
    assert [rid for rid, d in after.items() if record_is_active(d)] == [successor]


# ---------------------------------------------------------------------------
# the Identity control plane behind the connection delete
# ---------------------------------------------------------------------------


def _modelled(exc: ClientError) -> ClientError:
    cls = type(aws_error_code(exc), (ClientError,), {})
    return cls(exc.response, exc.operation_name)


def _grant(db) -> None:
    oauth_sessions.record_pending(
        db, DEFAULT_WORKSPACE_ID, session_uri="urn:s1", provider="team-idp",
        user_id="alice", agent_id="a1", tool="t", scopes=["openid"],
    )


def _row_and_grants(db) -> tuple[int, int]:
    rows = db.scalars(select(IdentityProvider).where(IdentityProvider.name == "team-idp")).all()
    grants = db.scalars(select(UserGrant).where(UserGrant.provider == "team-idp")).all()
    return len(rows), len(grants)


@pytest.mark.parametrize("shape_name", ["rt_access_denied", "runtime_throttle_client",
                                        "runtime_bedrock_5xx_client", "rt_conflict_modifying"])
def test_connection_delete_vault_fault_keeps_the_row_and_the_grants(db, shape_name):
    """identity_providers.delete_connection: the vault delete runs FIRST, the audit row and the
    user grants go after it — a vault fault must leave both exactly as they were (nothing half-
    deleted) and surface as the ClientError the route maps (api.md "Errors")."""
    control = make_control()
    _create_oauth(control, db)
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    _grant(db)
    before = _row_and_grants(db)
    assert before == (1, 1)
    shape = faults.SHAPES[shape_name]
    control.delete_oauth2_credential_provider.side_effect = _modelled(
        shape.exception("DeleteOauth2CredentialProvider"))
    with pytest.raises(ClientError) as info:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    db.rollback()
    assert aws_error_code(info.value) == shape.code
    assert _row_and_grants(db) == before, "a failed vault delete must not touch the ledger"
    mapped = AWS_ERROR_MAP.get(shape.code or "")
    assert mapped is not None or shape.code == "InternalServerException", (
        shape.code, "an unmapped code is the documented unhandled 500 (api.md)")


def test_connection_delete_when_the_provider_vanished_between_listing_and_delete(db):
    """The N16-obs87 class on the vault: the listing still names the provider, the delete
    answers ResourceNotFoundException. Today the 404 propagates (`aws.not_found`) and the row
    stays; the NEXT attempt lists it as gone and cleans the row ("A ledger row whose provider is
    already gone from the vault is still cleaned up") — two attempts converge. Recorded, not a
    finding: the agent delete read back first (N16-obs87) and the contract for Connections says
    nothing either way."""
    control = make_control()
    _create_oauth(control, db)
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": "team-idp"}]
    }
    control.delete_oauth2_credential_provider.side_effect = _modelled(
        faults.SHAPES["rt_not_found"].exception("DeleteOauth2CredentialProvider"))
    with pytest.raises(ClientError) as info:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    db.rollback()
    assert aws_error_code(info.value) == "ResourceNotFoundException"
    assert _row_and_grants(db)[0] == 1
    # the second attempt: the vault no longer lists it → the row-only cleanup
    control.list_oauth2_credential_providers.return_value = {"credentialProviders": []}
    control.delete_oauth2_credential_provider.side_effect = None
    ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    assert _row_and_grants(db) == (0, 0)
    assert control.delete_oauth2_credential_provider.call_count == 1  # not called again


def test_connection_delete_listing_fault_refuses_before_any_write(db):
    """`provider_names` (the vault listing) throttled: the delete refuses before the vault delete
    and before the row — the mapped 429, nothing written, no delete call."""
    control = make_control()
    _create_oauth(control, db)
    _grant(db)
    control.list_oauth2_credential_providers.side_effect = _modelled(
        faults.SHAPES["runtime_throttle_client"].exception("ListOauth2CredentialProviders"))
    with pytest.raises(ClientError) as info:
        ip.delete_connection(control, db, DEFAULT_WORKSPACE_ID, "oauth2", "team-idp")
    db.rollback()
    assert aws_error_code(info.value) == "ThrottlingException"
    assert _row_and_grants(db) == (1, 1)
    control.delete_oauth2_credential_provider.assert_not_called()
    assert json.dumps(AWS_ERROR_MAP["ThrottlingException"]) == '[429, "aws.throttled"]'
