import { useTranslation } from "react-i18next";

import type { RegistryRecordType } from "../../../lib/registry";
import { Tag, type TagTone } from "../../ui";
import { statusLabel, typeLabel } from "./common";

const STATUS_TONE: Record<string, TagTone> = {
  DRAFT: "gray",
  PENDING_APPROVAL: "orange",
  APPROVED: "green",
  REJECTED: "red",
  DEPRECATED: "gray",
};

export function StatusTag({ status }: { status: string }) {
  const { t } = useTranslation();
  return (
    <Tag tone={STATUS_TONE[status] ?? "gray"} dot>
      {statusLabel(t, status)}
    </Tag>
  );
}

const TYPE_TONE: Record<RegistryRecordType, TagTone> = {
  A2A: "orange",
  MCP: "blue",
  AGENT_SKILLS: "green",
};

export function TypeTag({ type }: { type: string }) {
  const { t } = useTranslation();
  return <Tag tone={TYPE_TONE[type as RegistryRecordType] ?? "gray"}>{typeLabel(t, type)}</Tag>;
}

const SOURCE_TONE: Record<string, TagTone> = { inline: "gray", zip: "blue", git: "orange", url: "green" };

export function SourceTag({ kind }: { kind: string }) {
  const { t } = useTranslation();
  return (
    <Tag tone={SOURCE_TONE[kind] ?? "gray"}>
      {t(`v2.registry.source.${kind}`, { defaultValue: kind })}
    </Tag>
  );
}

export function SystemTag() {
  const { t } = useTranslation();
  return (
    <Tag tone="blue" title={t("v2.registry.systemHint")}>
      {t("v2.registry.system")}
    </Tag>
  );
}
