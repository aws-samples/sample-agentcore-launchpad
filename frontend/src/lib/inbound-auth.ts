import type { InboundAuth, InboundAuthMode, InboundCustomClaim, JwtInboundConfig, OidcSource } from "./api";

/**
 * Editable JWT-authorizer form state. Lists stay strings (comma/space separated)
 * so typing is unconstrained; `jwtConfigFromForm` produces the API shape.
 */
export interface JwtFormState {
  discovery_url: string;
  allowed_clients: string;
  allowed_audience: string;
  allowed_scopes: string;
  custom_claims: InboundCustomClaim[];
  /** display-only: the Connection `discovery_url` was picked from ("" = typed) */
  source_connection: string;
}

export const EMPTY_JWT_FORM: JwtFormState = {
  discovery_url: "",
  allowed_clients: "",
  allowed_audience: "",
  allowed_scopes: "",
  custom_claims: [],
  source_connection: "",
};

/** The wizard's choice: `inherit` omits `inbound_auth` (the workspace default applies at deploy). */
export type InboundChoice = "inherit" | InboundAuthMode;

/** Methods whose Runtime can front a JWT authorizer (backend `JWT_CAPABLE_METHODS`). */
export const JWT_CAPABLE_METHODS = ["zip_runtime", "studio", "container", "byoc"] as const;

export const inboundCapable = (method: string, protocol?: string) =>
  (JWT_CAPABLE_METHODS as readonly string[]).includes(method) && protocol !== "a2a";

export const splitList = (s: string) => s.split(/[\s,]+/).filter(Boolean);

export function jwtFormFromConfig(config: JwtInboundConfig | null | undefined): JwtFormState {
  if (!config) return { ...EMPTY_JWT_FORM };
  return {
    discovery_url: config.discovery_url ?? "",
    allowed_clients: (config.allowed_clients ?? []).join(", "),
    allowed_audience: (config.allowed_audience ?? []).join(", "),
    allowed_scopes: (config.allowed_scopes ?? []).join(", "),
    custom_claims: (config.custom_claims ?? []).map((claim) => ({ ...claim })),
    source_connection: config.source_connection ?? "",
  };
}

/**
 * Whether the workspace-default editor differs from the saved default, compared
 * as the config a save would send (so list spacing alone is not a change).
 */
export function inboundDraftChanged(saved: InboundAuth, mode: InboundAuthMode, form: JwtFormState): boolean {
  if (mode !== saved.mode) return true;
  if (mode === "iam") return false;
  const sent = (f: JwtFormState) => JSON.stringify(jwtConfigFromForm(f));
  return sent(form) !== sent(jwtFormFromConfig(saved.jwt));
}

export function jwtConfigFromForm(form: JwtFormState): JwtInboundConfig {
  return {
    discovery_url: form.discovery_url.trim(),
    allowed_clients: splitList(form.allowed_clients),
    allowed_audience: splitList(form.allowed_audience),
    allowed_scopes: splitList(form.allowed_scopes),
    custom_claims: form.custom_claims
      .filter((claim) => claim.name.trim() && claim.match_values.some((v) => v.trim()))
      .map((claim) => ({
        ...claim,
        name: claim.name.trim(),
        match_values: claim.match_values.map((v) => v.trim()).filter(Boolean),
      })),
    ...(form.source_connection ? { source_connection: form.source_connection } : {}),
  };
}

// the service-model patterns (CustomClaimValidation) the backend also enforces
const CLAIM_NAME = /^[A-Za-z0-9_.\-:]+$/;
const CLAIM_VALUE = /^[A-Za-z0-9_.-]+$/;

/** Why the form cannot be submitted yet, as an `inboundAuth.problems.*` key — or null. */
export function jwtFormProblem(form: JwtFormState): string | null {
  const config = jwtConfigFromForm(form);
  if (!/^https:\/\/.+\/\.well-known\/openid-configuration$/.test(config.discovery_url)) {
    return "inboundAuth.problems.discoveryUrl";
  }
  if (
    !config.allowed_clients.length &&
    !config.allowed_audience.length &&
    !config.allowed_scopes.length &&
    !config.custom_claims.length
  ) {
    return "inboundAuth.problems.noRestriction";
  }
  if (config.custom_claims.some((c) => !CLAIM_NAME.test(c.name) || !c.match_values.every((v) => CLAIM_VALUE.test(v)))) {
    return "inboundAuth.problems.claimPattern";
  }
  return null;
}

/** The spec's `inbound_auth` for a choice; `undefined` ⇒ omitted (inherit). */
export function inboundAuthFromChoice(choice: InboundChoice, form: JwtFormState): InboundAuth | undefined {
  if (choice === "inherit") return undefined;
  return choice === "jwt" ? { mode: "jwt", jwt: jwtConfigFromForm(form) } : { mode: "iam" };
}

/** The wizard state a stored spec's `inbound_auth` loads back into. */
export function choiceFromSpec(stored: unknown): { choice: InboundChoice; form: JwtFormState } {
  const auth = stored as InboundAuth | null | undefined;
  if (auth?.mode === "jwt") return { choice: "jwt", form: jwtFormFromConfig(auth.jwt) };
  if (auth?.mode === "iam") return { choice: "iam", form: { ...EMPTY_JWT_FORM } };
  return { choice: "inherit", form: { ...EMPTY_JWT_FORM } };
}

/** One-line summary of a JWT config (detail views, the switch confirm). */
export function jwtSummary(config: { allowed_clients?: string[]; allowed_audience?: string[]; allowed_scopes?: string[]; custom_claims?: unknown[] } | null | undefined): string {
  if (!config) return "";
  const parts: string[] = [];
  if (config.allowed_clients?.length) parts.push(`clients: ${config.allowed_clients.join(", ")}`);
  if (config.allowed_audience?.length) parts.push(`aud: ${config.allowed_audience.join(", ")}`);
  if (config.allowed_scopes?.length) parts.push(`scopes: ${config.allowed_scopes.join(", ")}`);
  if (config.custom_claims?.length) {
    const names = config.custom_claims.map((c) => (typeof c === "string" ? c : (c as InboundCustomClaim).name));
    parts.push(`claims: ${names.join(", ")}`);
  }
  return parts.join(" · ");
}

/**
 * The JWT config the switch dialog opens with: the workspace default when it is
 * JWT, else the workspace Cognito preset; null when neither exists (the dialog
 * then opens empty). Every field stays editable.
 */
export function switchTargetJwt(
  workspaceDefault: InboundAuth | null | undefined,
  cognito: JwtInboundConfig | null | undefined,
): JwtInboundConfig | null {
  if (workspaceDefault?.mode === "jwt" && workspaceDefault.jwt) return workspaceDefault.jwt;
  return cognito ?? null;
}

/** Where the switch dialog's prefill came from (`inboundAuth.switch.from.*`). */
export type SwitchPrefill = "default" | "cognito" | "empty";

/** The switch dialog's initial form and the label of its prefill. */
export function switchDialogInitial(
  workspaceDefault: InboundAuth | null | undefined,
  cognito: JwtInboundConfig | null | undefined,
): { form: JwtFormState; prefill: SwitchPrefill } {
  const prefill: SwitchPrefill =
    workspaceDefault?.mode === "jwt" && workspaceDefault.jwt ? "default" : cognito ? "cognito" : "empty";
  return { form: jwtFormFromConfig(switchTargetJwt(workspaceDefault, cognito)), prefill };
}

// ── issuers: which IdP a discovery URL names (OIDC Discovery 1.0 §4) ─────────

export const DISCOVERY_SUFFIX = "/.well-known/openid-configuration";

/**
 * An issuer in comparable form (lowercased scheme + host, no trailing slash),
 * or null for a non-http(s) or templated (`{tenantid}`) value — backend
 * `normalize_issuer`.
 */
export function normalizeIssuer(issuer: string | null | undefined): string | null {
  const raw = (issuer ?? "").trim();
  if (!raw || /[{}]/.test(raw)) return null;
  const match = /^(https?):\/\/([^/?#]+)([^?#]*)/i.exec(raw);
  if (!match) return null;
  return `${match[1].toLowerCase()}://${match[2].toLowerCase()}${match[3].replace(/\/+$/, "")}`;
}

/** The issuer a discovery URL belongs to, or null when it is not one. */
export function issuerFromDiscoveryUrl(url: string | null | undefined): string | null {
  const raw = (url ?? "").trim();
  if (!raw.endsWith(DISCOVERY_SUFFIX)) return null;
  return normalizeIssuer(raw.slice(0, -DISCOVERY_SUFFIX.length));
}

/**
 * The console-reachability check: every platform invoke (console Chat both
 * ways, /v1, direct invoke, evaluation) presents a workspace-Cognito token, so
 * an agent trusting another issuer refuses them all. Null when they match or
 * either side is unknown (no pool yet, or a half-typed URL) — nothing to warn.
 */
export function issuerMismatch(
  discoveryUrl: string | null | undefined,
  workspaceIssuer: string | null | undefined,
): { agentIssuer: string; workspaceIssuer: string } | null {
  const agent = issuerFromDiscoveryUrl(discoveryUrl);
  const workspace = normalizeIssuer(workspaceIssuer);
  if (!agent || !workspace || agent === workspace) return null;
  return { agentIssuer: agent, workspaceIssuer: workspace };
}

/**
 * The JWT authorizer a wizard choice deploys with: the pinned form, the JWT
 * workspace default when inheriting, else none (IAM). What the review step
 * shows and checks for console reachability.
 */
export function wizardDeployedJwt(
  choice: InboundChoice | null | undefined,
  form: JwtFormState | null | undefined,
  workspaceDefault: InboundAuth | null | undefined,
): JwtInboundConfig | null {
  if (choice === "jwt") return jwtConfigFromForm(form ?? EMPTY_JWT_FORM);
  if ((choice ?? "inherit") === "inherit" && workspaceDefault?.mode === "jwt") return workspaceDefault.jwt ?? null;
  return null;
}

/** A single agent's config, or the workspace default every inheriting agent resolves to. */
export type ReachabilityScope = "agent" | "default";

/** The reachability warning's copy per scope: a workspace default names its inheritors, not one agent. */
export const REACHABILITY_KEYS: Record<ReachabilityScope, { title: string; body: string }> = {
  agent: { title: "inboundAuth.reachability.title", body: "inboundAuth.reachability.body" },
  default: { title: "inboundAuth.reachability.defaultTitle", body: "inboundAuth.reachability.defaultBody" },
};

/**
 * Take the discovery URL from a Connection. Only `discovery_url` (and the
 * display-only source name) change: the Connection's client id is the agent's
 * own OUTBOUND client, never the inbound caller, so the allowed lists stay.
 */
export function applyOidcSource(form: JwtFormState, source: OidcSource): JwtFormState {
  return { ...form, discovery_url: source.discovery_url, source_connection: source.name };
}

/** A typed discovery URL; the Connection label goes once the URL is no longer its. */
export function withDiscoveryUrl(form: JwtFormState, url: string): JwtFormState {
  return { ...form, discovery_url: url, source_connection: url === form.discovery_url ? form.source_connection : "" };
}

/**
 * A copy-paste M2M caller: a client_credentials token from the IdP's token
 * endpoint, then a bearer POST to the Runtime. Secrets stay shell variables.
 */
export function m2mCurlExample(invokeUrl: string, discoveryUrl: string, scopes: string[] = []): string {
  const scope = scopes.length ? ` \\\n  -d scope=${JSON.stringify(scopes.join(" "))}` : "";
  return [
    `# token_endpoint is listed in ${discoveryUrl}`,
    `TOKEN=$(curl -s -X POST "$TOKEN_ENDPOINT" \\`,
    `  -u "$CLIENT_ID:$CLIENT_SECRET" \\`,
    `  -d grant_type=client_credentials${scope} | jq -r .access_token)`,
    "",
    `curl -X POST "${invokeUrl}" \\`,
    `  -H "Authorization: Bearer $TOKEN" \\`,
    `  -H "Content-Type: application/json" \\`,
    `  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: $(uuidgen)-$(uuidgen)" \\`,
    `  -d '{"prompt": "hello"}'`,
  ].join("\n");
}

/** The Runtime data-plane URL a bearer caller POSTs to (backend `bearer_invoke_url`). */
export function bearerInvokeUrl(arn: string | null | undefined): string | null {
  const parts = (arn ?? "").split(":");
  if (parts.length < 6 || parts[2] !== "bedrock-agentcore" || !parts[5].startsWith("runtime/")) return null;
  return `https://bedrock-agentcore.${parts[3]}.amazonaws.com/runtimes/${encodeURIComponent(arn as string)}/invocations?qualifier=DEFAULT`;
}
