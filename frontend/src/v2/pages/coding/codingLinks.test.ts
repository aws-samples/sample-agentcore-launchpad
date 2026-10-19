import { describe, expect, it } from "vitest";

import { classifyCodingHref } from "./codingLinks";

const file = (path: string, line: number | null = null, column: number | null = null) => ({ kind: "file", path, line, column });
const blocked = (reason: string) => ({ kind: "blocked", reason });

describe("classifyCodingHref", () => {
  it("opens codex's absolute project links as project files", () => {
    expect(classifyCodingHref("/mnt/workspace/project/README.md", "")).toEqual(file("README.md"));
    expect(classifyCodingHref("/mnt/workspace/project/src/main.py", "docs")).toEqual(file("src/main.py"));
    expect(classifyCodingHref("file:///mnt/workspace/project/main.py", "")).toEqual(file("main.py"));
    expect(classifyCodingHref("file://localhost/mnt/workspace/project/main.py", "")).toEqual(file("main.py"));
  });

  it("strips a line suffix and keeps the line", () => {
    expect(classifyCodingHref("/mnt/workspace/project/main.py#L12", "")).toEqual(file("main.py", 12));
    expect(classifyCodingHref("/mnt/workspace/project/main.py#L12C3", "")).toEqual(file("main.py", 12, 3));
    expect(classifyCodingHref("/mnt/workspace/project/main.py#L12-L20", "")).toEqual(file("main.py", 12));
    expect(classifyCodingHref("/mnt/workspace/project/main.py:12", "")).toEqual(file("main.py", 12));
    expect(classifyCodingHref("/mnt/workspace/project/main.py:12:3", "")).toEqual(file("main.py", 12, 3));
    expect(classifyCodingHref("main.py:7", "")).toEqual(file("main.py", 7));
    expect(classifyCodingHref("src/main.py:7:2", "")).toEqual(file("src/main.py", 7, 2));
    // not a line: kept as a plain file link
    expect(classifyCodingHref("/mnt/workspace/project/README.md#usage", "")).toEqual(file("README.md"));
    expect(classifyCodingHref("/mnt/workspace/project/main.py:0", "")).toEqual(file("main.py"));
  });

  it("resolves relative links against the document's directory", () => {
    expect(classifyCodingHref("main.py", "")).toEqual(file("main.py"));
    expect(classifyCodingHref("main.py", ".")).toEqual(file("main.py"));
    expect(classifyCodingHref("./guide.md", "docs")).toEqual(file("docs/guide.md"));
    expect(classifyCodingHref("../README.md", "docs")).toEqual(file("README.md"));
    expect(classifyCodingHref("img/a.md?raw=1", "docs")).toEqual(file("docs/img/a.md"));
  });

  it("decodes percent-encoding per segment", () => {
    expect(classifyCodingHref("/mnt/workspace/project/my%20notes.md", "")).toEqual(file("my notes.md"));
    expect(classifyCodingHref("%E8%AF%B4%E6%98%8E.md", "")).toEqual(file("说明.md"));
    expect(classifyCodingHref("a%2F..%2F..%2Fx.md", "")).toEqual(blocked("malformed"));
    expect(classifyCodingHref("%E0%A4%A.md", "")).toEqual(blocked("malformed"));
  });

  it("opens http, https and mailto externally", () => {
    expect(classifyCodingHref("https://docs.aws.amazon.com/x", "")).toEqual({ kind: "external", href: "https://docs.aws.amazon.com/x" });
    expect(classifyCodingHref("HTTP://example.com", "")).toEqual({ kind: "external", href: "HTTP://example.com" });
    expect(classifyCodingHref("mailto:a@example.com", "")).toEqual({ kind: "external", href: "mailto:a@example.com" });
  });

  it("never follows other schemes", () => {
    expect(classifyCodingHref("javascript:alert(1)", "")).toEqual(blocked("scheme"));
    expect(classifyCodingHref("JavaScript:alert(1):12", "")).toEqual(blocked("scheme"));
    expect(classifyCodingHref("data:text/html,<b>x</b>", "")).toEqual(blocked("scheme"));
    expect(classifyCodingHref("vscode://file/mnt/workspace/project/a.py", "")).toEqual(blocked("scheme"));
    expect(classifyCodingHref("tel:12345", "")).toEqual(blocked("scheme"));
    expect(classifyCodingHref("file:relative.py", "")).toEqual(blocked("malformed"));
  });

  it("keeps links outside the project inert", () => {
    expect(classifyCodingHref("/mnt/workspace/.codex/config.toml", "")).toEqual(blocked("outside"));
    expect(classifyCodingHref("/etc/passwd", "")).toEqual(blocked("outside"));
    expect(classifyCodingHref("//evil.example/x", "")).toEqual(blocked("outside"));
    expect(classifyCodingHref("/mnt/workspace/project/../.launchpad/events.jsonl", "")).toEqual(blocked("outside"));
    expect(classifyCodingHref("../../x.md", "docs")).toEqual(blocked("outside"));
    expect(classifyCodingHref("~/x.md", "")).toEqual(blocked("outside"));
    expect(classifyCodingHref("file:///etc/passwd", "")).toEqual(blocked("outside"));
  });

  it("has nothing to open for anchors, the root, directories and junk", () => {
    expect(classifyCodingHref("#usage", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("?q=1", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("   ", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref(undefined, "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("/mnt/workspace/project", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("/mnt/workspace/project/src/", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref(".", "")).toEqual(blocked("empty"));
    expect(classifyCodingHref("..", "docs")).toEqual(blocked("empty"));
    expect(classifyCodingHref("a\\\\b.py", "")).toEqual(blocked("malformed"));
  });
});
