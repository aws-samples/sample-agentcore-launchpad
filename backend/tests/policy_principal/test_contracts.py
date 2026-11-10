"""Stage-0 contract tests (docs/cn/interfaces/policy-principal-contracts.md): every fixture
validates against its schema, the reference codec round-trips and rejects exactly the cases
the contract lists, and the fake engine speaks the I4 / I5 shapes."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import jsonschema
import pytest

from app.policy import contracts
from app.policy.contracts import codec
from tests.policy_principal.conftest import FIXTURES, load_fixture
from tests.policy_principal.fakes import EphemeralSigner, FakeEngineDown, body_bytes, signed_headers


def _validator(name: str) -> jsonschema.Draft202012Validator:
    schema = contracts.load_schema(name)
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


def _valid(name: str, instance) -> None:
    errors = sorted(_validator(name).iter_errors(instance), key=str)
    assert not errors, f"{name}: {[e.message for e in errors]}"


@pytest.mark.parametrize("name", contracts.SCHEMAS)
def test_every_schema_is_valid_draft_2020_12(name):
    _validator(name)


def test_every_schema_file_is_listed():
    on_disk = {p.name.removesuffix(".schema.json")
               for p in Path(contracts.__file__).parent.glob("*.schema.json")}
    assert on_disk == set(contracts.SCHEMAS)


VALID_CASES = load_fixture("claims_valid.json")["cases"]
INVALID_CASES = load_fixture("claims_invalid.json")["cases"]


@pytest.mark.parametrize("case", VALID_CASES, ids=lambda c: c["name"])
def test_valid_claims_match_schema_and_verify(case, signer):
    spec = load_fixture("claims_valid.json")
    _valid("principal_token_claims", case["claims"])
    token = signer.token(case["claims"])
    header, _ = codec.decode_unverified(token)
    _valid("principal_token_header", header)
    verified = codec.verify_token(token, signer.pubkeys(), now=spec["now"], **spec["verify_as"])
    assert verified.claims == case["claims"]


def _patched(base: dict, patch: dict) -> dict:
    claims = copy.deepcopy(base)
    for key, value in patch.items():
        if value == "$delete":
            claims.pop(key, None)
        else:
            claims[key] = value
    return claims


@pytest.mark.parametrize("case", INVALID_CASES, ids=lambda c: c["name"])
def test_invalid_claims_rejected_with_contract_code(case, signer):
    spec = load_fixture("claims_invalid.json")
    base = next(c["claims"] for c in load_fixture("claims_valid.json")["cases"]
                if c["name"] == spec["base"])
    token = signer.token(_patched(base, case["patch"]))
    with pytest.raises(codec.PrincipalTokenError) as err:
        codec.verify_token(token, signer.pubkeys(), now=spec["now"], **spec["verify_as"])
    assert err.value.code == case["expect"]


def _good_token(signer: EphemeralSigner) -> tuple[str, dict]:
    spec = load_fixture("claims_valid.json")
    return signer.token(spec["cases"][0]["claims"]), spec


def test_signature_from_another_key_rejected(signer):
    token, spec = _good_token(signer)
    other = EphemeralSigner(kid=signer.kid)
    with pytest.raises(codec.PrincipalTokenError) as err:
        codec.verify_token(token, other.pubkeys(), now=spec["now"], **spec["verify_as"])
    assert err.value.code == "bad_signature"


def test_unknown_kid_rejected(signer):
    token, spec = _good_token(signer)
    with pytest.raises(codec.PrincipalTokenError) as err:
        codec.verify_token(token, {"another-kid": signer.public_key}, now=spec["now"],
                           **spec["verify_as"])
    assert err.value.code == "unknown_kid"


@pytest.mark.parametrize("mutate", ["claims", "header", "truncate", "segments", "garbage"])
def test_tampered_or_malformed_tokens_rejected(signer, mutate):
    token, spec = _good_token(signer)
    head, claims, sig = token.split(".")
    if mutate == "claims":
        payload = json.loads(codec.b64url_decode(claims))
        payload["p"]["role"] = "admin"
        token = ".".join((head, codec.b64url(json.dumps(payload).encode()), sig))
        expected = {"bad_signature"}
    elif mutate == "header":
        token = ".".join((codec.b64url(b'{"alg":"none","kid":"x","typ":"JWT"}'), claims, sig))
        expected = {"bad_header"}
    elif mutate == "truncate":
        token = token[:-10]
        expected = {"bad_signature"}
    elif mutate == "segments":
        token = f"{head}.{claims}"
        expected = {"malformed"}
    else:
        token = "!!!.???.***"
        expected = {"malformed"}
    with pytest.raises(codec.PrincipalTokenError) as err:
        codec.verify_token(token, signer.pubkeys(), now=spec["now"], **spec["verify_as"])
    assert err.value.code in expected


def test_public_key_roundtrips_through_bundle_encoding(signer):
    key = codec.load_public_key(signer.public_spki_b64)
    token, spec = _good_token(signer)
    codec.verify_token(token, {signer.kid: key}, now=spec["now"], **spec["verify_as"])


def test_gateway_bundle_pointer_fixture_matches_schema():
    pointer = load_fixture("gateway_bundle_pointer.json")
    _valid("gateway_bundle_pointer", pointer)
    assert pointer["key"] == contracts.BUNDLE_VERSION_KEY.format(
        gateway_id=pointer["gateway_id"], version=pointer["version"])


@pytest.mark.parametrize("case", VALID_CASES, ids=lambda c: c["name"])
def test_fixture_psids_are_derived_with_the_codec(case):
    claims = case["claims"]
    generation = int(claims["psid"].rsplit("-g", 1)[1])
    assert claims["psid"] == codec.psid_for(claims["ws"], claims["sid"], claims["tsv"], generation)


def test_gateway_bundle_fixture_matches_schema(signer):
    bundle = load_fixture("gateway_bundle.json")
    bundle["pubkeys"] = {signer.kid: signer.public_spki_b64}
    _valid("gateway_bundle", bundle)


@pytest.mark.parametrize("name", [
    "engine_decide_request", "engine_decide_response", "engine_respond_request",
    "engine_respond_response", "engine_list_filter_request", "engine_list_filter_response",
    "engine_healthz", "engine_error", "engine_control_request", "engine_control_response",
])
def test_engine_message_fixtures_match_schema(name):
    _valid(name, load_fixture("engine_messages.json")[name])


@pytest.mark.parametrize("name", ["audit_decision_line", "audit_engine_line"])
def test_audit_line_fixtures_match_schema(name):
    _valid(name, load_fixture("audit_lines.json")[name])


@pytest.mark.parametrize("name", list(load_fixture("jsonrpc_errors.json")))
def test_jsonrpc_error_fixtures_match_schema(name):
    _valid("jsonrpc_policy_error", load_fixture("jsonrpc_errors.json")[name])


def test_deny_text_is_globals_signature_and_parses():
    from app.services import policy_denials

    text = contracts.deny_message("Policy evaluation denied due to pol_payoutadm")
    assert text == load_fixture("jsonrpc_errors.json")["deny"]["error"]["message"]
    parsed = policy_denials.parse_tool_denial(text)
    assert parsed is not None


def test_runtime_frame_fixture_matches_schema():
    frame = load_fixture("runtime_frames.json")["gateway_policy_denied"]
    _valid("runtime_policy_denied_frame", frame)


def test_psid_session_key_and_shard_are_stable():
    psid = codec.psid_for("default", "a_b" + "0" * 40, 3, 1)
    assert codec.PSID_RE.match(psid) and psid.endswith("-v3-g1")
    assert psid != codec.psid_for("default", "a_b" + "0" * 40, 4, 1)  # T4: new set, new session
    key_alice = codec.session_key("default", psid, "user", "hr-intern")
    assert key_alice != codec.session_key("default", psid, "user", "demo-admin")  # identity-bound
    assert len(key_alice) == 64 and codec.shard_for(key_alice, 2) in (0, 1)
    assert codec.decision_id("jti", 1, "final") == codec.decision_id("jti", 1, "final")
    assert len(codec.decision_id("jti", 1, "final")) == 32


def test_fake_engine_speaks_i4_with_hmac(fake_engine, hmac_key):
    request = load_fixture("engine_messages.json")["engine_decide_request"]
    body = body_bytes(request)
    status, reply = fake_engine.handle("POST", "/v1/decide", body,
                                       signed_headers(hmac_key, "POST", "/v1/decide", body))
    assert status == 200
    _valid("engine_decide_response", reply)
    assert reply["event_seq"] == 1
    # replayed nonce and wrong key are both 401
    headers = signed_headers(hmac_key, "POST", "/v1/decide", body, nonce="fixed-nonce")
    assert fake_engine.handle("POST", "/v1/decide", body, headers)[0] == 200
    assert fake_engine.handle("POST", "/v1/decide", body, headers)[0] == 401
    bad = signed_headers(b"x" * 32, "POST", "/v1/decide", body)
    assert fake_engine.handle("POST", "/v1/decide", body, bad) == (401, {"code": "unauthorized"})
    # signature binds the path
    other_path = signed_headers(hmac_key, "POST", "/v1/respond", body)
    assert fake_engine.handle("POST", "/v1/decide", body, other_path)[0] == 401


def test_fake_engine_faults(fake_engine, hmac_key):
    body = body_bytes(load_fixture("engine_messages.json")["engine_decide_request"])
    fake_engine.force_error = {"status": 409, "code": "session_invalidated"}
    status, reply = fake_engine.handle("POST", "/v1/decide", body,
                                       signed_headers(hmac_key, "POST", "/v1/decide", body))
    assert status == 409
    _valid("engine_error", reply)
    fake_engine.down = True
    with pytest.raises(FakeEngineDown):
        fake_engine.handle("POST", "/v1/decide", body,
                           signed_headers(hmac_key, "POST", "/v1/decide", body))
    with pytest.raises(FakeEngineDown):
        fake_engine.control({"op": "status"})


def test_fake_engine_speaks_i5(fake_engine):
    install = load_fixture("engine_messages.json")["engine_control_request"]
    _valid("engine_control_response", fake_engine.control(install))
    status = fake_engine.control({"op": "status"})
    _valid("engine_control_response", status)
    assert status["shards"][0]["sets"] == {"default": "ps-17"}
    req = load_fixture("engine_messages.json")["engine_decide_request"]
    out = fake_engine.control({"op": "authorize_stateless", "ws": "default", "requests": [req]})
    assert out["decisions"][0]["event_seq"] is None  # stateless never records


def test_recorded_interceptor_events_have_the_p0_shape():
    events = sorted((FIXTURES / "interceptor_events").glob("*.json"))
    assert len(events) == 5
    for path in events:
        doc = json.loads(path.read_text(encoding="utf-8"))
        mcp = doc["event"]["mcp"]
        arn = mcp["gatewayRequest"]["context"]["identity"]["awsPrincipalArn"]
        assert arn.startswith("arn:aws-cn:iam::123456789012:")  # P0 a: field path, anonymised
        assert set(doc["client_context"]["custom"]) >= {"GATEWAY_ARN", "REQUEST_ID"}
        body = mcp["gatewayRequest"]["body"]
        if doc["phase"] == "REQUEST" and body.get("method") == "tools/call":
            assert contracts.META_KEY in mcp["gatewayRequest"]["body"]["params"]["_meta"]  # P0 b


def test_committed_fixtures_hold_no_secret_material():
    secretish = ("PRIVATE KEY", "BEGIN EC", "aws_secret", "AKIA", "IQoJ")
    for path in FIXTURES.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8")
            assert not any(s in text for s in secretish), path.name
            assert set(re.findall(r"\b\d{12}\b", text)) <= {"123456789012"}, path.name  # anonymised


def _eval_tokens_file(signer: EphemeralSigner, *, jti_tag: str = "a") -> dict:
    """The D5 fixture with ``__TOKEN__`` replaced by the job's one signed system token (I9 v2:
    ``sid`` = the job id, ``p`` = ``System::"eval"``)."""
    spec = load_fixture("eval_tokens.json")
    out = copy.deepcopy(spec["file"])
    claims = copy.deepcopy(spec["claims"])
    job_id = out["job_id"]
    claims.update(sid=job_id, psid=codec.psid_for(claims["ws"], job_id, claims["tsv"]),
                  jti=f"jti-eval-{jti_tag}-{job_id[-8:]}")
    out["token"] = signer.token(claims)
    return out


def test_eval_tokens_file_matches_schema_and_the_token_is_the_jobs_system_token(signer):
    spec = load_fixture("eval_tokens.json")
    tokens_file = _eval_tokens_file(signer)
    _valid("eval_tokens", tokens_file)
    assert contracts.EVAL_TOKENS_KEY.format(job_id=tokens_file["job_id"]) == (
        f"evaluations/{tokens_file['job_id']}/tokens.json")
    verified = codec.verify_token(tokens_file["token"], signer.pubkeys(), now=spec["now"],
                                  **spec["verify_as"])
    job_id = tokens_file["job_id"]
    assert verified.claims["sid"] == job_id  # one policy session per job
    assert verified.claims["psid"] == codec.psid_for("default", job_id, verified.claims["tsv"])
    assert verified.claims["ch"] in ("eval", "canary")
    assert verified.claims["p"] == {"kind": "system", "id": "eval", "label": "system:eval"}


def test_eval_tokens_refresh_keeps_the_policy_session(signer):
    """A refresh re-signs jti / iat / exp only: same sid, psid and principal, so the engine
    sees one policy session for the whole run."""
    first, second = _eval_tokens_file(signer), _eval_tokens_file(signer, jti_tag="b")
    _, a = codec.decode_unverified(first["token"])
    _, b = codec.decode_unverified(second["token"])
    assert a["jti"] != b["jti"]
    assert {k: a[k] for k in ("sid", "psid", "p", "tsv")} == {
        k: b[k] for k in ("sid", "psid", "p", "tsv")}


@pytest.mark.parametrize("case", load_fixture("eval_tokens.json")["invalid"],
                         ids=lambda c: c["name"])
def test_eval_tokens_file_rejects(case, signer):
    tokens_file = _patched(_eval_tokens_file(signer), case["patch"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(tokens_file, contracts.load_schema("eval_tokens"),
                            format_checker=jsonschema.FormatChecker())
