// Which console experience the operator chose: the original console (v1) or
// the V2 console under /v2. Both ship side by side; the choice is a per-browser
// convenience, so a blocked or empty storage simply means "v1".
export type UiVersion = "v1" | "v2";

const KEY = "launchpad_ui_version";

export function getUiVersion(): UiVersion {
  try {
    return localStorage.getItem(KEY) === "v2" ? "v2" : "v1";
  } catch {
    return "v1";
  }
}

export function setUiVersion(version: UiVersion): void {
  try {
    localStorage.setItem(KEY, version);
  } catch {
    // storage unavailable (private window, blocked site data): the switch still
    // navigates, it just is not remembered
  }
}
