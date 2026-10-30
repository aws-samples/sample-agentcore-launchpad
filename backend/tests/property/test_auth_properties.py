"""E18 — password policy properties (round 16, channel A; users-and-ownership.md §9).

Random username / email / password × policy mode (`strict` / `relaxed`) × configured
minimum (6..16). The oracle below is written from the CONTRACT TEXT, not from the code:
`strict` = minimum length, not the username / email / email local part (case-insensitive,
stripped), not an embedded common password (matched lower-cased and with separators
dropped); `relaxed` = the minimum length only. `too_short` wins over every other reason.
Every property compares `validate_password` against the oracle AND against the advertised
`password_policy(settings)` payload the consoles pre-validate from.
"""

from __future__ import annotations

import re
import string
import uuid

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, assume, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from app.core.config import Settings, get_settings
from app.core.errors import AppError
from app.main import create_app
from app.services import users as users_service

# contract: docs/cn/interfaces/users-and-ownership.md §9 "Password policy"
#   strict  = auth_password_min_length (default 12), not the username / email / email local
#             part (case-insensitive), not in COMMON_PASSWORDS (matched after lower-casing
#             and stripping separators); relaxed = the minimum length only; floor 6;
#   detail.reason ∈ {too_short, matches_identity, common} + min_length; the consoles learn
#   the effective policy from GET /api/auth/status `password_policy: {mode, min_length}`.

pytestmark = pytest.mark.property

MODES = ("strict", "relaxed")
MIN_LENGTHS = st.integers(min_value=6, max_value=16)

# the same alphabets the product's validators accept
_USERNAME_HEAD = string.ascii_letters + string.digits
_USERNAME_TAIL = _USERNAME_HEAD + "._-"
_LOCAL = _USERNAME_HEAD + "._%+-"

usernames = st.builds(
    lambda head, tail: head + tail,
    st.text(_USERNAME_HEAD, min_size=1, max_size=1),
    st.text(_USERNAME_TAIL, min_size=2, max_size=20),
)
emails = st.builds(
    lambda local, host, tld: f"{local}@{host}.{tld}",
    st.text(_LOCAL, min_size=1, max_size=16),
    st.text(_USERNAME_HEAD, min_size=1, max_size=12),
    st.text(string.ascii_lowercase, min_size=2, max_size=6),
)
# printable ASCII + a little unicode + whitespace, so stripping / lower-casing matter
password_alphabet = st.characters(
    whitelist_categories=("Lu", "Ll", "Nd", "Po", "Ps", "Pe", "Sm", "Sc", "Zs"),
    max_codepoint=0x24F,
)
passwords = st.text(password_alphabet, min_size=0, max_size=40)
common = st.sampled_from(sorted(users_service.COMMON_PASSWORDS))


def _settings(mode: str, minimum: int) -> Settings:
    return Settings(auth_password_policy=mode, auth_password_min_length=minimum)


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def oracle(password: str, username: str, email: str, mode: str, minimum: int) -> str | None:
    """The §9 verdict: a reason, or None when the password is accepted. Order: §9 lists
    the length rule first and relaxed keeps only it, so `too_short` wins over the rest."""
    if len(password) < minimum:
        return "too_short"
    if mode == "relaxed":
        return None
    lowered = password.strip().lower()
    identity = {username.strip().lower(), email.strip().lower(),
                email.strip().lower().partition("@")[0]} - {""}
    if lowered in identity:
        return "matches_identity"
    # "matched after lower-casing and stripping separators" (§9) — on both sides
    if lowered in users_service.COMMON_PASSWORDS or _compact(password) in COMMON_COMPACT:
        return "common"
    return None


COMMON_COMPACT = frozenset(_compact(word) for word in users_service.COMMON_PASSWORDS)


def verdict(password: str, username: str, email: str, cfg: Settings) -> tuple[str | None, dict]:
    try:
        users_service.validate_password(password, username=username, email=email, settings=cfg)
    except AppError as exc:
        assert exc.code == "auth.weak_password"
        return exc.detail["reason"], exc.detail
    return None, {}


@h_settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(password=passwords, username=usernames, email=emails,
       mode=st.sampled_from(MODES), minimum=MIN_LENGTHS)
def test_verdict_matches_the_contract_and_the_advertised_policy(
    password, username, email, mode, minimum
):
    cfg = _settings(mode, minimum)
    advertised = users_service.password_policy(cfg)
    assert advertised == {"mode": mode, "min_length": minimum}
    reason, detail = verdict(password, username, email, cfg)
    assert reason == oracle(password, username, email, mode, minimum)
    if reason is not None:
        # every refusal names the advertised minimum, and nothing else
        assert detail == {"reason": reason, "min_length": advertised["min_length"]}
    # the console's client-side length pre-check agrees with the server
    assert (reason == "too_short") == (len(password) < advertised["min_length"])


@h_settings(max_examples=200, deadline=None)
@given(password=st.one_of(passwords, common, usernames, emails), username=usernames,
       email=emails, minimum=MIN_LENGTHS)
def test_relaxed_never_raises_anything_but_too_short(password, username, email, minimum):
    cfg = _settings("relaxed", minimum)
    reason, _ = verdict(password, username, email, cfg)
    assert reason in (None, "too_short")
    # relaxed is strictly weaker than strict: whatever strict accepts, relaxed accepts
    strict_reason, _ = verdict(password, username, email, _settings("strict", minimum))
    if strict_reason is None:
        assert reason is None


@h_settings(max_examples=200, deadline=None)
@given(username=usernames, email=emails, minimum=MIN_LENGTHS,
       variant=st.sampled_from(("username", "email", "local", "USERNAME", "EMAIL", "padded")))
def test_strict_refuses_the_identity_once_it_is_long_enough(username, email, minimum, variant):
    local = email.partition("@")[0]
    candidate = {
        "username": username, "email": email, "local": local,
        "USERNAME": username.upper(), "EMAIL": email.upper(), "padded": f"  {username}  ",
    }[variant]
    cfg = _settings("strict", minimum)
    reason, _ = verdict(candidate, username, email, cfg)
    if len(candidate) < minimum:
        assert reason == "too_short"  # length wins over identity
    else:
        assert reason == "matches_identity"


@h_settings(max_examples=200, deadline=None)
@given(word=common, minimum=MIN_LENGTHS, separator=st.sampled_from(("", "-", "_", " ", ".")),
       upper=st.booleans())
def test_strict_refuses_a_common_password_in_any_dress(word, minimum, separator, upper):
    dressed = separator.join(word) if separator else word
    if upper:
        dressed = dressed.upper()
    cfg = _settings("strict", minimum)
    reason, _ = verdict(dressed, "someone-else", "someone-else@acme-corp.com", cfg)
    if len(dressed) < minimum:
        assert reason == "too_short"
    else:
        assert reason == "common"


@h_settings(max_examples=200, deadline=None)
@given(username=usernames, email=emails, mode=st.sampled_from(MODES), minimum=MIN_LENGTHS)
def test_generated_passwords_always_pass_the_policy_they_were_generated_under(
    username, email, mode, minimum
):
    cfg = _settings(mode, minimum)
    generated = users_service.generate_password(settings=cfg)
    assert len(generated) == max(14, minimum)
    assert users_service.validate_password(
        generated, username=username, email=email, settings=cfg) == generated


@h_settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
@given(password=passwords, username=usernames, email=emails, mode=st.sampled_from(MODES),
       low=MIN_LENGTHS, high=MIN_LENGTHS)
def test_acceptance_is_monotone_in_the_configured_minimum(
    password, username, email, mode, low, high
):
    assume(low <= high)
    accepted_high, _ = verdict(password, username, email, _settings(mode, high))
    accepted_low, _ = verdict(password, username, email, _settings(mode, low))
    if accepted_high is None:
        assert accepted_low is None  # a stricter minimum passed → a looser one passes
    if accepted_low == "too_short":
        assert accepted_high == "too_short"


# ── the API differential: the registration verdict vs the advertised hint ────────
#
# §9: "The consoles learn the effective policy from the server … the registration form's hint
# and its client-side length pre-check read it, so no console copy hard-codes a rule the backend
# will not enforce." The property: for a random password, `POST /api/auth/register` refuses
# with `too_short` exactly when the password is shorter than the `min_length` the SAME server
# advertised on `GET /api/auth/status`, and in relaxed mode no other reason exists.


@pytest.fixture(params=[("strict", 12), ("relaxed", 8), ("relaxed", 6)],
                ids=lambda p: f"{p[0]}-{p[1]}")
def policy_app(request, monkeypatch):
    mode, minimum = request.param
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", "operator")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", "s3cret-pass")
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD_POLICY", mode)
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD_MIN_LENGTH", str(minimum))
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTER_IP_LIMIT", "0")  # §9: `0` disables the window
    # instant activation: under approval mode the §9 pending cap (50) would answer 409
    # `auth.registration_backlog` once enough generated passwords were accepted
    monkeypatch.setenv("LAUNCHPAD_AUTH_REGISTRATION_REQUIRE_APPROVAL", "false")
    monkeypatch.setattr(users_service, "_ITERATIONS", 1000)
    get_settings.cache_clear()
    client = TestClient(create_app(), client=("10.4.4.4", 1))
    yield client
    client.close()
    get_settings.cache_clear()


@h_settings(max_examples=60, deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(password=st.one_of(passwords.filter(bool), common,
                          st.text(_USERNAME_TAIL, min_size=5, max_size=14)),
       variant=st.sampled_from(("random", "username", "email")))
def test_api_registration_verdict_matches_the_advertised_policy(policy_app, password, variant):
    advertised = policy_app.get("/api/auth/status").json()["password_policy"]
    username = f"qa-{uuid.uuid4().hex[:10]}"
    email = f"{username}@acme-corp.com"
    candidate = {"random": password, "username": username, "email": email}[variant]
    res = policy_app.post("/api/auth/register",
                          json={"username": username, "email": email, "password": candidate})
    expected = oracle(candidate, username, email, advertised["mode"], advertised["min_length"])
    if expected is None:
        assert res.status_code == 201, (res.status_code, res.json().get("code"))
    else:
        assert res.status_code == 400, (res.status_code, res.text)
        assert res.json()["code"] == "auth.weak_password"
        assert res.json()["detail"] == {"reason": expected, "min_length": advertised["min_length"]}
    # the client-side pre-check (length only) agrees with the server
    assert (res.status_code == 400 and res.json()["detail"]["reason"] == "too_short") == (
        len(candidate) < advertised["min_length"])


@pytest.mark.xfail(
    strict=True,
    reason="N16-obs26: an EMPTY password answers the generic 422 validation.invalid_request "
           "(RegisterRequest.password min_length=1) instead of the policy's 400 auth.weak_password "
           "{reason: too_short, min_length}, so the console cannot word it from the hint",
)
def test_an_empty_password_is_refused_as_too_short_by_the_policy(policy_app):
    # shrunk from test_api_registration_verdict_matches_the_advertised_policy (password="")
    advertised = policy_app.get("/api/auth/status").json()["password_policy"]
    username = f"qa-{uuid.uuid4().hex[:10]}"
    res = policy_app.post("/api/auth/register", json={
        "username": username, "email": f"{username}@acme-corp.com", "password": ""})
    assert res.status_code == 400, (res.status_code, res.text)
    assert res.json()["code"] == "auth.weak_password"
    assert res.json()["detail"] == {"reason": "too_short", "min_length": advertised["min_length"]}


def test_the_policy_floor_is_six_and_pydantic_refuses_less():
    assert users_service.MIN_PASSWORD_LENGTH == 6
    with pytest.raises(ValueError):
        Settings(auth_password_min_length=5)
    assert users_service.effective_min_length(Settings(auth_password_min_length=6)) == 6
