import { Loader2 } from "lucide-react";
import { useTranslation } from "react-i18next";

/**
 * N17-obs470 (live 2026-10-26): during a deploy the backend is down for ~11 s. While the
 * shell's health probe fails (`useHealth`, every 3 s while down) this non-blocking notice
 * says the console is updating — the page and any unsaved form stay as they are, and a GET
 * the page makes meanwhile retries by itself (`lib/api.ts`). Once health answers on a new
 * build the `NewVersionBanner` takes over.
 */
export function RestartingNotice({ down }: { down: boolean }) {
  const { t } = useTranslation();
  if (!down) return null;
  return (
    <div className="v2-new-version restarting" role="status" data-testid="v2-restarting">
      <Loader2 size={14} aria-hidden="true" />
      <span>{t("v2.restarting.text")}</span>
    </div>
  );
}
