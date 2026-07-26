import { useTranslation } from "react-i18next";

/** Token-driven "load more" footer — renders nothing once AWS stops paginating. */
export function LoadMore({ token, onClick }: { token: string | null; onClick: () => void }) {
  const { t } = useTranslation();
  if (!token) return null;
  return (
    <div className="pagerbar">
      <span className="spacer" />
      <button className="fsel" onClick={onClick}>
        {t("memoryPage.loadMore")} ›
      </button>
    </div>
  );
}
