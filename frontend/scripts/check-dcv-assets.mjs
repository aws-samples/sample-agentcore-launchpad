/** Post-build regression: DCV resolves workers beneath the current SPA route.
 * A missing copy returns index.html with status 200, which importScripts rejects.
 * Check the built files through the real HTTP preview server, including MIME and bytes.
 */
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { preview } from "vite";

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = resolve(frontend, process.argv[2] ?? "dist");
const server = await preview({
  configFile: false,
  root: frontend,
  build: { outDir: dist },
  preview: { host: "127.0.0.1", port: 0, strictPort: true, open: false },
  logLevel: "silent",
});
try {
  const address = server.httpServer.address();
  assert(address && typeof address !== "string");
  const base = `http://127.0.0.1:${address.port}`;
  const sdk = "nice-dcv-web-client-sdk/dcvjs-esm/dcv";
  for (const worker of ["broadwayh264decoder-worker.js", "lz4decoder-worker.js"]) {
    const expected = await readFile(resolve(frontend, "node_modules/bedrock-agentcore/dist/src/tools/browser/live-view", sdk, worker));
    for (const route of ["", "governance/", "v2/governance/"]) {
      const path = `/${route}${sdk}/${worker}`;
      const response = await fetch(base + path);
      assert.equal(response.status, 200, path);
      assert.match(response.headers.get("content-type") ?? "", /(?:java|ecma)script/i,
        `${path}: a decoder worker must be JavaScript, never the SPA HTML fallback`);
      assert.deepEqual(Buffer.from(await response.arrayBuffer()), expected, `${path}: vendor worker bytes`);
    }
  }
  console.log("DCV assets: six root/classic/V2 worker URLs return JavaScript with matching vendor bytes");
} finally {
  server.httpServer.closeAllConnections();
  await new Promise((done, reject) => server.httpServer.close((error) => error ? reject(error) : done()));
}
