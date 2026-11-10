import { useTranslation } from "react-i18next";

import { type ResourceState, resourceState } from "../../../lib/knowledgeBases";
import { awsStatusLabel } from "../../awsStatus";
import { Tag, type TagTone } from "../../ui";
import { KB_STATUS } from "./status";

/** KB lifecycle status → tag (translated); anything else renders raw. */
export function KbStatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  const meta = KB_STATUS[status];
  return (
    <Tag tone={meta?.tone ?? "gray"} dot>
      {meta ? t(meta.labelKey) : awsStatusLabel(t, status)}
    </Tag>
  );
}

const STATE_TONE: Record<ResourceState, TagTone> = {
  good: "green",
  crit: "red",
  warn: "blue",
  muted: "gray",
};

/** Data-source / ingestion-job / document status (raw AWS enum) → tag, worded through
 *  `v2.awsStatus.*` (N17-obs456; an unknown status stays raw); `label` replaces it where the
 *  console has its own copy (N17-obs274: a document's INDEXING). */
export function ResourceTag({ status, title, label }: { status: string; title?: string; label?: string }) {
  const { t } = useTranslation();
  return (
    <Tag tone={STATE_TONE[resourceState(status)]} dot title={title}>
      {label ?? awsStatusLabel(t, status)}
    </Tag>
  );
}
