import { describe, expect, it } from "vitest";

import { mayMutate } from "./ownership";

/** Mirror of `services/ownership.py`: creator-or-admin, NULL creator → administrator-only. */
describe("mayMutate", () => {
  const mine = { created_by: "u-ada", created_by_name: "ada" };
  const theirs = { created_by: "u-bob", created_by_name: "bob" };
  const legacy = { created_by: null, created_by_name: null };

  it("lets an administrator (and the open console's operator) mutate every row", () => {
    expect(mayMutate(mine, true, null)).toBe(true);
    expect(mayMutate(theirs, true, "u-ada")).toBe(true);
    expect(mayMutate(legacy, true, null)).toBe(true);
    expect(mayMutate(undefined, true, null)).toBe(true);
  });

  it("lets a member mutate only the rows their account created", () => {
    expect(mayMutate(mine, false, "u-ada")).toBe(true);
    expect(mayMutate(theirs, false, "u-ada")).toBe(false);
    expect(mayMutate(mine, false, null)).toBe(false);
  });

  it("treats a row with no recorded creator as an administrator's", () => {
    expect(mayMutate(legacy, false, "u-ada")).toBe(false);
    expect(mayMutate({}, false, "u-ada")).toBe(false);
    expect(mayMutate(null, false, "u-ada")).toBe(false);
  });
});
