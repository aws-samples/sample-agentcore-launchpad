import type { ActingMode, ConnectionKind, ConnectionTemplate, OboConfig } from "./api";

/**
 * Whether a connection template can carry an on-behalf-of token-exchange config
 * (backend `obo_config_input` / `check_obo_idp`): only CustomOauth2 providers
 * take `onBehalfOfTokenExchangeConfig`, and Cognito implements neither RFC 8693
 * nor RFC 7523 (docs/identity.md §8.3), so its template says why instead.
 */
export function oboAvailability(template: Pick<ConnectionTemplate, "id" | "vendor"> | undefined): "offered" | "cognito" | "none" {
  if (!template || template.vendor !== "CustomOauth2") return "none";
  return template.id === "cognito" ? "cognito" : "offered";
}

export interface OboFormState {
  enabled: boolean;
  grant_type: OboConfig["grant_type"];
  actor_token_content: NonNullable<OboConfig["actor_token_content"]>;
  actor_token_scopes: string;
}

export const EMPTY_OBO_FORM: OboFormState = {
  enabled: false,
  grant_type: "TOKEN_EXCHANGE",
  actor_token_content: "NONE",
  actor_token_scopes: "",
};

/**
 * The request's `obo` block, or undefined when off. The actor token is an RFC 8693
 * notion (jwt-bearer sends the grant alone); actor scopes ride only with an M2M actor.
 */
export function oboFromForm(form: OboFormState): OboConfig | undefined {
  if (!form.enabled) return undefined;
  if (form.grant_type === "JWT_AUTHORIZATION_GRANT") return { grant_type: form.grant_type };
  const m2m = form.actor_token_content === "M2M";
  return {
    grant_type: form.grant_type,
    actor_token_content: form.actor_token_content,
    actor_token_scopes: m2m ? form.actor_token_scopes.split(/[\s,]+/).filter(Boolean) : [],
  };
}

/** Acting modes a gateway target offers for a connection kind (backend `create_target`). */
export function targetModes(kind: ConnectionKind | undefined): ActingMode[] {
  return kind === "oauth2" ? ["as_agent", "as_user", "obo"] : ["as_agent"];
}
