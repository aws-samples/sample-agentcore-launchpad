"""The ONLY differences the CN evaluation/optimization HTTP contract may have from the
Global baseline ``4e27b1f4`` (plan §A.5, contract amendment CA-1 — accepted by the lead
2026-10-10).

Each entry is ``(json_pointer, op, value)`` applied to the *baseline* OpenAPI slice
(``openapi_baseline_eval.json``) before it is compared with the CN app's slice:

* ``ALLOWED_DELTA`` — permanent (CA-1): the managed-Harness canary fields are gone, the
  recommendation providers are the two self-built ones, judge defaults are the CN catalog.
* ``PENDING_DELTA`` — temporary: routes another W-E group restores later. **The owning
  group deletes its entries in the same commit that restores the route** — a stale entry
  makes ``apply_delta`` raise, so the snapshot test fails loudly rather than hiding the
  route's return.

Nothing else may differ. Adding an entry here is a contract decision for the lead.
"""

from __future__ import annotations

import copy
from typing import Any

Delta = tuple[str, str, Any]

CN_JUDGE_DEFAULT = "global.zai.glm-5.3"
CN_PROVIDERS = ["gepa_lite", "harness_optimizer"]

_CANARY = "/components/schemas/RuntimeCanaryCreate/properties"

ALLOWED_DELTA: list[Delta] = [
    # CA-1a — the managed-Harness canary (two existing Harness versions, early 50/50
    # start) does not exist in CN; Runtime canaries keep CandidateEdit.
    (f"{_CANARY}/harness_versions", "remove", None),
    (f"{_CANARY}/start_stage", "remove", None),
    ("/components/schemas/HarnessVersions", "remove", None),
    # CA-1b — recommendation providers: the AgentCore job is gone, the Strands
    # harness-optimizer provider arrives (W-E2-5); gepa_lite stays and becomes the default.
    ("/components/schemas/RecommendationCreate/properties/provider/anyOf/0/enum",
     "replace", CN_PROVIDERS),
    # CA-1c — judge model defaults follow the CN catalog (spec C3).
    ("/components/schemas/JudgeCreate/properties/model_id/default", "replace", CN_JUDGE_DEFAULT),
    ("/components/schemas/JudgeUpdate/properties/model_id/default", "replace", CN_JUDGE_DEFAULT),
]

_PLAN = "/paths/~1api~1assistant~1architect~1conversations~1{conversation_id}~1evaluation-plan"

# CA-5 (W-E5, lead ruling 2026-10-10: "slimmed to local datasets + local evaluators +
# optional Lambda code evaluator") — the SE-049 reviewed recovery of a Lambda RevisionId
# race against the AgentCore evaluator binding has no CN twin (evaluators are ledger
# rows; the console never calls the route): removed, not flagged.
ALLOWED_DELTA += [
    (f"{_PLAN}~1operations~1{{operation_id}}~1lambda-revision-review", "remove", None),
    ("/components/schemas/LambdaRevisionReview", "remove", None),
]

# N-SCN-obs14 (r7-l, lead-directed 2026-10-12): a directional verdict needs
# `Settings.verdict_min_arm_n` scored records in each arm; `traffic` may carry
# `sessions_per_arm` to cycle the replay dataset (≤ 3 ×) until every arm holds that many
# sessions. Additive: one optional integer (1…500) on the canary action body, default null.
_CANARY_ACTION = "/components/schemas/RuntimeCanaryAction/properties"
ALLOWED_DELTA += [
    (f"{_CANARY_ACTION}/sessions_per_arm", "add", {
        "anyOf": [{"type": "integer", "minimum": 1.0, "maximum": 500.0}, {"type": "null"}],
        "title": "Sessions Per Arm",
    }),
]

# N-SCN-obs10 round-6 (r8-a, lead-directed 2026-10-14): `from-sessions` becomes a job above
# `FROM_SESSIONS_SYNC_MAX` sessions (or on `?sync=false`) — 202 `{job_id, status, sessions,
# poll}` polled on `GET /api/jobs/{job_id}`; `?sync=true` keeps the 201 shape (400 above the
# cap). Additive: one optional query parameter + one extra response code on the same route.
_FROM_SESSIONS = "/paths/~1api~1eval~1datasets~1from-sessions/post"
ALLOWED_DELTA += [
    (f"{_FROM_SESSIONS}/parameters", "add", [{
        "in": "query", "name": "sync", "required": False,
        "schema": {"anyOf": [{"type": "boolean"}, {"type": "null"}], "title": "Sync"},
    }]),
    (f"{_FROM_SESSIONS}/responses/202", "add", {
        "content": {"application/json": {"schema": {"additionalProperties": True,
                                                    "type": "object"}}},
    }),
]

# N12-obs46 (r11-g, lead-directed 2026-10-16 — upstream b5eb3dc8 parity): a canary may choose
# the evaluators BOTH arms are scored with (`online_evaluators`, validated like an online
# evaluation config + a numeric rating scale; absent → the default pair) and a
# `primary_evaluator` among them whose scores alone decide the verdict (absent → the aggregate
# verdict). Additive: two optional fields on the canary create body, default null. The
# baseline 4e27b1f4 predates the upstream feature, so the Global schema gains them too.
ALLOWED_DELTA += [
    (f"{_CANARY}/online_evaluators", "add", {
        "anyOf": [{"items": {"type": "string"}, "type": "array", "maxItems": 50},
                  {"type": "null"}],
        "title": "Online Evaluators",
    }),
    (f"{_CANARY}/primary_evaluator", "add", {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "title": "Primary Evaluator",
    }),
]

# N13-obs2 (r13-b, user decision 2026-10-17 — "delete the configuration-experiment residue;
# canaries stay"): the configuration-bundle experiment is retired. Its five routes and their
# two request schemas are gone (CA-1b's `recommend_provider` and N-SCN-obs5's `force` entries
# on `ActionRequest` went with the schema); the canary create body loses the experiment →
# canary promote hand-off `source_experiment_id`; the recommendation-provider discovery the
# run-recommendation picker still needs moves to `GET /api/eval/recommendation-providers`
# (same response). Upstream keeps the experiment (docs/cn/reports/upstream-parity.md).
_EXPERIMENTS = "/paths/~1api~1experiments"
ALLOWED_DELTA += [
    (_EXPERIMENTS, "remove", None),
    (f"{_EXPERIMENTS}~1providers", "remove", None),
    (f"{_EXPERIMENTS}~1readiness", "remove", None),
    (f"{_EXPERIMENTS}~1{{exp_id}}", "remove", None),
    (f"{_EXPERIMENTS}~1{{exp_id}}~1action", "remove", None),
    ("/components/schemas/ExperimentCreate", "remove", None),
    ("/components/schemas/app__optimization__routers__ActionRequest", "remove", None),
    (f"{_CANARY}/source_experiment_id", "remove", None),
    ("/paths/~1api~1eval~1recommendation-providers", "add", {"get": {
        "operationId": "recommendation_providers_api_eval_recommendation_providers_get",
        "responses": {"200": {"content": {"application/json": {"schema": {
            "additionalProperties": True,
            "title": "Response Recommendation Providers Api Eval Recommendation Providers Get",
            "type": "object"}}}}},
        "summary": "Recommendation Providers",
        "tags": ["evaluation"],
    }}),
]

# N13-obs16 (r13-g, lead-directed 2026-10-18 — referential delete guards, acceptance E09 NC-11 /
# E10 NC-08): deleting a dataset / cloud dataset / custom evaluator still referenced by an
# active run, a pipeline, an online config or a running canary answers 409 `<kind>.in_use`;
# an administrator may override with `?force=true`. Additive: one optional boolean query
# parameter on the three DELETE routes (the arrays are replaced whole — `apply_delta` cannot
# append to a list — with the baseline's path parameter kept verbatim).
_FORCE_PARAM = {
    "in": "query", "name": "force", "required": False,
    "schema": {"default": False, "title": "Force", "type": "boolean"},
}


def _path_param(name: str, title: str) -> dict[str, Any]:
    return {"in": "path", "name": name, "required": True,
            "schema": {"title": title, "type": "string"}}


ALLOWED_DELTA += [
    ("/paths/~1api~1eval~1datasets~1{dataset_id}/delete/parameters", "replace",
     [_path_param("dataset_id", "Dataset Id"), _FORCE_PARAM]),
    ("/paths/~1api~1eval~1datasets~1cloud~1{cloud_id}/delete/parameters", "replace",
     [_path_param("cloud_id", "Cloud Id"), _FORCE_PARAM]),
    ("/paths/~1api~1eval~1evaluators~1{evaluator_id}/delete/parameters", "replace",
     [_path_param("evaluator_id", "Evaluator Id"), _FORCE_PARAM]),
]

# N13-obs21 (r13-h, lead ruling 2026-10-18 — E12 NC-05): `PATCH /api/eval/online/{id}` refuses
# unknown / protected keys (`agent_id`, `mode`, …) with 422 `validation.invalid_request` instead of
# silently ignoring them (`OnlineConfigPatch` is `extra="forbid"`). Schema-only: the properties
# are unchanged, the object gains `additionalProperties: false`.
ALLOWED_DELTA += [
    ("/components/schemas/OnlineConfigPatch/additionalProperties", "add", False),
]

# N15-obs1 (r15-a, lead-directed 2026-10-19 — upstream eeddc368 / PR #289 parity, "evaluation
# scorecards"): three additive eval routes. `GET /api/eval/scorecard?agent_id=` (ledger-only
# agent scorecards), `GET /api/eval/runs/compare?a=&b=` (two finished evaluator runs side by
# side) and `POST /api/eval/runs/{run_id}/rerun` (the run's scope again, through the shared
# `_start_run` validation — CN: without upstream's budget gate) + its `RunRerun` body. Every run
# payload also gains `snapshot {seq, source}` (an untyped `dict[str, Any]` field: no schema
# change). The baseline 4e27b1f4 predates the upstream feature.
_OBJ_200 = {"content": {"application/json": {"schema": {"additionalProperties": True,
                                                        "type": "object"}}}}
_HTTP_422 = {"content": {"application/json": {"schema": {
    "$ref": "#/components/schemas/HTTPValidationError"}}}}
ALLOWED_DELTA += [
    ("/paths/~1api~1eval~1scorecard", "add", {"get": {
        "operationId": "get_scorecard_api_eval_scorecard_get",
        "parameters": [{
            "in": "query", "name": "agent_id", "required": False,
            "schema": {"anyOf": [{"maxLength": 32, "minLength": 1, "type": "string"},
                                 {"type": "null"}], "title": "Agent Id"},
        }],
        "responses": {
            "200": {**_OBJ_200, "content": {"application/json": {"schema": {
                **_OBJ_200["content"]["application/json"]["schema"],
                "title": "Response Get Scorecard Api Eval Scorecard Get"}}}},
            "422": _HTTP_422,
        },
        "summary": "Get Scorecard",
        "tags": ["evaluation"],
    }}),
    ("/paths/~1api~1eval~1runs~1compare", "add", {"get": {
        "operationId": "compare_runs_api_eval_runs_compare_get",
        "parameters": [
            {"in": "query", "name": "a", "required": True,
             "schema": {"maxLength": 16, "minLength": 1, "title": "A", "type": "string"}},
            {"in": "query", "name": "b", "required": True,
             "schema": {"maxLength": 16, "minLength": 1, "title": "B", "type": "string"}},
        ],
        "responses": {
            "200": {"content": {"application/json": {"schema": {
                "additionalProperties": True,
                "title": "Response Compare Runs Api Eval Runs Compare Get",
                "type": "object"}}}},
            "422": _HTTP_422,
        },
        "summary": "Compare Runs",
        "tags": ["evaluation"],
    }}),
    ("/paths/~1api~1eval~1runs~1{run_id}~1rerun", "add", {"post": {
        "operationId": "rerun_run_api_eval_runs__run_id__rerun_post",
        "parameters": [{"in": "path", "name": "run_id", "required": True,
                        "schema": {"title": "Run Id", "type": "string"}}],
        "requestBody": {"content": {"application/json": {"schema": {
            "anyOf": [{"$ref": "#/components/schemas/RunRerun"}, {"type": "null"}],
            "title": "Body"}}}},
        "responses": {
            "201": {"content": {"application/json": {"schema": {
                "additionalProperties": True,
                "title": "Response Rerun Run Api Eval Runs  Run Id  Rerun Post",
                "type": "object"}}}},
            "422": _HTTP_422,
        },
        "summary": "Rerun Run",
        "tags": ["evaluation"],
    }}),
    ("/components/schemas/RunRerun", "add", {
        "properties": {"name": {"anyOf": [{"maxLength": 64, "minLength": 1, "type": "string"},
                                          {"type": "null"}], "title": "Name"}},
        "title": "RunRerun", "type": "object",
    }),
]

# upstream #239 (719a8e43, cn/r15-b parity — N15-obs5): `POST /api/eval/pipelines/{id}/run`
# answers **202** with the row as `running` (the bounded run proceeds on a daemon thread with
# its own DB session) instead of 200 after a synchronous run. Same response schema under the
# new code. The baseline 4e27b1f4 predates the upstream change, so the Global schema gains it
# too; this is upstream's own contract, not a CN deviation.
_PIPELINE_RUN = "/paths/~1api~1eval~1pipelines~1{pipeline_id}~1run/post/responses"
ALLOWED_DELTA += [
    (f"{_PIPELINE_RUN}/200", "remove", None),
    (f"{_PIPELINE_RUN}/202", "add", {
        "content": {"application/json": {"schema": {
            "additionalProperties": True,
            "title": "Response Run Pipeline Api Eval Pipelines  Pipeline Id  Run Post",
            "type": "object"}}},
    }),
]

# N15-obs8 (r15-d, lead-directed 2026-10-19 — isolation UI V8): every run payload (`GET
# /api/eval/runs`, `GET /api/eval/runs/{id}`, create / stop / recheck) gains the additive
# `hidden: {agent: bool, dataset: bool}`; a referenced dataset / agent the viewer cannot see
# keeps its id and answers `dataset_name` / `agent_name` null with `hidden.<ref>` true. The
# baseline describes those responses as `{"type": "object", "additionalProperties": true}`
# (no property list), so the field changes nothing in the frozen slice and has NO delta entry
# here — a pointer into a property list that does not exist would make `apply_delta` raise.
# The key-set pin lives in `test_eval_response_shapes.RUN_OUT`.

# N17-obs350 / P3.2 (r17-bh, lead-directed 2026-10-23 eval-accuracy Round 1 — "make the judge
# model selectable per run, defaulting to the role value"): `RunCreate` gains the optional
# `judge_model_id` (the built-in judge of the run; omitted = the `judge` model role at
# submit). The run payload's additive `judge_model_id` / `judge_inference` need no entry
# (untyped responses, see N15-obs8 above); their pin is `test_eval_response_shapes.RUN_OUT`.
ALLOWED_DELTA.append(
    ("/components/schemas/RunCreate/properties/judge_model_id", "add", {
        "anyOf": [{"maxLength": 120, "minLength": 1, "type": "string"}, {"type": "null"}],
        "title": "Judge Model Id"}),
)

# N17-obs337 / audit item 13 (r17-bi, lead-directed 2026-10-23 eval-accuracy Round 2 — "the
# online BYOC data source, log_source {service_name, log_group_names[1..10]}"): Global's online
# data source is `cloudWatchLogs {logGroupNames, serviceNames}`, so `OnlineConfigCreate` takes
# exactly one of `agent_id` or the batch run's `LogSource` — `agent_id` becomes optional
# (no longer `required`) and `log_source` is added. The config read-back is untyped (no entry).
_ONLINE_CREATE = "/components/schemas/OnlineConfigCreate"
ALLOWED_DELTA += [
    (f"{_ONLINE_CREATE}/properties/agent_id", "replace", {
        "anyOf": [{"type": "string"}, {"type": "null"}], "title": "Agent Id"}),
    (f"{_ONLINE_CREATE}/properties/log_source", "add", {
        "anyOf": [{"$ref": "#/components/schemas/LogSource"}, {"type": "null"}]}),
    (f"{_ONLINE_CREATE}/required", "remove", None),
]

# N17-obs407 (r17-bj, lead-directed 2026-10-24 eval-accuracy Round 3 D3-a — "expected-trajectory
# tool names must match the agent's real tool names; validate at dataset create/edit when an agent
# is linked"): the dataset bodies gain the optional `agent_id` link (`PUT` unlinks with ""). The
# dataset / run payloads' additive `agent_id` / `tool_name_rewrites` are untyped (no entry); their
# pins are `test_eval_response_shapes.DATASET_OUT` / `RUN_OUT`.
_DATASET_AGENT = {"anyOf": [{"maxLength": 32, "minLength": 1, "type": "string"}, {"type": "null"}],
                  "title": "Agent Id"}
ALLOWED_DELTA += [
    ("/components/schemas/DatasetCreate/properties/agent_id", "add", _DATASET_AGENT),
    ("/components/schemas/DatasetUpload/properties/agent_id", "add", _DATASET_AGENT),
    ("/components/schemas/DatasetUpdate/properties/agent_id", "add", {
        "anyOf": [{"maxLength": 32, "type": "string"}, {"type": "null"}], "title": "Agent Id"}),
]

# D3-d / R3-c (r17-bj, lead-directed 2026-10-24 eval-accuracy Round 3 — "datasets get a tuning /
# held-out split … acceptance runs may use held-out scenarios only"): `RunCreate` gains the
# optional `split` (all | tuning | held_out, default all). A scenario's own `split` rides in the
# untyped `items`; the payloads' additive `split_counts` / `dataset_split` are pinned in
# `test_eval_response_shapes`.
ALLOWED_DELTA.append(
    ("/components/schemas/RunCreate/properties/split", "add", {
        "default": "all", "enum": ["all", "tuning", "held_out"], "title": "Split",
        "type": "string"}),
)

# R3-d (r17-bi3, lead-directed 2026-10-24 eval-accuracy Round 3 — "a power calculation shown
# before start, fixed-n … verdicts decided on the primary evaluator only"): the canary create
# body gains the optional experiment `design` {mde, sd, alpha, power}; the verdict tests the
# primary once at the planned sessions per arm. Additive; the read-back is untyped.
ALLOWED_DELTA += [
    ("/components/schemas/CanaryDesign", "add", {
        "additionalProperties": False,
        "properties": {
            "alpha": {"default": 0.05, "exclusiveMinimum": 0.0, "maximum": 0.5,
                      "title": "Alpha", "type": "number"},
            "mde": {"exclusiveMinimum": 0.0, "maximum": 1.0, "title": "Mde",
                    "type": "number"},
            "power": {"default": 0.8, "exclusiveMaximum": 1.0, "minimum": 0.5,
                      "title": "Power", "type": "number"},
            "sd": {"default": 0.3, "exclusiveMinimum": 0.0, "maximum": 1.0,
                   "title": "Sd", "type": "number"},
        },
        "required": ["mde"], "title": "CanaryDesign", "type": "object"}),
    (f"{_CANARY}/design", "add", {
        "anyOf": [{"$ref": "#/components/schemas/CanaryDesign"}, {"type": "null"}]}),
]

# Audit 7.6 (r17-bj3, lead-directed 2026-10-24 pre-video batch — "editing a SCORING field of an
# evaluator an enabled online config or a running canary references → 409 evaluator.in_use …
# admin force allowed, as with the existing in_use pattern"): the evaluator PUT gains the same
# optional `force` query parameter as its DELETE.
ALLOWED_DELTA.append(
    ("/paths/~1api~1eval~1evaluators~1{evaluator_id}/put/parameters", "replace",
     [_path_param("evaluator_id", "Evaluator Id"), _FORCE_PARAM]),
)

# N17-obs464 (r17-bl, lead-directed 2026-10-26 — "edit the AI-recommended prompt by hand and
# publish it from the card, also after it was accepted; pin the expected current version so a
# concurrent publish gets 409"): `POST …/recommendations/{rec_id}/revise` with
# `RecommendationRevise {system_prompt, expected_version}`. The recommendation payload's
# additive `edits` / `final_prompt` are untyped (no entry).
ALLOWED_DELTA += [
    ("/components/schemas/RecommendationRevise", "add", {
        "properties": {
            "expected_version": {"maxLength": 32, "minLength": 1, "title": "Expected Version",
                                 "type": "string"},
            "system_prompt": {"maxLength": 20000, "title": "System Prompt", "type": "string"},
        },
        "required": ["expected_version", "system_prompt"],
        "title": "RecommendationRevise", "type": "object"}),
    ("/paths/~1api~1eval~1runs~1{run_id}~1recommendations~1{rec_id}~1revise", "add", {
        "post": {
            "operationId": "revise_run_recommendation_api_eval_runs__run_id__recommendations"
                           "__rec_id__revise_post",
            "parameters": [_path_param("run_id", "Run Id"), _path_param("rec_id", "Rec Id")],
            "requestBody": {"content": {"application/json": {"schema": {
                "$ref": "#/components/schemas/RecommendationRevise"}}}, "required": True},
            "responses": {
                "202": {"content": {"application/json": {"schema": {
                    "additionalProperties": True,
                    "title": "Response Revise Run Recommendation Api Eval Runs  Run Id  "
                             "Recommendations  Rec Id  Revise Post",
                    "type": "object"}}}},
                "422": {"content": {"application/json": {"schema": {
                    "$ref": "#/components/schemas/HTTPValidationError"}}}},
            },
            "summary": "Revise Run Recommendation",
            "tags": ["evaluation"],
        }}),
]

PENDING_DELTA: list[Delta] = [
    # (W-E1-5 restored `/sessions/{id}/evaluate` + SessionEvaluateBody; W-E2-3 restored
    #  `/api/overview/online-quality`; W-E5 restored the evaluation-plan routes — every
    #  pending entry is consumed)
]


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def apply_delta(document: dict[str, Any], entries: list[Delta]) -> dict[str, Any]:
    """Return a copy of ``document`` with ``entries`` applied (RFC 6901 pointers).

    Raises ``KeyError`` when a pointer does not resolve — a delta that no longer
    matches the baseline (or a PENDING entry whose route has come back) is an error,
    never a silent no-op.
    """
    doc = copy.deepcopy(document)
    for pointer, op, value in entries:
        tokens = [_unescape(t) for t in pointer.lstrip("/").split("/")]
        parent: Any = doc
        for token in tokens[:-1]:
            parent = parent[int(token)] if isinstance(parent, list) else parent[token]
        last = tokens[-1]
        key: Any = int(last) if isinstance(parent, list) else last
        if op == "remove":
            if isinstance(parent, list):
                parent.pop(key)
            else:
                if key not in parent:
                    raise KeyError(pointer)
                del parent[key]
        elif op == "replace":
            if isinstance(parent, dict) and key not in parent:
                raise KeyError(pointer)
            parent[key] = copy.deepcopy(value)
        elif op == "add":
            parent[key] = copy.deepcopy(value)
        else:
            raise ValueError(f"unknown delta op {op!r} at {pointer}")
    return doc
