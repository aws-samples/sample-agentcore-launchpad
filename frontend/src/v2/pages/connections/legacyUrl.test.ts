import { describe, expect, it } from "vitest";

import { legacyMyConnectionsToV2 } from "./legacyUrl";

describe("legacyMyConnectionsToV2", () => {
  it("lands on the My authorizations tab of Connections", () => {
    expect(legacyMyConnectionsToV2("")).toBe("/v2/connections?view=mine");
  });
  it("preserves other query params and overrides a stale view", () => {
    expect(legacyMyConnectionsToV2("?connection=github&view=old")).toBe(
      "/v2/connections?connection=github&view=mine",
    );
  });
});
