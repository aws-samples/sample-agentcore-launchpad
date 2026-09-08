import { relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { viteStaticCopy } from "vite-plugin-static-copy";

const frontendDir = fileURLToPath(new URL(".", import.meta.url));
const dcvSdkDir = resolve(
  frontendDir,
  "node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk",
);

function copySdkDirectory(src: string, dest: string) {
  const stripBase = relative(frontendDir, src)
    .split(/[/\\]+/)
    .filter(Boolean).length;
  return { src, dest, rename: { stripBase } };
}

export default defineConfig(({ mode }) => {
  // LAUNCHPAD_API lets a worktree/dev instance point at its own backend port
  const env = loadEnv(mode, ".", "LAUNCHPAD_");
  const apiTarget = env.LAUNCHPAD_API ?? "http://localhost:8000";
  const proxy = {
    "/api": apiTarget,
    // the Chat page's "equivalent API call" snippet targets window.location.origin
    // (console and public API share an origin in prod) — without this the copied
    // curl hits vite and 404s before the backend can check the API key
    "/v1": apiTarget,
  };

  return {
    plugins: [
      react(),
      viteStaticCopy({
        targets: [
          copySdkDirectory(
            resolve(dcvSdkDir, "dcvjs-esm"),
            "nice-dcv-web-client-sdk/dcvjs-esm",
          ),
          copySdkDirectory(
            resolve(dcvSdkDir, "dcv-ui"),
            "nice-dcv-web-client-sdk/dcv-ui",
          ),
          copySdkDirectory(
            // DCV resolves decoder workers relative to the current SPA route.
            resolve(dcvSdkDir, "dcvjs-esm"),
            "governance/nice-dcv-web-client-sdk/dcvjs-esm",
          ),
        ],
      }),
    ],
    resolve: {
      alias: {
        dcv: resolve(dcvSdkDir, "dcvjs-esm/dcv.js"),
        "dcv-ui": resolve(dcvSdkDir, "dcv-ui/dcv-ui.js"),
      },
      dedupe: [
        "react",
        "react-dom",
        "prop-types",
        "@cloudscape-design/components",
        "@cloudscape-design/global-styles",
        "@cloudscape-design/design-tokens",
        "@babel/runtime",
      ],
    },
    build: {
      // Only the vendored NICE DCV live-view SDK is above the 500 kB default,
      // and it is already behind a `React.lazy` boundary (governance/ToolsView)
      // — nothing but the Governance browser demo ever downloads it. 2900 kB
      // covers that one chunk and nothing more: the entry chunk (~600 kB once
      // the route table went lazy, App.tsx) and every page chunk stay an order
      // of magnitude below it, so a regression that inflates them still trips
      // this warning. Raise it only if the DCV SDK itself grows.
      chunkSizeWarningLimit: 2900,
    },
    server: {
      proxy,
    },
    preview: {
      proxy,
    },
  };
});
