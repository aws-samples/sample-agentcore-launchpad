import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { UserGrantInfo, UserGrantStatus } from "./api";
import {
  AUTH_POLL_LIMIT_MS,
  AUTH_POLL_MS,
  authCardView,
  effectiveStatus,
  liveAuthAsk,
  pollGrant,
  restoredAuthAsk,
  retryPromptFor,
  revokeDisabled,
  runRevoke,
  safeAuthUrl,
} from "./user-grants";
import { chatPathFor, parseReturnParams } from "../v2/pages/authReturnParams";
import { appendDelta, type ChatMessage } from "../v2/pages/chat/messages";

const state = (status: UserGrantStatus, force_reauth = false) => ({ status, force_reauth });

describe("auth card polling", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("polls a live card until authorized, then flips and enables the retry", async () => {
    const check = vi
      .fn()
      .mockResolvedValueOnce(state("pending"))
      .mockResolvedValueOnce(state("pending"))
      .mockResolvedValue(state("authorized"));
    const seen: UserGrantStatus[] = [];
    const onExpired = vi.fn();
    pollGrant({ check, live: true, onStatus: (s) => seen.push(s), onExpired });

    await vi.advanceTimersByTimeAsync(0);
    expect(seen).toEqual(["pending"]);
    expect(authCardView({ live: true, status: "pending", expired: false, retryPrompt: "hi", retryDisabled: false }))
      .toMatchObject({ phase: "pending", showOpen: true, showRetry: false, retryEnabled: false });

    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS * 2);
    expect(seen).toEqual(["pending", "pending", "authorized"]);
    expect(authCardView({ live: true, status: "authorized", expired: false, retryPrompt: "hi", retryDisabled: false }))
      .toMatchObject({ phase: "authorized", showOpen: false, showRetry: true, retryEnabled: true });

    // stops once usable
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS * 4);
    expect(check).toHaveBeenCalledTimes(3);
    expect(onExpired).not.toHaveBeenCalled();
  });

  it("treats an authorized grant with a revocation in force as still pending", async () => {
    expect(effectiveStatus(state("authorized", true))).toBe("pending");
    const check = vi.fn().mockResolvedValueOnce(state("authorized", true)).mockResolvedValue(state("authorized"));
    const seen: UserGrantStatus[] = [];
    pollGrant({ check, live: true, onStatus: (s) => seen.push(s), onExpired: vi.fn() });
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS);
    expect(seen).toEqual(["pending", "authorized"]);
  });

  it("keeps waiting through a failed read and expires at the session limit", async () => {
    const check = vi.fn().mockRejectedValueOnce(new Error("blip")).mockResolvedValue(state("pending"));
    const onExpired = vi.fn();
    pollGrant({ check, live: true, onStatus: vi.fn(), onExpired });
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS);
    expect(check).toHaveBeenCalledTimes(2);
    await vi.advanceTimersByTimeAsync(AUTH_POLL_LIMIT_MS);
    expect(onExpired).toHaveBeenCalledOnce();
    const calls = check.mock.calls.length;
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS * 3);
    expect(check.mock.calls.length).toBe(calls);
  });

  it("stops polling when cancelled (unmount)", async () => {
    const check = vi.fn().mockResolvedValue(state("pending"));
    const cancel = pollGrant({ check, live: true, onStatus: vi.fn(), onExpired: vi.fn() });
    await vi.advanceTimersByTimeAsync(0);
    cancel();
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS * 5);
    expect(check).toHaveBeenCalledOnce();
  });

  it("checks a restored card once", async () => {
    const check = vi.fn().mockResolvedValue(state("none"));
    pollGrant({ check, live: false, onStatus: vi.fn(), onExpired: vi.fn() });
    await vi.advanceTimersByTimeAsync(AUTH_POLL_MS * 5);
    expect(check).toHaveBeenCalledOnce();
  });
});

describe("retry from restored history", () => {
  const history = [
    { kind: "user", text: "first question" },
    { kind: "agent", text: "answer" },
    { kind: "user", text: "what is on my calendar?" },
    { kind: "tool", text: "calendar" },
    { kind: "auth", text: "google" },
  ];

  it("re-sends the user turn that triggered the ask", () => {
    expect(retryPromptFor(history, 4)).toBe("what is on my calendar?");
    expect(retryPromptFor([{ kind: "auth", text: "google" }], 0)).toBeNull();
    expect(retryPromptFor([{ kind: "user", text: "  " }, { kind: "auth", text: "g" }], 1)).toBeNull();
  });

  it("restores a URL-less card whose retry is enabled when a prompt exists", () => {
    const ask = restoredAuthAsk({ text: "google", name: "calendar" }, "agent-1");
    expect(ask).toEqual({ provider: "google", tool: "calendar", scopes: [], url: null, agent_id: "agent-1" });
    const live = ask.url !== null;
    expect(authCardView({ live, status: "none", expired: false, retryPrompt: retryPromptFor(history, 4), retryDisabled: false }))
      .toMatchObject({ phase: "restored", showOpen: false, showRetry: true, retryEnabled: true });
    // busy chat or no prompt: shown but disabled
    expect(authCardView({ live, status: null, expired: false, retryPrompt: "x", retryDisabled: true }).retryEnabled).toBe(false);
    expect(authCardView({ live, status: null, expired: false, retryPrompt: null, retryDisabled: false }).retryEnabled).toBe(false);
  });

  it("builds a live ask from the SSE event, defaulting the agent", () => {
    expect(liveAuthAsk({ provider: "google", url: "https://idp/authorize?x" }, "agent-1")).toEqual({
      provider: "google",
      tool: "",
      scopes: [],
      url: "https://idp/authorize?x",
      agent_id: "agent-1",
    });
  });
});

describe("authorization URL is https-only", () => {
  it("keeps an absolute https URL", () => {
    expect(safeAuthUrl("https://idp.example.com/authorize?state=x")).toBe("https://idp.example.com/authorize?state=x");
    expect(safeAuthUrl("  https://idp.example.com/a  ")).toBe("https://idp.example.com/a");
  });

  it.each([
    "javascript:alert(document.cookie)",
    "JavaScript:alert(1)",
    " javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "http://idp.example.com/authorize",
    "//idp.example.com/authorize",
    "/relative/authorize",
    "not a url",
    "",
  ])("drops %j", (url) => {
    expect(safeAuthUrl(url)).toBeNull();
  });

  it("drops non-string values", () => {
    expect(safeAuthUrl(undefined)).toBeNull();
    expect(safeAuthUrl(null)).toBeNull();
    expect(safeAuthUrl(42)).toBeNull();
  });

  it("turns a live ask with an unsafe URL into the no-URL (retry-only) card", () => {
    const ask = liveAuthAsk({ provider: "google", url: "javascript:alert(1)" }, "agent-1");
    expect(ask.url).toBeNull();
    expect(authCardView({ live: ask.url !== null, status: null, expired: false, retryPrompt: "hi", retryDisabled: false }))
      .toMatchObject({ phase: "restored", showOpen: false, showRetry: true });
    expect(liveAuthAsk({ provider: "google", url: "http://idp/authorize" }, "agent-1").url).toBeNull();
  });

  it("never carries a URL on a restored history row, whatever the row holds", () => {
    const row = { text: "google", name: "calendar", url: "javascript:alert(1)" };
    expect(restoredAuthAsk(row, "agent-1").url).toBeNull();
    const httpsRow = { ...row, url: "https://idp/authorize" };
    expect(restoredAuthAsk(httpsRow, "agent-1").url).toBeNull();
  });
});

describe("revoke", () => {
  const grant: UserGrantInfo = {
    connection: "google",
    agent_id: "a1",
    agent_name: "Helper",
    tool: "calendar",
    scopes: ["openid"],
    status: "authorized",
    force_reauth: false,
    created_at: null,
    updated_at: null,
    authorized_at: null,
    revoked_at: null,
  };

  it("is busy while the call runs, then closes and reloads", async () => {
    const busy: (string | null)[] = [];
    let release!: (v: { provider: string }) => void;
    const revoke = vi.fn(() => new Promise<{ provider: string }>((r) => (release = r)));
    const onDone = vi.fn();
    const onError = vi.fn();
    const run = runRevoke("google", { revoke, setBusy: (c) => busy.push(c), onDone, onError });
    expect(busy).toEqual(["google"]);
    expect(revokeDisabled(grant, true, "google")).toBe(true);
    release({ provider: "google" });
    await expect(run).resolves.toBe(true);
    expect(revoke).toHaveBeenCalledWith("google");
    expect(onDone).toHaveBeenCalledWith("google");
    expect(onError).not.toHaveBeenCalled();
    expect(busy).toEqual(["google", null]);
  });

  it("reports a failure and clears busy without closing", async () => {
    const busy: (string | null)[] = [];
    const onDone = vi.fn();
    const onError = vi.fn();
    const ok = await runRevoke("google", {
      revoke: () => Promise.reject(new Error("denied")),
      setBusy: (c) => busy.push(c),
      onDone,
      onError,
    });
    expect(ok).toBe(false);
    expect(onDone).not.toHaveBeenCalled();
    expect(onError).toHaveBeenCalledOnce();
    expect(busy).toEqual(["google", null]);
  });

  it("disables revoke without the permission or once already in force", () => {
    expect(revokeDisabled(grant, true, null)).toBe(false);
    expect(revokeDisabled(grant, false, null)).toBe(true);
    expect(revokeDisabled({ ...grant, status: "revoked", force_reauth: true }, true, null)).toBe(true);
    expect(revokeDisabled({ ...grant, force_reauth: true }, true, null)).toBe(false);
  });
});

describe("/auth/return params", () => {
  it("reads the session uri and the customState way back", () => {
    const state = JSON.stringify({ agent_id: "a1", tool: "calendar", session_id: "s1" });
    const ret = parseReturnParams(`?session_id=urn%3Asess&state=${encodeURIComponent(state)}`);
    expect(ret).toEqual({ sessionUri: "urn:sess", agentId: "a1", tool: "calendar", chatSession: "s1" });
    expect(chatPathFor(ret)).toBe("/v2/chat?agent=a1&session=s1");
  });

  it("survives a missing or mangled state", () => {
    expect(parseReturnParams("?session_id=x&state=%7Bbad")).toEqual({
      sessionUri: "x",
      agentId: null,
      tool: null,
      chatSession: null,
    });
    expect(parseReturnParams("").sessionUri).toBeNull();
    expect(chatPathFor(parseReturnParams(""))).toBe("/v2/chat");
  });
});

describe("streamed answer around an auth card", () => {
  const card: ChatMessage = { kind: "auth", text: "team-idp", name: "userinfo" };

  it("keeps the deltas before and after the card in one bubble", () => {
    let m: ChatMessage[] = [{ kind: "user", text: "hi" }];
    m = appendDelta(m, "看", false);
    m = [...m, card];
    m = appendDelta(m, "起来您尚未完成授权", true);
    expect(m.map((x) => x.kind)).toEqual(["user", "agent", "auth"]);
    expect(m[1]).toMatchObject({ text: "看起来您尚未完成授权", streaming: true });
  });

  it("opens a new bubble after a tool event closed the previous one", () => {
    let m = appendDelta([{ kind: "user", text: "hi" }], "a", false);
    m = [...m, { kind: "tool", text: "t", name: "t" }];
    m = appendDelta(m, "b", false);
    expect(m.map((x) => x.kind)).toEqual(["user", "agent", "tool", "agent"]);
  });

  it("never appends to a finished bubble from an earlier turn", () => {
    const m = appendDelta([{ kind: "agent", text: "old" }, card], "new", true);
    expect(m.map((x) => x.text)).toEqual(["old", "team-idp", "new"]);
  });
});
