/**
 * The `X-Workspace` request header: storage of the selection, and the global
 * `window.fetch` wrapper that stamps it on every backend call.
 *
 * Wrapping `fetch` rather than the typed client is deliberate: most console
 * pages call `fetch("/api/...")` directly (the SSE chat turn, the studio debug
 * client, `useHealth`, the registry/evaluation/knowledge surfaces), each with
 * its own `res.ok` error handling. One wrapper reaches all of them, including
 * the streams, without rewriting those branches.
 */

export const WORKSPACE_STORAGE_KEY = "launchpad_workspace";
export const WORKSPACE_HEADER = "X-Workspace";

/** Workspace ids the backend can mint (`routers/workspaces.py::_SLUG`) — the
 * same 2-32 characters, so a tampered storage value cannot make every request
 * carry a header the backend would refuse. */
const WORKSPACE_ID = /^[a-z0-9][a-z0-9-]{1,31}$/;

/** The selection is a UI preference, not a credential — a private-mode window
 * that refuses storage simply falls back to the backend's default. */
export function storedWorkspaceId(): string | null {
  try {
    const raw = window.localStorage.getItem(WORKSPACE_STORAGE_KEY);
    return raw && WORKSPACE_ID.test(raw) ? raw : null;
  } catch {
    return null;
  }
}

export function storeWorkspaceId(id: string): void {
  try {
    window.localStorage.setItem(WORKSPACE_STORAGE_KEY, id);
  } catch {
    /* storage unavailable — the header is dropped, not the request */
  }
}

function backendPath(input: RequestInfo | URL): string | null {
  const raw =
    typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  let url: URL;
  try {
    url = new URL(raw, window.location.href);
  } catch {
    return null;
  }
  if (url.origin !== window.location.origin) return null;
  const scoped = url.pathname.startsWith("/api/") || url.pathname.startsWith("/v1/");
  return scoped ? url.pathname : null;
}

let installed = false;

/**
 * Install the wrapper. Must run before the app renders so no page's mount
 * effect can fire an unstamped request.
 */
export function installWorkspaceHeader(): void {
  if (installed) return; // HMR / a second import must not wrap the wrapper
  installed = true;
  const original = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    if (backendPath(input) === null) return original(input, init);
    // Console API responses are live state and must never be satisfied from the
    // HTTP cache. The backend sends no caching headers, so normally nothing is
    // cached — but whatever once answered on this origin (a port forward that
    // briefly pointed elsewhere, a static preview server on the same port) can
    // leave a heuristically-fresh 200 under an /api URL, and the browser then
    // serves that entry to every fetch() without touching the network.
    const stamped: RequestInit = { ...init, cache: init?.cache ?? "no-store" };
    const workspaceId = storedWorkspaceId();
    if (!workspaceId) return original(input, stamped);
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    );
    // A caller that named a workspace means it (the Workspaces page polls the
    // bootstrap job of a workspace that is not the current selection).
    if (!headers.has(WORKSPACE_HEADER)) headers.set(WORKSPACE_HEADER, workspaceId);
    return original(input, { ...stamped, headers });
  };
}
