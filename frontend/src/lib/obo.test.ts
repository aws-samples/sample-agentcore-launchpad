import { describe, expect, it } from "vitest";

import { EMPTY_OBO_FORM, oboAvailability, oboFromForm, targetModes } from "./obo";

describe("oboAvailability", () => {
  it("offers OBO on CustomOauth2 templates except Cognito", () => {
    expect(oboAvailability({ id: "custom_oidc", vendor: "CustomOauth2" })).toBe("offered");
    expect(oboAvailability({ id: "custom_endpoints", vendor: "CustomOauth2" })).toBe("offered");
    expect(oboAvailability({ id: "cognito", vendor: "CustomOauth2" })).toBe("cognito");
    expect(oboAvailability({ id: "github", vendor: "GithubOauth2" })).toBe("none");
    expect(oboAvailability(undefined)).toBe("none");
  });
});

describe("oboFromForm", () => {
  it("is omitted when off", () => {
    expect(oboFromForm(EMPTY_OBO_FORM)).toBeUndefined();
  });

  it("drops actor scopes unless the actor token is M2M", () => {
    expect(oboFromForm({ ...EMPTY_OBO_FORM, enabled: true, actor_token_scopes: "a b" })).toEqual({
      grant_type: "TOKEN_EXCHANGE",
      actor_token_content: "NONE",
      actor_token_scopes: [],
    });
    expect(
      oboFromForm({ enabled: true, grant_type: "TOKEN_EXCHANGE", actor_token_content: "M2M", actor_token_scopes: "api/read, api/write" }),
    ).toEqual({ grant_type: "TOKEN_EXCHANGE", actor_token_content: "M2M", actor_token_scopes: ["api/read", "api/write"] });
  });

  it("sends the jwt-bearer grant alone", () => {
    expect(oboFromForm({ enabled: true, grant_type: "JWT_AUTHORIZATION_GRANT", actor_token_content: "M2M", actor_token_scopes: "x" })).toEqual({
      grant_type: "JWT_AUTHORIZATION_GRANT",
    });
  });
});

describe("targetModes", () => {
  it("offers user-bound modes only over OAuth2", () => {
    expect(targetModes("oauth2")).toEqual(["as_agent", "as_user", "obo"]);
    expect(targetModes("api_key")).toEqual(["as_agent"]);
    expect(targetModes(undefined)).toEqual(["as_agent"]);
  });
});
