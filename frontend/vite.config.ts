import { execFileSync } from "node:child_process";
import { relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";
import { configDefaults } from "vitest/config";
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

/** N17-obs466: the commit this bundle is built from, compiled in as `__LAUNCHPAD_BUNDLE_SHA__`
 *  (`src/v2/buildSha.ts`) so an open tab can tell that the served `dist/` moved on. Only for
 *  `vite build`; null under the dev server and vitest (no banner there). `LAUNCHPAD_BUILD_SHA`
 *  wins (a container build); otherwise the checkout HEAD — the same commit host-update and
 *  bootstrap-host then write into `dist/.launchpad-build`. No git → null (no banner). */
function bundleSha(command: string): string | null {
  if (command !== "build" || process.env.VITEST) return null;
  const pinned = (process.env.LAUNCHPAD_BUILD_SHA ?? "").trim().toLowerCase();
  if (/^[0-9a-f]{7,64}$/.test(pinned)) return pinned;
  try {
    const head = execFileSync("git", ["rev-parse", "HEAD"], { cwd: frontendDir, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
    return /^[0-9a-f]{7,64}$/.test(head) ? head : null;
  } catch {
    return null;
  }
}

export default defineConfig(({ command, mode }) => {
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

  // The static copy of the vendored DCV SDK is for `vite` / `vite build` only: under
  // vitest nothing imports it, and the plugin's chokidar watcher over the SDK tree would
  // otherwise consume thousands of inotify watches (ENOSPC on a host whose budget an IDE
  // already holds) and fail `vitest run` before the first test.
  const underVitest = Boolean(process.env.VITEST);

  return {
    define: { __LAUNCHPAD_BUNDLE_SHA__: JSON.stringify(bundleSha(command)) },
    plugins: [
      react(),
      ...(underVitest ? [] : [viteStaticCopy({
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
          copySdkDirectory(
            // BrowserLiveView's fixed baseUrl is normalized by DCV to a relative
            // path. V2 needs its own workers too, especially HTTP's H264 fallback.
            resolve(dcvSdkDir, "dcvjs-esm"),
            "v2/governance/nice-dcv-web-client-sdk/dcvjs-esm",
          ),
        ],
      })]),
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
    test: {
      // Stryker (`make mutation-frontend`) copies the tree into .stryker-tmp/ while it runs; the
      // copies must never be collected as a second suite by `npm test`.
      exclude: [...configDefaults.exclude, "**/.stryker-tmp/**"],
      // N17-obs471: no restart-window retry (src/lib/restartRetry.ts) outlives its test
      setupFiles: ["./src/test-setup.ts"],
      // Under GitHub Actions (.github/workflows/cn-verify.yml) also write a junit report
      // the failure-artifact step uploads, and keep the per-test annotations. Locally the
      // reporter set is vitest's default; `npm test` itself is unchanged either way.
      reporters: process.env.GITHUB_ACTIONS
        ? ["default", "github-actions", ["junit", { outputFile: "vitest-junit.xml" }]]
        : ["default"],
    },
  };
});
