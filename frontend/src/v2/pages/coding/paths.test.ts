import { describe, expect, it } from "vitest";

import { displayPath, PROJECT_ROOT, projectPath } from "./paths";

describe("projectPath", () => {
  it("strips the project root from codex's absolute paths", () => {
    expect(projectPath("/mnt/workspace/project/README.md")).toBe("README.md");
    expect(projectPath("/mnt/workspace/project/src/app/main.py")).toBe("src/app/main.py");
    expect(projectPath("/mnt/workspace/project//src/./main.py")).toBe("src/main.py");
    expect(projectPath("/mnt/workspace/project/src/../main.py")).toBe("main.py");
    expect(projectPath(`${PROJECT_ROOT}/a b/ü.md`)).toBe("a b/ü.md");
  });

  it("leaves already-relative paths alone (codex's cwd is the project)", () => {
    expect(projectPath("README.md")).toBe("README.md");
    expect(projectPath("src/main.py")).toBe("src/main.py");
    expect(projectPath("./src/main.py")).toBe("src/main.py");
    expect(projectPath("src/../main.py")).toBe("main.py");
    expect(projectPath(".env")).toBe(".env");
  });

  it("returns null outside the project", () => {
    expect(projectPath("/tmp/x.txt")).toBeNull();
    expect(projectPath("/mnt/workspace/.codex/config.toml")).toBeNull();
    expect(projectPath("/mnt/workspace/project-other/a.py")).toBeNull();
    expect(projectPath("/mnt/workspace/projectx")).toBeNull();
    expect(projectPath("/mnt/workspace/project/../.launchpad/state.json")).toBeNull();
    expect(projectPath("../outside.py")).toBeNull();
    expect(projectPath("src/../../outside.py")).toBeNull();
    expect(projectPath("~/notes.md")).toBeNull();
    expect(projectPath("/../mnt/workspace/project/a.py")).toBeNull();
  });

  it("returns null for the root itself and for nothing", () => {
    expect(projectPath(PROJECT_ROOT)).toBeNull();
    expect(projectPath(`${PROJECT_ROOT}/`)).toBeNull();
    expect(projectPath(".")).toBeNull();
    expect(projectPath("")).toBeNull();
    expect(projectPath(null)).toBeNull();
    expect(projectPath(undefined)).toBeNull();
  });

  it("displays a project path relative and anything else as reported", () => {
    expect(displayPath("/mnt/workspace/project/main.py")).toBe("main.py");
    expect(displayPath("main.py")).toBe("main.py");
    expect(displayPath("/tmp/x.txt")).toBe("/tmp/x.txt");
  });
});
