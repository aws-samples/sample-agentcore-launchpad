import { describe, expect, it } from "vitest";

import fixture from "../../../backend/tests/fixtures/method_capabilities.json";
import { BUILTIN_TOOLS, BYOC_KINDS, FILESYSTEM_METHODS, SUPPORTED_ACTING_MODES } from "./agent-spec";
import { HARNESS_NATIVE_TOOLS } from "./api";
import { type MethodCapabilities, parseMethodCapability } from "./capabilities";

/**
 * Round 16 consistency check (design §1.3 A): the frontend's typed option lists are a
 * projection of the backend's method declaration, frozen in
 * `backend/tests/fixtures/method_capabilities.json` by `tests/test_method_capabilities.py`.
 * A declaration that moves re-freezes the fixture and this test says which constant must
 * follow — never a wizard that offers what the server refuses. CONTRACT:
 * docs/cn/interfaces/testing.md §6.1; every assertion is differential (frontend constant vs
 * the backend's frozen declaration), none is an expected value written by hand.
 */
const caps: MethodCapabilities = Object.fromEntries(Object.entries(fixture).map(([m, raw]) => [m, parseMethodCapability(raw)]));
const union = (field: "builtin_tools" | "tool_auth_modes" | "protocols") =>
  [...new Set(Object.values(caps).flatMap((c) => c[field]))].sort();

describe("frontend option lists ⊆ the backend method declaration", () => {
  it("knows the four methods", () => {
    expect(Object.keys(caps).sort()).toEqual(["byoc", "harness", "studio", "zip_runtime"]);
  });
  it("labels exactly the builtins some method renders", () => {
    expect([...BUILTIN_TOOLS].sort()).toEqual(union("builtin_tools"));
    expect(caps.harness.builtin_tools.sort()).toEqual([...BUILTIN_TOOLS].sort()); // the Harness renders them all
  });
  it("offers exactly the acting modes some method implements", () => {
    expect([...SUPPORTED_ACTING_MODES].sort()).toEqual(union("tool_auth_modes"));
    expect(caps.harness.tool_auth_modes).toEqual([]); // the Harness has no identity channel
  });
  it("matches the Harness native tools and the byoc artifact kinds", () => {
    expect([...HARNESS_NATIVE_TOOLS].sort()).toEqual([...caps.harness.native_tools].sort());
    expect([...BYOC_KINDS].sort()).toEqual([...caps.byoc.byoc_kinds].sort());
  });
  it("offers the A2A protocol on zip_runtime only", () => {
    expect(union("protocols")).toEqual(["a2a", "http"]);
    expect(Object.entries(caps).filter(([, c]) => c.protocols.includes("a2a")).map(([m]) => m)).toEqual(["zip_runtime"]);
  });
  it("mounts managed session storage only where the platform renders the runtime", () => {
    for (const method of FILESYSTEM_METHODS) expect(caps[method].memory).toBe(true);
  });
});
