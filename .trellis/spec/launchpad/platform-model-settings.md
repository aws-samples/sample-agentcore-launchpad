# Platform Model Settings and the Price Table

> The platform's own model choices are `Settings` fields that an admin can override from the
> V2 Models page. Introduced by task `10-14-v2-model-settings`, ported from `origin/console-v3`
> with main's values.

## 1. Scope / Trigger

This spec applies when you:
- add or read a platform model role;
- change a hard-coded model default;
- touch `get_settings()` caching, `model_prices`, or `/api/model-defaults` and
  `/api/admin/models/*`.

## 2. Contracts

### Roles

Every role lives in the registry in `services/model_settings.py`, together with its constraint.

| Role | Default | Constraint |
|---|---|---|
| `agent_default_model_id` | `global.openai.gpt-6-sol` | — |
| `claude_sdk_model_id` | `global.anthropic.claude-sonnet-5` | Claude only |
| `judge_model_id` | `global.openai.gpt-6-sol` | — |
| `judge_fallback_model_id` | `global.anthropic.claude-sonnet-5-5` | may be empty, which means no retry |
| `assistant_model_id` | `global.openai.gpt-6-sol` | — |
| `architect_model_id` | `global.openai.gpt-6-astra` | — |
| `actor_model_id` | `global.openai.gpt-6-luna` | — |
| `studio_fallback_model_id` | `global.anthropic.claude-sonnet-5` | — |

- **The agent default (user decision, 2026-10-14).** It is the backend default for an
  `AgentSpec` without a `model_id` **and** the V2 wizard's pre-selection. Before this change the
  backend default was `claude-sonnet-5`.
- **Existing settings in the same registry:** `codegen_model` (Claude only),
  `prompt_opt_default_model_id`, `skill_lab_judge_model_id` (OpenAI or Claude),
  `skill_lab_target_model_id` (Claude only) and `skill_lab_codex_target_model_id` (OpenAI only).
- **No `platform_model_id`.** Main has no consumer for it.

### Never read the constants

**Read the settings live, never a constant.** For example, `AgentSpec.model_id` uses
`default_factory=agent_default_model_id`.

- **Never read `AgentSpec.model_fields["model_id"].default`.** Under a default factory it is
  `PydanticUndefined`, and a guard test fails on it. `harness_convert` had this bug, and the
  branch still does.
- **A container spec without a model gets `claude_sdk_model_id`.** That path is the Claude
  Agent SDK.
- **The Studio IAM fallback is its own role.** The backend IAM scope and the frontend codegen
  must both read `studio_fallback_model_id`, from the server.

### Overrides

Overrides are stored in `platform_settings`, which is hub-global and **not** workspace-scoped.
`create_all` builds it, and each box (prod, prod2) has its own copy.

- **Precedence:** console override > `LAUNCHPAD_*` env > yaml > field default.
- **Overrides survive `get_settings.cache_clear()`.** `model_settings` keeps an in-process
  snapshot, loaded at startup after `init_db` and refreshed on every set or clear. A hook
  registered through `config.register_settings_hook` applies that snapshot to every freshly
  built `Settings`. On the branch, the 24 h price refresher and default-workspace resource writes
  cleared the cache and silently reverted every override.
- **Never build a `Settings` inside the sync** when the cache is empty. A `Settings` built at that
  moment captures whatever environment is set right then, which broke another test on the same
  xdist worker.

### Routes

- `GET /api/model-defaults` (member) returns each role's effective `model_id`, `default` (what a
  reset returns to: env, then yaml, then the field default), `overridden`, `constraint`,
  `updated_by` and `updated_at`.
- `PUT` and `DELETE /api/admin/models/{role}` and `POST /api/admin/models/probe` are ADMIN.
- The probe is one tiny fixed Converse call (`converse` or `tool_use`) in the default workspace.
  There is no judge-evaluator probe.

### Prices

- `_seed_model_prices` **merges** the built-in defaults under a yaml `model_prices` block. Yaml
  wins on the keys it has. Before this, a yaml block replaced the whole dict and hid every newer
  default; prod and prod2 both have one.
- The refresher never rewrites a built-in `global.`-scoped short key with a regional premium or
  with a sibling model's price. Before the fix, `glm-5.3` and `kimi-k3` drifted about 10%, and
  `gpt-6-astra` took `openai.gpt-6-astra` at 11/55. Operator-set keys keep the old behaviour.

### Frontend

- `lib/model-defaults.ts` holds the static fallbacks, which equal the backend defaults. It fetches
  once per load and does not cache a failure.
- **A server default is adopted only while a field still holds the static seed.** A user's
  choice or a stored id is never rewritten.
- On Mantle, the wizard keeps `MODEL_CATALOG.mantle[0]`, because Mantle ids are a different id
  space. See `model-source-selection.md`: "order picks the default" now holds only on Mantle.

## 3. Tests

- **`test_model_settings.py`:** defaults, container → Claude, Studio independence, override
  set/clear/persist, survival across `cache_clear`, admin-only writes, constraint validation, a
  stubbed probe.
- **`test_model_prices.py`:** the yaml merge, the measured GLM-5.3 / Kimi-K3 / Astra case with a
  stubbed litellm map, and the prices of the offered models.
- **`test_videos.py`:** every V2 nav path, including `/v2/models`, needs a video taxonomy section.
- **`model-defaults.test.ts`:** the frontend side of the role defaults.
- **Live check (`research/live-check.md`):** probes for the default models, and a zip_runtime
  agent created without a model, which stored `gpt-6-sol`/`bedrock`, deployed, answered, and was
  then deleted.
