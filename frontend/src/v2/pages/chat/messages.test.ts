import { describe, expect, it } from "vitest";

import { appendDelta, type ChatMessage, nextVerdict, setVerdict, stampSaved } from "./messages";

const user = (text: string): ChatMessage => ({ kind: "user", text });

describe("stampSaved", () => {
  it("stamps the open answer before its closing event arrives", () => {
    let m: ChatMessage[] = [user("hi")];
    m = appendDelta(m, "Hel", false);
    m = appendDelta(m, "lo", true);
    m = stampSaved(m, 42);
    expect(m[1]).toEqual({ kind: "agent", text: "Hello", streaming: true, id: 42 });
  });

  it("reaches past an auth card asked mid-answer", () => {
    let m: ChatMessage[] = [user("hi"), { kind: "agent", text: "a", streaming: true }];
    m = [...m, { kind: "auth", text: "github", name: "repo" }];
    const next = stampSaved(m, 7);
    expect(next[1].id).toBe(7);
    expect(next[2]).toBe(m[2]);
  });

  it("gives each bubble a tool call split its own id", () => {
    let m: ChatMessage[] = [user("hi")];
    m = appendDelta(m, "first", false);
    m = stampSaved(m, 10); // saved, then the tool event closes the bubble
    m = [...m, { kind: "tool", text: "search", name: "search" }];
    m = appendDelta(m, "second", false);
    m = stampSaved(m, 12);
    expect(m.filter((x) => x.kind === "agent").map((x) => x.id)).toEqual([10, 12]);
  });

  it("leaves the thread alone without an open answer or when already stamped", () => {
    const noAnswer: ChatMessage[] = [user("hi"), { kind: "tool", text: "t", name: "t" }];
    expect(stampSaved(noAnswer, 3)).toBe(noAnswer);
    const stamped: ChatMessage[] = [user("hi"), { kind: "agent", text: "a", id: 1 }];
    expect(stampSaved(stamped, 2)).toBe(stamped);
    expect(stampSaved([], 2)).toEqual([]);
  });

  it("ignores a malformed id", () => {
    const m: ChatMessage[] = [{ kind: "agent", text: "a", streaming: true }];
    expect(stampSaved(m, undefined as unknown as number)).toBe(m);
  });
});

describe("thumbs verdicts", () => {
  it("toggles: same thumb clears, the other switches", () => {
    expect(nextVerdict(null, "up")).toBe("up");
    expect(nextVerdict(undefined, "down")).toBe("down");
    expect(nextVerdict("up", "up")).toBeNull();
    expect(nextVerdict("up", "down")).toBe("down");
  });

  it("sets the verdict by ledger id, and is a no-op for an id not in the thread", () => {
    const m: ChatMessage[] = [user("q"), { kind: "agent", text: "a", id: 5 }, { kind: "agent", text: "b", id: 6 }];
    const next = setVerdict(m, 6, "down");
    expect(next[2].verdict).toBe("down");
    expect(next[1]).toBe(m[1]);
    expect(setVerdict(next, 6, null)[2].verdict).toBeNull();
    expect(setVerdict(m, 99, "up")).toBe(m);
  });
});
