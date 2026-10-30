"""Stream A1 — the hub's principal-token minting (contract I1,
docs/cn/interfaces/policy-principal-contracts.md; design §13.1 D1 / D4 / D5 / D6, §14.2 A1).

KMS ``Sign(DIGEST, ECDSA_SHA_256)`` is stubbed by the stage-0 ``EphemeralSigner`` (same digest
in / DER out shape); every minted token must verify with ``codec.verify_token`` and every
claim set must satisfy ``principal_token_claims.schema.json`` BEFORE it is signed."""

from __future__ import annotations

import base64
import json
import logging

import jsonschema
import pytest
from cryptography.hazmat.primitives import serialization

from app.policy import contracts
from app.policy.contracts import codec
from app.services import principal_token as pt
from tests.policy_principal.conftest import load_fixture
from tests.policy_principal.fakes import EphemeralSigner

KEY_ID = "1234abcd-12ab-34cd-56ef-1234567890ab"
ALIAS = "alias/launchpad-principal"
NOW = 1_793_022_000


class StubKms:
    """KMS ``DescribeKey`` / ``Sign`` / ``GetPublicKey`` over an in-memory P-256 key."""

    def __init__(self, signer: EphemeralSigner) -> None:
        self.signer = signer
        self.calls: list[tuple[str, dict]] = []

    def describe_key(self, **kw):
        self.calls.append(("describe_key", kw))
        return {"KeyMetadata": {"KeyId": KEY_ID, "KeySpec": "ECC_NIST_P256",
                                "KeyUsage": "SIGN_VERIFY", "Enabled": True}}

    def sign(self, **kw):
        self.calls.append(("sign", {k: v for k, v in kw.items() if k != "Message"}))
        assert len(kw["Message"]) == 32  # a sha256 digest, never the token text
        return {"KeyId": KEY_ID, "Signature": self.signer.sign_digest(kw["Message"]),
                "SigningAlgorithm": "ECDSA_SHA_256"}

    def get_public_key(self, **kw):
        self.calls.append(("get_public_key", kw))
        der = self.signer.public_key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"KeyId": KEY_ID, "PublicKey": der, "KeySpec": "ECC_NIST_P256"}


@pytest.fixture
def kms(signer) -> StubKms:
    return StubKms(signer)


def _validate(claims: dict) -> None:
    jsonschema.Draft202012Validator(contracts.load_schema("principal_token_claims")).validate(claims)


USER = pt.TokenPrincipal.user("hr-intern", role="member", user_id="u_hrintern01")


def _claims(**over):
    kw = dict(workspace_id="default", agent_id="ag_hrhelp01", session_id="s" * 40, tsv=3,
              generation=0, channel="chat", principal=USER, method="harness", now=NOW,
              jti="jti-0001-user-member")
    kw.update(over)
    return pt.build_claims(**kw)


def test_the_fixture_shape_is_reproduced_and_psid_is_derived(monkeypatch):
    monkeypatch.setattr(pt, "current_env_name", lambda: "")
    case = next(c for c in load_fixture("claims_valid.json")["cases"]
                if c["name"] == "user_member_chat")["claims"]
    built = _claims(session_id=case["sid"], now=case["iat"], jti=case["jti"])
    assert {k: v for k, v in built.items() if k != "psid"} == {
        k: v for k, v in case.items() if k != "psid"}
    assert built["psid"] == codec.psid_for("default", case["sid"], 3, 0)
    _validate(built)


@pytest.mark.parametrize("method, ttl", [
    ("harness", 900), ("zip_runtime", 900), ("studio", 900), ("byoc", 300),
    ("discovered", 300), ("unknown-method", 300),
])
def test_ttl_follows_the_method(method, ttl):
    claims = _claims(method=method)
    assert claims["exp"] - claims["iat"] == ttl  # D4: platform templates 15 min, else 5 min


def test_audience_is_the_agent_first_then_its_peers_deduplicated():
    claims = _claims(audience=["ag_peer0001", "ag_hrhelp01", "ag_peer0001"])
    assert claims["aud"] == ["ag_hrhelp01", "ag_peer0001"]


def test_full_audience_with_distinct_engine_epochs_fits_wire_limit(kms, signer):
    peers = [f"ag_{i:029d}" for i in range(31)]
    claims = _claims(audience=peers, engine_versions={f"pe_{i:012x}": i for i in range(32)})
    token = pt.mint(claims, pt.KmsSigner(kms, ALIAS))
    assert len(token) <= codec.MAX_TOKEN_CHARS
    verified = codec.verify_token(token, {KEY_ID: signer.public_key}, now=NOW,
                                  workspace_id="default", env=claims["env"], agent_id="ag_hrhelp01")
    assert verified.claims["engine_versions"] == claims["engine_versions"]


def test_principals_follow_d1_d5_d6():
    key = pt.TokenPrincipal.api_key("key_demo0001", name="demo-key")
    assert key.claim() == {"kind": "api_key", "id": "key_demo0001", "label": "demo-key"}
    assert "role" not in key.claim()  # D1: its own principal, no role, no parent
    assert pt.TokenPrincipal.system("eval").claim() == {
        "kind": "system", "id": "eval", "label": "system:eval"}
    admin = pt.TokenPrincipal.user("demo-admin", role="admin", user_id="u_demoadmin1")
    assert admin.claim()["role"] == "admin"
    with pytest.raises(ValueError):
        pt.TokenPrincipal.user("x", role="owner")
    with pytest.raises(ValueError):
        pt.TokenPrincipal.system("root")
    for claims in (_claims(principal=key, channel="v1"),
                   _claims(principal=pt.TokenPrincipal.system("canary"), channel="canary")):
        _validate(claims)


def test_an_invalid_claim_set_is_refused_before_signing():
    with pytest.raises(pt.PrincipalTokenUnavailable) as err:
        _claims(channel="sms")
    assert err.value.code == "policy.principal_invalid"


def test_mint_signs_the_digest_with_kms_and_the_token_verifies(kms, signer, monkeypatch):
    monkeypatch.setattr(pt, "current_env_name", lambda: "")
    minter = pt.KmsSigner(kms, ALIAS)
    claims = _claims()
    token = pt.mint(claims, minter)
    verified = codec.verify_token(token, {KEY_ID: signer.public_key}, now=NOW + 5,
                                  workspace_id="default", env="", agent_id="ag_hrhelp01")
    assert verified.claims == claims and verified.header["kid"] == KEY_ID
    signs = [kw for name, kw in kms.calls if name == "sign"]
    assert signs == [{"KeyId": KEY_ID, "MessageType": "DIGEST",
                      "SigningAlgorithm": "ECDSA_SHA_256"}]
    # the alias resolves once; later mints reuse the KeyId
    pt.mint(_claims(jti="jti-00000002"), minter)
    assert [n for n, _ in kms.calls].count("describe_key") == 1


def test_the_public_key_is_exported_as_bundle_spki(kms, signer):
    minter = pt.KmsSigner(kms, ALIAS)
    assert minter.public_spki_b64() == signer.public_spki_b64
    assert codec.load_public_key(minter.public_spki_b64()).public_numbers() == \
        signer.public_key.public_numbers()
    base64.b64decode(minter.public_spki_b64(), validate=True)


def test_a_key_that_is_not_p256_sign_verify_is_refused(kms):
    kms.describe_key = lambda **_: {"KeyMetadata": {"KeyId": KEY_ID, "KeySpec": "RSA_2048",
                                                    "KeyUsage": "SIGN_VERIFY", "Enabled": True}}
    with pytest.raises(pt.PrincipalTokenUnavailable) as err:
        pt.mint(_claims(), pt.KmsSigner(kms, ALIAS))
    assert err.value.code == "policy.principal_unavailable"


def test_no_configured_key_means_no_token():
    with pytest.raises(pt.PrincipalTokenUnavailable) as err:
        pt.KmsSigner(object(), "")
    assert err.value.code == "policy.principal_unavailable"


def test_a_kms_failure_surfaces_as_unavailable_without_the_digest(kms):
    def boom(**_):
        raise RuntimeError("AccessDeniedException")
    kms.sign = boom
    with pytest.raises(pt.PrincipalTokenUnavailable) as err:
        pt.mint(_claims(), pt.KmsSigner(kms, ALIAS))
    assert err.value.code == "policy.principal_unavailable"


def test_the_token_never_reaches_a_log(kms, caplog):
    caplog.set_level(logging.DEBUG)
    token = pt.mint(_claims(), pt.KmsSigner(kms, ALIAS))
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text  # nor its signature




def test_the_claims_are_compact_json_serialisable():
    json.dumps(_claims())
