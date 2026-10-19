import { describe, expect, it } from "vitest";

import { isMarkdown, languageFor } from "./language";

describe("languageFor", () => {
  it("maps extensions to Monaco languages", () => {
    expect(languageFor("main.py")).toBe("python");
    expect(languageFor("src/app.tsx")).toBe("typescript");
    expect(languageFor("server.mjs")).toBe("javascript");
    expect(languageFor("pyproject.toml")).toBe("ini");
    expect(languageFor("config/app.YAML")).toBe("yaml");
    expect(languageFor("run.sh")).toBe("shell");
    expect(languageFor("data.json")).toBe("json");
  });

  it("maps well-known file names", () => {
    expect(languageFor("Dockerfile")).toBe("dockerfile");
    expect(languageFor("deploy/Dockerfile.prod")).toBe("dockerfile");
    expect(languageFor(".gitignore")).toBe("plaintext");
    expect(languageFor("requirements.txt")).toBe("plaintext");
    expect(languageFor(".env")).toBe("ini");
  });

  it("falls back to plain text", () => {
    expect(languageFor("LICENSE")).toBe("plaintext");
    expect(languageFor("archive.unknownext")).toBe("plaintext");
    expect(languageFor(".bashrc")).toBe("plaintext");
  });

  it("detects Markdown", () => {
    expect(isMarkdown("README.md")).toBe(true);
    expect(isMarkdown("docs/guide.markdown")).toBe(true);
    expect(isMarkdown("AGENTS.MD")).toBe(true);
    expect(isMarkdown("main.py")).toBe(false);
  });
});
