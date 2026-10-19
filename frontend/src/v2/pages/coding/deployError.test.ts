import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import { deployFailureText } from "./deployError";

const t = (key: string, options?: Record<string, unknown>) => (options ? `${key} ${JSON.stringify(options)}` : key);

const failed = (detail: unknown) => new ApiError("coding.deploy_failed", "Deploying the project failed.", detail);

describe("deployFailureText", () => {
  it("names the missing entrypoint and the root Python files", () => {
    expect(deployFailureText(failed({ reason: "entrypoint_missing", entrypoint: "app.py", candidates: ["main.py", "x.py"] }), t)).toBe(
      'v2.coding.deploy.failure.entrypoint_missing {"entrypoint":"app.py"} v2.coding.deploy.failure.candidates {"files":"main.py, x.py"}',
    );
    expect(deployFailureText(failed({ reason: "entrypoint_missing", entrypoint: "app.py", candidates: [] }), t)).toBe(
      'v2.coding.deploy.failure.entrypoint_missing {"entrypoint":"app.py"}',
    );
  });

  it("explains a missing Dockerfile and an invalid spec", () => {
    expect(deployFailureText(failed({ reason: "dockerfile_missing" }), t)).toBe("v2.coding.deploy.failure.dockerfile_missing");
    expect(deployFailureText(failed({ reason: "invalid_spec", errors: ["a", "b"] }), t)).toBe(
      'v2.coding.deploy.failure.invalid_spec {"errors":"a; b"}',
    );
  });

  it("keeps the shared localized message otherwise", () => {
    expect(deployFailureText(failed(null), t)).toBe("Deploying the project failed.");
    expect(deployFailureText(failed({ reason: "invalid_spec", errors: [] }), t)).toBe("Deploying the project failed.");
    expect(deployFailureText(new ApiError("agent.name_exists", "An agent with this name already exists.", {}), t)).toBe(
      "An agent with this name already exists.",
    );
    expect(deployFailureText(new Error("boom"), t)).toBe("boom");
  });
});
