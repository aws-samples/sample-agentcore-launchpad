# ruff: noqa: F811  — pytest fixtures are imported by name from tests.evaluation.test_scorecard
"""E10 — evaluators × model roles × model enabled/disabled (round 16, channel A).

* **The role matrix** (`services/model_roles.py` × `services/model_registry.update_model`):
  Hypothesis assigns a random subset of the Settings-backed roles to four catalog rows — an admin-
  registered tool-capable chat model, one without tool use, one disabled, and the built-in
  Kimi row — then asks: does each `PUT /api/settings/model-roles/{role}` answer as the role's
  rules say (empty / disabled / no tool use / ok), and does disabling each model answer 409
  `model.in_use` listing EXACTLY the roles whose effective value is that model (the Kimi row
  carries the built-in Skill Lab target), 200 otherwise? Reset, everything is allowed again.
* **The judge description boundary**: 200 characters of any Unicode is accepted (create
  and update), 201 is a 422, for both routes.
Contract sources: `docs/cn/interfaces/model-registry.md` §12 (a role accepts only an enabled,
tool-capable-where-needed catalog chat row — 422 `models.model_not_allowed {reason}`; the
referrers see the effective role values, 409 `model.in_use` on delete AND disable), §9 / plan
§5.5 (a disabled row: stored specs keep parsing, new picks are refused);
`evaluation-engine.md` l.1276 ("`judge.description` ≤ 200 characters"; the AWS
EvaluatorDescription bound).
* **A judge on a disabled model**: the registry lets an administrator disable a model that
  custom judges still name (D21 §5.5: stored definitions keep parsing, new picks are
  refused). Is a RUN that picks such a judge a new pick? (N16-obs17.)
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from app.services import model_roles
from tests.evaluation.test_scorecard import _agent, queued  # noqa: F401 — fixture by name

SCALE = float(os.environ.get("LAUNCHPAD_PROPERTY_SCALE", "1"))
ROLES_PREFIX = "/api/settings/model-roles"
PROVIDERS = "/api/settings/model-providers"
GLM = "global.zai.glm-5.3"
KIMI = "global.moonshotai.kimi-k3"
ACME = {"tools": ("acme-org/Tools-7B", True, True), "notools": ("acme-org/Chat-7B", False, True),
        "disabled": ("acme-org/Off-7B", True, False)}
ROLE_NAMES = [r.name for r in model_roles.ROLES]


def _ensure_acme(client: TestClient) -> dict[str, dict[str, Any]]:
    """The three admin-registered rows (idempotent across Hypothesis examples)."""
    created = client.post(PROVIDERS, json={
        "name": "acme", "kind": "openai_compatible", "label": "Acme",
        "base_url": "https://api.siliconflow.cn/acme/v1"})
    assert created.status_code in (201, 409), created.text
    registry = client.get(PROVIDERS).json()
    provider_id = next(p["id"] for p in registry["providers"] if p["name"] == "acme")
    rows = {m["api_model_name"]: m for m in registry["models"] if m["provider_id"] == provider_id}
    out: dict[str, dict[str, Any]] = {}
    for key, (name, tools, enabled) in ACME.items():
        if name not in rows:
            res = client.post(f"{PROVIDERS}/{provider_id}/models", json={
                "api_model_name": name, "kind": "chat", "probe": False, "enabled": enabled,
                "overrides": {"tools": tools}})
            assert res.status_code == 201, res.text
            rows[name] = res.json()
        out[key] = rows[name]
    kimi = next(m for m in registry["models"] if m["model_id"] == KIMI)
    out["kimi"] = kimi
    return out


def _ref(row: dict[str, Any]) -> str:
    return f"{PROVIDERS}/{row['provider_id']}/models/{row['id']}"


def _reset_roles(client: TestClient) -> None:
    for role in ROLE_NAMES:
        assert client.delete(f"{ROLES_PREFIX}/{role}").status_code == 200


def _expected_put(role: model_roles.Role, choice: str, rows: dict[str, dict[str, Any]]
                  ) -> tuple[int, str | None]:
    if choice == "":
        return (200, None) if role.allow_empty else (422, "empty")
    if choice == "disabled":
        return 422, "disabled"
    if choice == "notools" and role.needs_tools:
        return 422, "no_tool_use"
    return 200, None


@settings(max_examples=max(5, int(10 * SCALE)), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(st.dictionaries(st.sampled_from(ROLE_NAMES),
                       st.sampled_from(["tools", "notools", "disabled", "kimi", ""]),
                       max_size=len(ROLE_NAMES)))
@example({role: "tools" for role in ROLE_NAMES})
@example({role: "kimi" for role in ROLE_NAMES})
def test_disabling_a_model_is_409_with_exactly_the_roles_that_point_at_it(client, assignment):
    rows = _ensure_acme(client)
    _reset_roles(client)
    # rev-r25 F3: the built-in defaults on the Kimi row (Skill Lab target, Studio coding
    # agent) belong to HIDDEN features — never referrers, so they never count here; a VISIBLE
    # role whose default is one of the rows (N17-obs449: failure_detection → Kimi) does
    defaults = {r["role"]: r["model_id"] for r in client.get("/api/model-defaults").json()[
        "roles"] if r["role"] != "embedding"
        and not model_roles.role_hidden(model_roles.role(r["role"]))}
    pointing: dict[str, set[str]] = {
        key: {name for name, model in defaults.items() if model == row["model_id"]}
        for key, row in rows.items()}
    try:
        for role_name, choice in assignment.items():
            role = model_roles.role(role_name)
            model_id = "" if choice == "" else rows[choice]["model_id"]
            res = client.put(f"{ROLES_PREFIX}/{role_name}", json={"model_id": model_id})
            status, reason = _expected_put(role, choice, rows)
            assert res.status_code == status, (role_name, choice, res.text)
            if status == 200:
                assert res.json()["model_id"] == model_id
                for key in pointing:
                    pointing[key].discard(role_name)
                if choice and not model_roles.role_hidden(role):  # hidden: never a referrer
                    pointing[choice].add(role_name)
            else:
                body = res.json()
                assert body["code"] == "models.model_not_allowed", body
                assert body["detail"]["reason"] == reason, (role_name, choice, body)
        effective = {r["role"]: r["model_id"] for r in client.get("/api/model-defaults").json()[
            "roles"] if not model_roles.role_hidden(model_roles.role(r["role"]))
            and r["role"] != "embedding"}
        for key, row in rows.items():
            assert {r for r, m in effective.items() if m == row["model_id"]} == pointing[key], key
            res = client.put(_ref(row), json={"enabled": False})
            if key == "disabled":
                assert res.status_code == 200, res.text  # already off: nothing to guard
                continue
            if pointing[key]:
                assert res.status_code == 409, (key, res.text)
                body = res.json()
                assert body["code"] == "model.in_use" and body["detail"]["suggest"] == "repoint"
                roles = {r["role"] for r in body["detail"]["referrers"] if r["kind"] == "setting"}
                assert roles == pointing[key], (key, roles, pointing[key])
                assert all(r["model_ids"] == [row["model_id"]] for r in body["detail"][
                    "referrers"])
            else:
                assert res.status_code == 200, (key, res.text)
                assert client.put(_ref(row), json={"enabled": True}).status_code == 200
    finally:
        _reset_roles(client)
        for key, row in rows.items():
            client.put(_ref(row), json={"enabled": key != "disabled"})


def test_the_default_judge_model_cannot_be_disabled_while_every_role_points_at_it(client):
    """GLM 5.3 is every role's default and the default chat row: the 409 is the default's
    (`model.is_default`), checked before the role referrers."""
    res = client.put(f"{PROVIDERS}/bedrock/models/{GLM}", json={"enabled": False})
    assert res.status_code == 409 and res.json()["code"] == "model.is_default", res.text


# ─── the judge description boundary ──────────────────────────────────────────


description_chars = st.characters(exclude_categories=("Cs", "Cc"))


def _judge(client: TestClient, name: str, **overrides: Any):
    body = {"name": name, "instructions": "Rate the tone of {context}", "level": "SESSION"}
    body.update(overrides)
    return client.post("/api/eval/evaluators", json=body)


@settings(max_examples=max(5, int(20 * SCALE)), deadline=None, database=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(st.text(alphabet=description_chars, min_size=200, max_size=200), st.integers(0, 10 ** 6))
@example("描" * 200, 1)
@example("x" * 200, 2)
def test_a_judge_description_of_200_chars_is_accepted_and_201_refused(client, text, salt):
    assert len(text) == 200
    name = f"judge_{salt}"
    ok = _judge(client, name, description=text)
    assert ok.status_code == 201, ok.text
    evaluator_id = ok.json()["evaluator_id"]
    try:
        too_long = _judge(client, f"{name}_long", description=text + "x")
        assert too_long.status_code == 422, too_long.text
        assert too_long.json()["code"] == "validation.invalid_request"
        row = client.get(f"/api/eval/evaluators/{evaluator_id}").json()
        assert row["description"] == text
        upd = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
            "instructions": "Rate the tone of {context}", "description": text})
        assert upd.status_code == 200, upd.text
        upd = client.put(f"/api/eval/evaluators/{evaluator_id}", json={
            "instructions": "Rate the tone of {context}", "description": text + "y"})
        assert upd.status_code == 422, upd.text
        assert client.get(f"/api/eval/evaluators/{evaluator_id}").json()["description"] == text
    finally:
        client.delete(f"/api/eval/evaluators/{evaluator_id}")


# ─── a judge whose model was disabled afterwards ─────────────────────────────


@pytest.mark.xfail(strict=True, reason="N16-obs17: a run may still pick a custom judge whose "
                                      "model an administrator disabled after the judge was "
                                      "defined — the refusal only lands inside the worker")
def test_a_run_refuses_a_judge_whose_model_was_disabled_since(client, queued):
    rows = _ensure_acme(client)
    tools = rows["tools"]
    judge = _judge(client, "judge_on_acme", model_id=tools["model_id"],
                   description="runs on acme")
    assert judge.status_code == 201, judge.text
    evaluator_id = judge.json()["evaluator_id"]
    try:
        # D21 §5.5: evaluators naming the model do not block the disable
        assert client.put(_ref(tools), json={"enabled": False}).status_code == 200
        # …but the next NEW pick of the judge is a run that would call the disabled model
        agent = _agent()
        res = client.post("/api/eval/runs", json={
            "agent_id": agent, "lookback_hours": 24, "evaluators": [evaluator_id],
            "wait_seconds": 0})
        assert res.status_code == 422, res.text
        assert res.json()["code"].startswith("model.")
    finally:
        client.put(_ref(tools), json={"enabled": True})
        client.delete(f"/api/eval/evaluators/{evaluator_id}?force=true")
