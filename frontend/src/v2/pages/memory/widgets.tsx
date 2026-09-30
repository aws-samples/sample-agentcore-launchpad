import { useTranslation } from "react-i18next";

import { Button } from "../../ui";

/** Token-driven "load more" footer — renders nothing once AWS stops paginating. */
export function LoadMore({
  token,
  loading,
  onClick,
  testId,
}: {
  token: string | null;
  loading: boolean;
  onClick: () => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  if (!token) return null;
  return (
    <div className="v2-memory-more">
      <Button size="sm" disabled={loading} onClick={onClick} testId={testId}>
        {loading ? t("v2.common.loading") : t("v2.memory.loadMore")}
      </Button>
    </div>
  );
}
