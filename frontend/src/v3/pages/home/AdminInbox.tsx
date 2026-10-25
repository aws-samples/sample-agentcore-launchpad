import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { InboxInfo } from "../../../lib/api";
import { Lamp, Panel } from "../../ui";
import { inboxSignal } from "./modules";

/**
 * The administrator's inbox (V2 Workbench T22): pending registrations, expiring
 * accounts, unfinished workspaces, failed jobs, releases awaiting review, agents never
 * evaluated. Every row links to where it gets handled (a server-provided path). The
 * caller renders it for administrators only; it renders nothing while loading, on a
 * failed read (the modules it points at surface their own problems) or when empty.
 */
export function AdminInbox({ inbox }: { inbox: InboxInfo | null }) {
  const { t } = useTranslation();
  const items = inbox?.items ?? [];
  if (items.length === 0) return null;
  return (
    <div data-testid="v3-home-inbox">
      <Panel title={t("v2.inbox.title")} end={t("v2.inbox.sub", { count: inbox?.total ?? 0 })} flush>
        <table className="v3-table">
          <tbody>
            {items.map((item) => {
              const s = inboxSignal(item.severity);
              return (
                <tr key={item.key} data-testid={`v3-inbox-${item.key}`}>
                  <td style={{ width: 30 }}><Lamp s={s} title={t(`v2.inbox.severity.${item.severity}`)} /></td>
                  <td className="num" style={{ width: 48, fontWeight: 600 }}>{item.count}</td>
                  <td>
                    <div>{t(`v2.inbox.item.${item.key}`, { count: item.count })}</div>
                    {item.sample.length > 0 && (
                      <div className="mono" style={{ color: "var(--v3-text-3)", fontSize: 12 }}>{item.sample.join(" · ")}</div>
                    )}
                  </td>
                  <td style={{ width: 120, textAlign: "right" }}>
                    <Link to={item.to} className="v3-btn sm">
                      {t("v2.inbox.open")} <ArrowRight size={13} aria-hidden="true" />
                    </Link>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </Panel>
    </div>
  );
}
