import { useTranslation } from "react-i18next";

import { Tag } from "../../ui";
import { govTone } from "./common";
import { awsStatusLabel } from "../../awsStatus";

/** An AWS status / mode string as a toned tag; "—" when absent. */
export function StatusTag({ status, title }: { status: string | null | undefined; title?: string }) {
  const { t } = useTranslation();
  if (!status) return <span className="v2-muted">—</span>;
  // N17-obs457: gateway / target / rate-limit statuses read in the console language
  return (
    <Tag tone={govTone(status)} title={title}>
      {awsStatusLabel(t, status)}
    </Tag>
  );
}

/** Pretty JSON block. */
export function JsonBlock({ value }: { value: unknown }) {
  return <pre className="v2-pre">{JSON.stringify(value ?? null, null, 2)}</pre>;
}
