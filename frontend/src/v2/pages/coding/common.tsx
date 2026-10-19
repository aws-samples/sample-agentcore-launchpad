import { useTranslation } from "react-i18next";

import type { CodingProjectStatus } from "../../../lib/api";
import { Tag } from "../../ui";
import type { TurnStatus } from "./codingEvents";

export function ProjectStatusTag({ status }: { status: CodingProjectStatus }) {
  const { t } = useTranslation();
  return (
    <Tag tone={status === "running" ? "blue" : "gray"} dot={status === "running"} testId="v2-coding-project-status">
      {t(`v2.coding.status.${status}`)}
    </Tag>
  );
}

const TURN_TONE = {
  inProgress: "blue",
  completed: "green",
  interrupted: "orange",
  failed: "red",
  abandoned: "red",
} as const;

export function TurnStatusTag({ status }: { status: TurnStatus }) {
  const { t } = useTranslation();
  return (
    <Tag tone={TURN_TONE[status]} dot={status === "inProgress"}>
      {t(`v2.coding.turnStatus.${status}`)}
    </Tag>
  );
}
