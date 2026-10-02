import { describe, expect, it } from "vitest";

import { type AgentFormCatalogs, agentFormValid, buildAgentSpec, formFromStoredSpec, republishSpec } from "./agent-spec";
import {
  applyOidcSource,
  bearerInvokeUrl,
  EMPTY_JWT_FORM,
  inboundDraftChanged,
  issuerFromDiscoveryUrl,
  issuerMismatch,
  normalizeIssuer,
  REACHABILITY_KEYS,
  switchDialogInitial,
  withDiscoveryUrl,
  inboundCapable,
  jwtConfigFromForm,
  jwtFormFromConfig,
  jwtFormProblem,
  m2mCurlExample,
  switchTargetJwt,
  wizardDeployedJwt,
} from "./inbound-auth";

const DISCOVERY = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x/.well-known/openid-configuration";
const JWT = { discovery_url: DISCOVERY, allowed_clients: ["c1"], allowed_audience: [], allowed_scopes: [], custom_claims: [] };
const CATALOGS: AgentFormCatalogs = {
  gatewayTargets: [],
  remoteMcp: [],
  storedGatewayConfig: {},
  kbInfo: (id) => ({ kb_id: id, name: id, description: "" }),
};
const STORED = { name: "jwt-agent", method: "zip_runtime", system_prompt: "p" };
const load = (stored: object) => formFromStoredSpec("zip_runtime", "jwt-agent", stored).form;
const valid = (form: ReturnType<typeof load>) =>
  agentFormValid(form, CATALOGS, { knobIssues: [], byocUploading: false, connections: [] });

describe("JWT form", () => {
  it("round-trips a config through the editable strings", () => {
    const config = { ...JWT, allowed_scopes: ["a/read", "a/write"] };
    expect(jwtConfigFromForm(jwtFormFromConfig(config))).toEqual(config);
  });

  it("names the first problem", () => {
    const form = jwtFormFromConfig(JWT);
    expect(jwtFormProblem(form)).toBeNull();
    expect(jwtFormProblem({ ...form, discovery_url: "http://idp/.well-known/openid-configuration" })).toBe(
      "inboundAuth.problems.discoveryUrl",
    );
    expect(jwtFormProblem({ ...form, allowed_clients: " , " })).toBe("inboundAuth.problems.noRestriction");
    expect(
      jwtFormProblem({
        ...form,
        custom_claims: [{ name: "bad name", value_type: "STRING", match_operator: "EQUALS", match_values: ["x"] }],
      }),
    ).toBe("inboundAuth.problems.claimPattern");
  });

  it("only offers JWT to HTTP Runtime methods", () => {
    expect(inboundCapable("zip_runtime")).toBe(true);
    expect(inboundCapable("container", "a2a")).toBe(false);
    expect(inboundCapable("harness")).toBe(false);
  });
});

describe("inbound_auth in the agent spec", () => {
  it("states null for inherit so a V2 re-publish unpins", () => {
    const form = load({ ...STORED, inbound_auth: { mode: "iam" } });
    expect(form.inbound).toBe("iam");
    const spec = republishSpec(buildAgentSpec({ ...form, inbound: "inherit" }, CATALOGS), {
      ...STORED,
      inbound_auth: { mode: "iam" },
    });
    expect(spec.inbound_auth).toBeNull();
  });

  it("round-trips a JWT pin", () => {
    const stored = { ...STORED, inbound_auth: { mode: "jwt", jwt: JWT } };
    const form = load(stored);
    expect(form.inbound).toBe("jwt");
    expect(valid(form)).toBe(true);
    expect(buildAgentSpec(form, CATALOGS).inbound_auth).toEqual({ mode: "jwt", jwt: JWT });
  });

  it("carries the stored pin when the form has no inbound input (classic wizard)", () => {
    const stored = { ...STORED, inbound_auth: { mode: "jwt", jwt: JWT } };
    const form = { ...load(stored), inbound: undefined, inboundJwt: undefined };
    const built = buildAgentSpec(form, CATALOGS);
    expect("inbound_auth" in built).toBe(false);
    expect(republishSpec(built, stored).inbound_auth).toEqual({ mode: "jwt", jwt: JWT });
  });

  it("blocks an invalid JWT pin and ignores it for a harness", () => {
    const form = { ...load(STORED), inbound: "jwt" as const, inboundJwt: { ...EMPTY_JWT_FORM } };
    expect(valid(form)).toBe(false);
    expect(valid({ ...form, method: "harness" })).toBe(true);
  });
});

describe("switch and caller helpers", () => {
  it("prefers a JWT workspace default over the Cognito preset", () => {
    const cognito = { ...JWT, allowed_clients: ["pool-client"] };
    expect(switchTargetJwt({ mode: "jwt", jwt: JWT }, cognito)).toBe(JWT);
    expect(switchTargetJwt({ mode: "iam" }, cognito)).toBe(cognito);
    expect(switchTargetJwt(null, null)).toBeNull();
  });

  it("builds the bearer URL and an M2M curl", () => {
    const arn = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/x-abc";
    const url = bearerInvokeUrl(arn);
    expect(url).toBe(
      `https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/${encodeURIComponent(arn)}/invocations?qualifier=DEFAULT`,
    );
    expect(bearerInvokeUrl("arn:aws:iam::1:role/x")).toBeNull();
    const curl = m2mCurlExample(url as string, DISCOVERY, ["launchpad/invoke"]);
    expect(curl).toContain("grant_type=client_credentials");
    expect(curl).toContain('-d scope="launchpad/invoke"');
    expect(curl).toContain("Authorization: Bearer $TOKEN");
  });
});

const POOL_ISSUER = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x";
const OKTA = "https://acme.okta.com/oauth2/default/.well-known/openid-configuration";
const OKTA_SOURCE = {
  name: "corp-okta",
  vendor: "CustomOauth2",
  discovery_url: OKTA,
  issuer: "https://acme.okta.com/oauth2/default",
  derived_from: "discovery_url" as const,
};

describe("issuer compare", () => {
  it("derives the issuer a discovery URL names", () => {
    expect(issuerFromDiscoveryUrl(DISCOVERY)).toBe(POOL_ISSUER);
    expect(issuerFromDiscoveryUrl(" HTTPS://Login.Example.COM/Tenant/v2.0/.well-known/openid-configuration ")).toBe(
      "https://login.example.com/Tenant/v2.0",
    );
    expect(issuerFromDiscoveryUrl("https://idp.example/oidc")).toBeNull();
    expect(issuerFromDiscoveryUrl("")).toBeNull();
    expect(normalizeIssuer("https://idp.example/")).toBe("https://idp.example");
    expect(normalizeIssuer("https://login.microsoftonline.com/{tenantid}/v2.0")).toBeNull();
    expect(normalizeIssuer("idp.example")).toBeNull();
  });

  it("warns only when both issuers are known and differ", () => {
    expect(issuerMismatch(DISCOVERY, POOL_ISSUER)).toBeNull();
    expect(issuerMismatch(DISCOVERY, `${POOL_ISSUER}/`)).toBeNull();
    expect(issuerMismatch(OKTA, POOL_ISSUER)).toEqual({
      agentIssuer: "https://acme.okta.com/oauth2/default",
      workspaceIssuer: POOL_ISSUER,
    });
    // no pool yet, or a half-typed URL: nothing to compare
    expect(issuerMismatch(OKTA, null)).toBeNull();
    expect(issuerMismatch("https://acme.okta.com/oau", POOL_ISSUER)).toBeNull();
  });
});

describe("Connection → discovery", () => {
  it("fills the discovery URL and source but never the allowed clients", () => {
    const form = { ...jwtFormFromConfig(JWT), allowed_clients: "caller-app" };
    const next = applyOidcSource(form, OKTA_SOURCE);
    expect(next.discovery_url).toBe(OKTA);
    expect(next.source_connection).toBe("corp-okta");
    expect(next.allowed_clients).toBe("caller-app");
    expect(applyOidcSource({ ...EMPTY_JWT_FORM }, OKTA_SOURCE).allowed_clients).toBe("");
    expect(jwtConfigFromForm(next).source_connection).toBe("corp-okta");
  });

  it("drops the source label once the URL is edited away from it", () => {
    const picked = applyOidcSource({ ...EMPTY_JWT_FORM }, OKTA_SOURCE);
    expect(withDiscoveryUrl(picked, OKTA).source_connection).toBe("corp-okta");
    const typed = withDiscoveryUrl(picked, "https://other.example/.well-known/openid-configuration");
    expect(typed.source_connection).toBe("");
    expect("source_connection" in jwtConfigFromForm(typed)).toBe(false);
  });
});

describe("switch dialog", () => {
  const cognito = { ...JWT, allowed_clients: ["pool-client"] };

  it("prefills from the JWT workspace default, else the Cognito preset, else empty", () => {
    const fromDefault = switchDialogInitial({ mode: "jwt", jwt: { ...JWT, source_connection: "corp-okta" } }, cognito);
    expect(fromDefault.prefill).toBe("default");
    expect(fromDefault.form.allowed_clients).toBe("c1");
    expect(fromDefault.form.source_connection).toBe("corp-okta");
    const fromCognito = switchDialogInitial({ mode: "iam" }, cognito);
    expect(fromCognito.prefill).toBe("cognito");
    expect(fromCognito.form.allowed_clients).toBe("pool-client");
    const empty = switchDialogInitial(null, null);
    expect(empty.prefill).toBe("empty");
    expect(empty.form).toEqual(EMPTY_JWT_FORM);
  });

  it("edits the prefill into an own-IdP config the same validation guards", () => {
    const { form } = switchDialogInitial({ mode: "iam" }, cognito);
    const edited = { ...applyOidcSource(form, OKTA_SOURCE), allowed_clients: "", allowed_audience: "api://agent" };
    expect(jwtFormProblem(edited)).toBeNull();
    expect(jwtConfigFromForm(edited)).toEqual({
      discovery_url: OKTA,
      allowed_clients: [],
      allowed_audience: ["api://agent"],
      allowed_scopes: [],
      custom_claims: [],
      source_connection: "corp-okta",
    });
    expect(issuerMismatch(edited.discovery_url, POOL_ISSUER)).not.toBeNull();
    expect(jwtFormProblem({ ...edited, allowed_audience: "" })).toBe("inboundAuth.problems.noRestriction");
    expect(jwtFormProblem(withDiscoveryUrl(edited, "https://acme.okta.com"))).toBe("inboundAuth.problems.discoveryUrl");
  });
});

describe("switch dialog prefill note", () => {
  it("renders as one sentence run in each language, without a stray space after a CJK mark", async () => {
    const i18n = (await import("../i18n")).default;
    for (const lng of ["en", "zh-CN"]) {
      const t = i18n.getFixedT(lng);
      for (const source of ["fromDefault", "fromCognito", "fromEmpty"]) {
        const note = t("inboundAuth.switch.prefillNote", { source: t(`inboundAuth.switch.${source}`) });
        expect(note).not.toMatch(/inboundAuth\./);
        expect(note).not.toMatch(/[，。：；？！）] /);
      }
    }
  });
});

describe("reachability warning copy", () => {
  it("names the inheriting agents on the workspace default, a single agent elsewhere", async () => {
    const i18n = (await import("../i18n")).default;
    const vars = { issuer: "https://accounts.google.com", workspaceIssuer: "https://cognito-idp.us-west-2.amazonaws.com/p" };
    const zh = i18n.getFixedT("zh-CN");
    const en = i18n.getFixedT("en");
    expect(zh(REACHABILITY_KEYS.agent.title)).toBe("控制台无法调用此智能体");
    expect(zh(REACHABILITY_KEYS.default.title)).toContain("继承此默认值的智能体");
    expect(zh(REACHABILITY_KEYS.default.body, vars)).not.toContain("此智能体");
    expect(en(REACHABILITY_KEYS.default.title)).toContain("inherit this default");
    expect(en(REACHABILITY_KEYS.default.body, vars)).not.toMatch(/this agent\b/);
    for (const scope of ["agent", "default"] as const) {
      for (const t of [zh, en]) {
        expect(t(REACHABILITY_KEYS[scope].body, vars)).toContain("https://accounts.google.com");
        expect(t(REACHABILITY_KEYS[scope].title)).not.toMatch(/^inboundAuth\./);
      }
    }
  });
});

describe("inboundDraftChanged", () => {
  const google = {
    discovery_url: "https://accounts.google.com/.well-known/openid-configuration",
    allowed_clients: ["a", "b"],
    allowed_audience: [],
    allowed_scopes: [],
    custom_claims: [],
  };
  it("is clean for the saved default and for list spacing alone", () => {
    expect(inboundDraftChanged({ mode: "iam" }, "iam", { ...EMPTY_JWT_FORM })).toBe(false);
    expect(inboundDraftChanged({ mode: "iam", jwt: null }, "iam", jwtFormFromConfig(google))).toBe(false);
    const saved = { mode: "jwt" as const, jwt: google };
    expect(inboundDraftChanged(saved, "jwt", jwtFormFromConfig(google))).toBe(false);
    expect(inboundDraftChanged(saved, "jwt", { ...jwtFormFromConfig(google), allowed_clients: "a,b" })).toBe(false);
  });
  it("is dirty for a mode change or an edited JWT field", () => {
    expect(inboundDraftChanged({ mode: "iam" }, "jwt", { ...EMPTY_JWT_FORM })).toBe(true);
    const saved = { mode: "jwt" as const, jwt: google };
    expect(inboundDraftChanged(saved, "iam", jwtFormFromConfig(google))).toBe(true);
    expect(inboundDraftChanged(saved, "jwt", { ...jwtFormFromConfig(google), allowed_scopes: "read" })).toBe(true);
    expect(
      inboundDraftChanged(saved, "jwt", withDiscoveryUrl(jwtFormFromConfig(google), "https://idp.example.com/.well-known/openid-configuration")),
    ).toBe(true);
  });
});

describe("wizardDeployedJwt", () => {
  const google = {
    discovery_url: "https://accounts.google.com/.well-known/openid-configuration",
    allowed_clients: ["c"],
    allowed_audience: [],
    allowed_scopes: [],
    custom_claims: [],
  };
  const pinned = jwtFormFromConfig(google);
  it("is the pinned form for a JWT choice", () => {
    expect(wizardDeployedJwt("jwt", pinned, { mode: "iam" })).toEqual(jwtConfigFromForm(pinned));
    expect(wizardDeployedJwt("jwt", undefined, null)?.discovery_url).toBe("");
  });
  it("is the JWT workspace default when inheriting, else none", () => {
    expect(wizardDeployedJwt("inherit", pinned, { mode: "jwt", jwt: google })).toEqual(google);
    expect(wizardDeployedJwt(undefined, pinned, { mode: "jwt", jwt: google })).toEqual(google);
    expect(wizardDeployedJwt("inherit", pinned, { mode: "iam" })).toBeNull();
    expect(wizardDeployedJwt("inherit", pinned, null)).toBeNull();
    expect(wizardDeployedJwt("iam", pinned, { mode: "jwt", jwt: google })).toBeNull();
  });
  it("feeds the review's reachability check", () => {
    const pool = "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x";
    const deployed = wizardDeployedJwt("jwt", pinned, null);
    expect(issuerMismatch(deployed?.discovery_url, pool)?.agentIssuer).toBe("https://accounts.google.com");
  });
});
