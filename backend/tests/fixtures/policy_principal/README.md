# Stage-0 contract fixtures (per-user principal + gateway interceptor + Dogwood engine)

Contract: `docs/cn/interfaces/policy-principal-contracts.md`; schemas:
`backend/app/policy/contracts/*.schema.json`; reference codec: `backend/app/policy/contracts/codec.py`.

Everything here is **public or synthetic**. There are no private keys, no HMAC keys and no tokens.
Tests generate an ephemeral ES256 key pair and an HMAC key at runtime
(`tests/policy_principal/conftest.py`), sign the claim sets below, and substitute the public key
wherever a fixture says `__TEST_PUBKEY__`. Account ids are the documentation placeholder
`123456789012`.

| File | Interface | Content |
|---|---|---|
| `claims_valid.json` | I1 | claim sets that must verify (user member / admin, api_key, system eval, BYOC 5-min TTL) |
| `claims_invalid.json` | I1 | `{name, base, patch, expect}`: a valid base with a patch applied, and the verifier error code it must yield |
| `gateway_bundle.json` | I3 | a complete bundle |
| `engine_messages.json` | I4 / I5 | one example per request / response / error shape |
| `audit_lines.json` | I7 | one gateway decision line, one engine line |
| `jsonrpc_errors.json` | I8 | deny, session-required, conflict |
| `runtime_frames.json` | runtime frame | a gateway `policy_denied` frame |
| `eval_tokens.json` | I9 v2 (D5, amendment 10) | an eval `tokens.json` — ONE `System::"eval"` token per job (`__TOKEN__` placeholder, the claims the test signs with `sid` = the job id), invalid patches |
| `engine_invoke.json` | I4 amendment 4 | the item of a hub INVOKE decision (authorize_stateless), the baseline invoke permits, 5 cases incl. a wrong item |
| `interceptor_events/*.json` | B input | the P0-recorded event shapes (2026-10-26), redacted and anonymised |
