/**
 * `/login` (and `/register`) are not console pages: the sign-in form is drawn by
 * `AuthGate` over whatever URL the browser opened, so a signed-in caller who is
 * still on `/login` — a bookmark, a link from an e-mail, the address the form was
 * reached on — is sent on to where they meant to go.
 *
 * The target comes from `?next=` (or `?redirect=`) when it is a same-origin console
 * path; anything else (an absolute URL, a protocol-relative `//host`, a backslash
 * trick, another auth path) falls back to `/`, whose index route then honours the
 * remembered console choice (V2 / V3 / classic).
 */
export const AUTH_PATHS = ["/login", "/register"] as const;

function isAuthPath(path: string): boolean {
  const bare = path.split(/[?#]/, 1)[0].replace(/\/+$/, "") || "/";
  return (AUTH_PATHS as readonly string[]).includes(bare);
}

/** The same-origin path a signed-in visitor of `/login?<search>` is sent to. */
export function loginRedirectTarget(search: string): string {
  const params = new URLSearchParams(search);
  const raw = (params.get("next") ?? params.get("redirect") ?? "").trim();
  if (!raw.startsWith("/")) return "/";
  // `//evil.example` and `/\evil.example` are protocol-relative in browsers
  if (raw.startsWith("//") || raw.startsWith("/\\")) return "/";
  // control characters never belong in a console path
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f]/.test(raw)) return "/";
  if (isAuthPath(raw)) return "/";
  return raw;
}
