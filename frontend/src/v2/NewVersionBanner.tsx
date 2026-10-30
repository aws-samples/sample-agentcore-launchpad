import { RefreshCw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { cleanSha, isNewVersion } from "./buildSha";
import { Button } from "./ui";

/**
 * N17-obs466 (user request 2026-10-26): "a new version is available" for an open tab. The
 * console compares the sha its bundle was built with (`./buildSha`) with the sha the SERVED
 * `dist/` was built from (`/api/health` `build.dist_sha` — not `build.sha`, which moves on a
 * backend-only update while the bundle stays). It rides the shell's existing health probe
 * (`./useHealth`: on mount, every 30 s, on focus / online), so it costs no extra request.
 * Non-blocking, and it NEVER reloads by itself — an unsaved editor would be lost; 「稍后」
 * hides it for that served version in this tab. Derived on every render, so it also goes
 * away when the two re-converge (the seconds between a rebuild and its stamp).
 */

const DISMISS_KEY = "launchpad.newVersion.dismissed";

function readDismissed(): string | null {
  try {
    return sessionStorage.getItem(DISMISS_KEY);
  } catch {
    return null;
  }
}

export function NewVersionBanner({
  bundleSha,
  servedSha,
  reload = () => window.location.reload(),
}: {
  bundleSha: string | null;
  servedSha: string | null | undefined;
  reload?: () => void;
}) {
  const { t } = useTranslation();
  const [dismissed, setDismissed] = useState<string | null>(readDismissed);
  const served = cleanSha(servedSha);
  if (!served || !isNewVersion(bundleSha, served) || dismissed === served) return null;
  const later = () => {
    setDismissed(served);
    try {
      sessionStorage.setItem(DISMISS_KEY, served);
    } catch {
      // per-tab convenience only
    }
  };
  return (
    <div className="v2-new-version" role="status" data-testid="v2-new-version" data-sha={served}>
      <RefreshCw size={14} aria-hidden="true" />
      <span>{t("v2.newVersion.text", { sha: served.slice(0, 8) })}</span>
      <span className="v2-new-version-actions">
        <Button size="sm" kind="primary" onClick={reload} testId="v2-new-version-reload">
          {t("v2.newVersion.reload")}
        </Button>
        <Button size="sm" onClick={later} testId="v2-new-version-later">
          {t("v2.newVersion.later")}
        </Button>
      </span>
    </div>
  );
}
