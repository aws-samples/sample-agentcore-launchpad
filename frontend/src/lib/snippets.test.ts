import { describe, expect, it } from "vitest";

import {
  buildSnippet,
  invokeUrl,
  KEY_PLACEHOLDER,
  shQuote,
  SNIPPET_LANGS,
  SNIPPET_MODES,
  strLiteral,
} from "./snippets";

const ORIGIN = "https://launchpad.example.com";
const BASE = { baseUrl: ORIGIN, agentId: "agent-123" };
const HOSTILE_ID = `a b'"\`$(rm -rf ~);\\x\n`;

/** Minimal POSIX tokenizer for the one shape shQuote emits: '...' runs joined by \'. */
function unShQuote(token: string): string {
  let out = "";
  let i = 0;
  while (i < token.length) {
    if (token[i] === "'") {
      const end = token.indexOf("'", i + 1);
      if (end < 0) throw new Error(`unterminated quote in ${token}`);
      out += token.slice(i + 1, end);
      i = end + 1;
    } else if (token[i] === "\\" && i + 1 < token.length) {
      out += token[i + 1];
      i += 2;
    } else {
      throw new Error(`unquoted ${JSON.stringify(token[i])} in ${token}`);
    }
  }
  return out;
}

/** Parses (never runs) a snippet as the body of an async function. */
function parsesAsJs(source: string): void {
  const AsyncFunction = Object.getPrototypeOf(async () => undefined).constructor as new (
    ...args: string[]
  ) => unknown;
  new AsyncFunction(source);
}

describe("invokeUrl", () => {
  it("targets the /v1 invoke and invoke-stream routes", () => {
    expect(invokeUrl(BASE, "sync")).toBe(`${ORIGIN}/v1/agents/agent-123/invoke`);
    expect(invokeUrl(BASE, "stream")).toBe(`${ORIGIN}/v1/agents/agent-123/invoke-stream`);
  });

  it("uses the origin it is given and drops trailing slashes", () => {
    expect(invokeUrl({ baseUrl: "http://localhost:5173//", agentId: "x" }, "sync")).toBe(
      "http://localhost:5173/v1/agents/x/invoke",
    );
  });

  it("percent-encodes the agent id as one path segment, quote characters included", () => {
    const url = invokeUrl({ baseUrl: ORIGIN, agentId: "a/b c'(d)*!" }, "sync");
    expect(url).toBe(`${ORIGIN}/v1/agents/a%2Fb%20c%27%28d%29%2A%21/invoke`);
  });
});

describe("quoting helpers", () => {
  it("shQuote round-trips anything, single quotes included", () => {
    for (const s of ["plain", "it's", `'"$\`\\`, "", "''"]) {
      expect(unShQuote(shQuote(s))).toBe(s);
    }
  });

  it("strLiteral is a JS string literal of the same value", () => {
    for (const s of [`a"b`, "back\\slash", "new\nline", "\u2028"]) {
      expect(new Function(`return ${strLiteral(s)};`)()).toBe(s);
    }
  });
});

describe("buildSnippet", () => {
  it("curl: POSTs JSON with the X-Api-Key placeholder, -N only when streaming", () => {
    const sync = buildSnippet(BASE, "curl", "sync");
    expect(sync).toContain(`curl -X POST '${ORIGIN}/v1/agents/agent-123/invoke' \\`);
    expect(sync).toContain(`-H 'X-Api-Key: ${KEY_PLACEHOLDER}'`);
    expect(sync).toContain("-H 'Content-Type: application/json'");
    expect(sync).toContain(`-d '{"prompt": "Hello!"}'`);
    const stream = buildSnippet(BASE, "curl", "stream");
    expect(stream).toContain(`curl -N -X POST '${ORIGIN}/v1/agents/agent-123/invoke-stream' \\`);
  });

  it("python: httpx post reads `text`; stream prints delta text and raises on error", () => {
    const sync = buildSnippet(BASE, "python", "sync");
    expect(sync).toContain("import httpx");
    expect(sync).toContain(`    "${ORIGIN}/v1/agents/agent-123/invoke",`);
    expect(sync).toContain(`headers={"X-Api-Key": "${KEY_PLACEHOLDER}"}`);
    expect(sync).toContain(`json={"prompt": "Hello!"}`);
    expect(sync).toContain(`print(resp.json()["text"])`);
    const stream = buildSnippet(BASE, "python", "stream");
    expect(stream).toContain("with httpx.stream(");
    expect(stream).toContain(`    "${ORIGIN}/v1/agents/agent-123/invoke-stream",`);
    expect(stream).toContain(`if event == "delta":`);
    expect(stream).toContain(`print(data["text"], end="", flush=True)`);
    expect(stream).toContain(`elif event == "error":`);
  });

  it("javascript: fetch reads `text`; stream parses SSE frames", () => {
    const sync = buildSnippet(BASE, "javascript", "sync");
    expect(sync).toContain(`await fetch("${ORIGIN}/v1/agents/agent-123/invoke", {`);
    expect(sync).toContain(`"X-Api-Key": "${KEY_PLACEHOLDER}"`);
    expect(sync).toContain("const { text } = await resp.json();");
    const stream = buildSnippet(BASE, "javascript", "stream");
    expect(stream).toContain(`await fetch("${ORIGIN}/v1/agents/agent-123/invoke-stream", {`);
    expect(stream).toContain(`buffer.split("\\n\\n")`);
    expect(stream).toContain(`if (event === "delta") process.stdout.write(payload.text);`);
  });

  it("every JavaScript snippet is syntactically valid", () => {
    for (const mode of SNIPPET_MODES) {
      expect(() => parsesAsJs(buildSnippet(BASE, "javascript", mode))).not.toThrow();
    }
  });

  it("a hostile agent id or origin cannot break out of any language's quoting", () => {
    const hostileOrigin = `http://x"';$(id)\\`;
    const input = { baseUrl: hostileOrigin, agentId: HOSTILE_ID };
    for (const mode of SNIPPET_MODES) {
      const url = invokeUrl(input, mode);
      // the id itself is fully percent-encoded
      expect(url.slice(hostileOrigin.length)).toMatch(/^\/v1\/agents\/[A-Za-z0-9%._~-]+\/invoke/);

      const curlLine = buildSnippet(input, "curl", mode).split("\n")[0];
      const urlToken = curlLine.replace(/^curl (-N )?-X POST /, "").replace(/ \\$/, "");
      expect(unShQuote(urlToken)).toBe(url);

      const pyLine = buildSnippet(input, "python", mode)
        .split("\n")
        .find((l) => l.trim().startsWith(`"http`));
      expect(JSON.parse(pyLine!.trim().replace(/,$/, ""))).toBe(url);

      const js = buildSnippet(input, "javascript", mode);
      expect(() => parsesAsJs(js)).not.toThrow();
      const jsUrl = js.split("\n")[0].replace(/^const resp = await fetch\(/, "").replace(/, \{$/, "");
      expect(JSON.parse(jsUrl)).toBe(url);
    }
  });

  it("covers every language × mode without throwing and never embeds a real key", () => {
    for (const lang of SNIPPET_LANGS) {
      for (const mode of SNIPPET_MODES) {
        const s = buildSnippet(BASE, lang, mode);
        expect(s).toContain(KEY_PLACEHOLDER);
        expect(s).not.toMatch(/lp_live_/);
      }
    }
  });
});
