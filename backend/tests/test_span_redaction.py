"""N17-obs307 — the shared credential mask for span / log-record attributes."""

import pytest

from app.services.strands_eval.redaction import (
    REDACTED,
    is_sensitive_key,
    redact_attributes,
    redact_text,
    redact_value,
)

STS_ID = "ASIAEXAMPLE00000TEST"
IAM_ID = "AKIAIOSFODNN7EXAMPLE"


@pytest.mark.parametrize("key", [
    "aws.auth.account.access_key",
    "aws.remote.resource.account.access_key",
    "AWS_ACCESS_KEY_ID",
    "accessKeyId",
    "aws_secret_access_key",
    "client_secret",
    "aws.session_token",
    "X-Amz-Security-Token",
    "http.request.header.authorization",
    "Authorization",
    "db.password",
    "PASSWORD",
])
def test_credential_like_keys_are_sensitive(key):
    assert is_sensitive_key(key)


@pytest.mark.parametrize("key", [
    "session.id", "aws.auth.region", "gen_ai.usage.input_tokens", "rpc.service",
    "http.url", "aws.request_id", "gen_ai.tool.name", "token_count",
])
def test_ordinary_keys_are_not(key):
    assert not is_sensitive_key(key)


def test_non_string_keys_are_not_sensitive():
    assert not is_sensitive_key(42)
    assert not is_sensitive_key(None)


def test_masks_the_value_and_keeps_the_key():
    attrs = {"aws.auth.account.access_key": STS_ID, "aws.auth.region": "cn-north-1",
             "session.id": "s1", "gen_ai.usage.input_tokens": 12}
    out = redact_attributes(attrs)
    assert out == {"aws.auth.account.access_key": REDACTED, "aws.auth.region": "cn-north-1",
                   "session.id": "s1", "gen_ai.usage.input_tokens": 12}
    assert attrs["aws.auth.account.access_key"] == STS_ID  # input not mutated


def test_a_sensitive_key_masks_any_value_type():
    out = redact_attributes({"password": 1234, "authorization": ["Bearer x"],
                             "client_secret": {"v": "y"}, "session_token": None})
    assert set(out.values()) == {REDACTED}


def test_key_ids_inside_values_are_masked_in_place():
    assert redact_text(f"cred={STS_ID}/20261023 and {IAM_ID}.") == (
        f"cred={REDACTED}/20261023 and {REDACTED}.")
    assert redact_attributes({"http.url": f"https://x?X-Amz-Credential={STS_ID}"}) == {
        "http.url": f"https://x?X-Amz-Credential={REDACTED}"}


def test_lookalikes_are_left_alone():
    # lower case, too short, other prefixes: not an access-key id
    for text in ("asiaexample00000test", "ASIA123", "ABIAEXAMPLE00000TEST", "plain text"):
        assert redact_text(text) == text


def test_nested_records_are_masked_at_every_level():
    record = {
        "traceId": "t", "attributes": {"aws.auth.account.access_key": STS_ID},
        "resource": {"attributes": {"service.name": "svc", "env": [f"k={IAM_ID}"]}},
        "body": {"input": {"messages": [{"content": f"echo {STS_ID}"}]}},
    }
    out = redact_value(record)
    assert out["attributes"]["aws.auth.account.access_key"] == REDACTED
    assert out["resource"]["attributes"] == {"service.name": "svc", "env": [f"k={REDACTED}"]}
    assert out["body"]["input"]["messages"][0]["content"] == f"echo {REDACTED}"
    assert out["traceId"] == "t"


def test_scalars_pass_through():
    for value in (None, 3, 2.5, True):
        assert redact_value(value) == value
