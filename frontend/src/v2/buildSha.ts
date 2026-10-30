/** N17-obs466: the commit THIS bundle was built from — injected by `vite build`
 *  (`vite.config.ts` `define`: `LAUNCHPAD_BUILD_SHA`, else `git rev-parse HEAD`). null under
 *  the dev server and vitest, so a dev tab never offers a reload. */
declare const __LAUNCHPAD_BUNDLE_SHA__: string | null | undefined;

export const BUNDLE_SHA: string | null =
  typeof __LAUNCHPAD_BUNDLE_SHA__ === "string" && __LAUNCHPAD_BUNDLE_SHA__ ? __LAUNCHPAD_BUNDLE_SHA__ : null;

/** The shell reads the bundle sha through this (a test stands in another bundle). */
export function bundleSha(): string | null {
  return BUNDLE_SHA;
}

const SHA_RE = /^[0-9a-f]{7,64}$/;

export function cleanSha(sha: string | null | undefined): string | null {
  const text = (sha ?? "").trim().toLowerCase();
  return SHA_RE.test(text) ? text : null;
}

/** Both known and naming different commits (a short sha is a prefix of its full form). */
export function isNewVersion(bundleSha: string | null | undefined, servedSha: string | null | undefined): boolean {
  const mine = cleanSha(bundleSha);
  const served = cleanSha(servedSha);
  if (!mine || !served) return false;
  return !(mine.startsWith(served) || served.startsWith(mine));
}
