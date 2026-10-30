import { describe, expect, it } from "vitest";
import { BOOTSTRAP_STAGES } from "./workspaces";

// Copied verbatim from backend `app/services/workspace_bootstrap.py::STAGE_ORDER`
// (CN build: `model-credential` (C4), `memory` (the self-hosted Memory/KB substrate,
// ruling R2), `skill-lab`, `policy` (D20); no cognito stage). This hand-kept fixture
// lagged the backend by two stages until round 16 (N16-obs50) — the differential that
// reads the backend file is `backend/tests/consistency/test_surface_consistency.py`;
// this pin only guards the frontend constant against an accidental edit.
const BACKEND_STAGE_ORDER = [
  "validate-access",
  "iam",
  "model-credential",
  "storage",
  "codebuild",
  "gateway",
  "registry",
  "memory",
  "skill-lab",
  "observability",
  "policy",
  "finalize",
];

describe("workspace bootstrap stages (spec C4)", () => {
  it("mirrors the backend STAGE_ORDER exactly", () => {
    expect(BOOTSTRAP_STAGES).toEqual(BACKEND_STAGE_ORDER);
  });
});
