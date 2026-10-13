/**
 * Copy-ready `/v1` integration snippets for one agent.
 *
 * Built client-side on purpose: the inputs are the agent id and the origin the
 * console is served from, both already in the browser, and the output is static
 * text. A server route would only add an endpoint to classify and keep in step
 * with `backend/app/routers/public_api.py`.
 *
 * The contract the snippets mirror (public_api.py):
 * - `POST /v1/agents/{id}/invoke`, JSON body `{prompt, session_id?, actor_id?}`,
 *   returns `{agent, text, session_id, latency_ms}`;
 * - `POST /v1/agents/{id}/invoke-stream`, same body, returns SSE frames
 *   `event: <name>\ndata: <json>\n\n` with `meta`, `delta` (`{text}`), `tool`,
 *   `error` and `done` events, plus `: keep-alive` comments;
 * - auth is the `X-Api-Key` header.
 *
 * Every interpolated value goes through a per-language quoting helper, so an odd
 * origin or agent id can never break out of its string literal.
 */

export type SnippetLang = "curl" | "python" | "javascript";
export type SnippetMode = "sync" | "stream";

export const KEY_PLACEHOLDER = "YOUR_API_KEY";

export interface SnippetInput {
  /** origin serving `/v1` (e.g. `window.location.origin`) */
  baseUrl: string;
  agentId: string;
  /** placeholder shown for the key (never a real key) */
  key?: string;
}

export const SNIPPET_LANGS: SnippetLang[] = ["curl", "python", "javascript"];
export const SNIPPET_MODES: SnippetMode[] = ["sync", "stream"];

/** One path segment, strictly percent-encoded: `encodeURIComponent` leaves
 *  `!'()*` alone, and `'` would otherwise end a shell single-quoted string. */
function pathSegment(value: string): string {
  return encodeURIComponent(value).replace(
    /[!'()*]/g,
    (c) => `%${c.charCodeAt(0).toString(16).toUpperCase()}`,
  );
}

export function invokeUrl(input: SnippetInput, mode: SnippetMode): string {
  const root = input.baseUrl.replace(/\/+$/, "");
  const action = mode === "sync" ? "invoke" : "invoke-stream";
  return `${root}/v1/agents/${pathSegment(input.agentId)}/${action}`;
}

/** POSIX shell single-quoted literal. */
export function shQuote(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

/** A JSON string literal is also a valid Python and JavaScript string literal. */
export function strLiteral(value: string): string {
  return JSON.stringify(value);
}

export function buildSnippet(input: SnippetInput, lang: SnippetLang, mode: SnippetMode): string {
  const url = invokeUrl(input, mode);
  const key = input.key ?? KEY_PLACEHOLDER;
  if (lang === "curl") {
    return [
      `curl ${mode === "stream" ? "-N " : ""}-X POST ${shQuote(url)} \\`,
      `  -H ${shQuote(`X-Api-Key: ${key}`)} \\`,
      `  -H 'Content-Type: application/json' \\`,
      `  -d '{"prompt": "Hello!"}'`,
    ].join("\n");
  }
  const u = strLiteral(url);
  const k = strLiteral(key);
  if (lang === "python") {
    if (mode === "sync") {
      return [
        "import httpx",
        "",
        "resp = httpx.post(",
        `    ${u},`,
        `    headers={"X-Api-Key": ${k}},`,
        `    json={"prompt": "Hello!"},`,
        "    timeout=300,",
        ")",
        "resp.raise_for_status()",
        `print(resp.json()["text"])`,
      ].join("\n");
    }
    return [
      "import json",
      "import httpx",
      "",
      "with httpx.stream(",
      `    "POST",`,
      `    ${u},`,
      `    headers={"X-Api-Key": ${k}},`,
      `    json={"prompt": "Hello!"},`,
      "    timeout=None,",
      ") as resp:",
      "    resp.raise_for_status()",
      "    event = None",
      "    for line in resp.iter_lines():",
      `        if line.startswith("event: "):`,
      `            event = line[len("event: "):]`,
      `        elif line.startswith("data: "):`,
      `            data = json.loads(line[len("data: "):])`,
      `            if event == "delta":`,
      `                print(data["text"], end="", flush=True)`,
      `            elif event == "error":`,
      "                raise RuntimeError(data)",
    ].join("\n");
  }
  const request = [
    `const resp = await fetch(${u}, {`,
    `  method: "POST",`,
    `  headers: { "X-Api-Key": ${k}, "Content-Type": "application/json" },`,
    `  body: JSON.stringify({ prompt: "Hello!" }),`,
    "});",
    "if (!resp.ok) throw new Error(await resp.text());",
  ];
  if (mode === "sync") {
    return [...request, "const { text } = await resp.json();", "console.log(text);"].join("\n");
  }
  return [
    ...request,
    "",
    "const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();",
    `let buffer = "";`,
    "for (;;) {",
    "  const { value, done } = await reader.read();",
    "  if (done) break;",
    "  buffer += value;",
    `  const frames = buffer.split("\\n\\n");`,
    `  buffer = frames.pop() ?? "";`,
    "  for (const frame of frames) {",
    `    const lines = frame.split("\\n");`,
    `    const event = lines.find((l) => l.startsWith("event: "))?.slice(7);`,
    `    const data = lines.find((l) => l.startsWith("data: "));`,
    "    if (!data) continue;",
    "    const payload = JSON.parse(data.slice(6));",
    `    if (event === "delta") process.stdout.write(payload.text);`,
    `    else if (event === "error") throw new Error(JSON.stringify(payload));`,
    "  }",
    "}",
  ].join("\n");
}
