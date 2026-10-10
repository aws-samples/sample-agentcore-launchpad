import { Plus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type V2LogService } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged } from "../../hooks";
import { MAX_LOG_GROUPS, SERVICE_NAME_RE } from "../../tasks";
import { Alert, Button, Field, LinkButton, Pager, SearchInput, Table, Tag } from "../../ui";

const SPANS_GROUP = "aws/spans";
const DISCOVERY_HOURS = 336;
const DEBOUNCE_MS = 400;

function LogGroupAdder({ taken, onAdd }: { taken: string[]; onAdd: (name: string) => void }) {
  const { t } = useTranslation();
  const [input, setInput] = useState("");
  const [q, setQ] = useState("");
  useEffect(() => {
    const timer = window.setTimeout(() => setQ(input.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [input]);
  const found = useLoad(() => (q ? api.v2LogGroups(q) : Promise.resolve(null)), `log-groups:${q}`);
  const options = (found.data?.log_groups ?? []).filter((g) => !taken.includes(g.name)).slice(0, 8);
  return (
    <div className="v2-stack" style={{ gap: 6 }}>
      <div className="v2-row" style={{ flexWrap: "nowrap" }}>
        <input
          className="v2-input"
          value={input}
          placeholder={t("v2.tasks.cw.groupSearch")}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            // an exact name can be added without waiting for the search
            if (e.key === "Enter" && input.trim()) {
              e.preventDefault();
              onAdd(input.trim());
              setInput("");
            }
          }}
          data-testid="v2-task-cw-group-q"
        />
      </div>
      {q && (
        <div className="v2-suggest">
          {found.loading ? (
            <span className="v2-muted">{t("v2.common.loading")}</span>
          ) : found.error ? (
            <span className="v2-muted">{found.error}</span>
          ) : options.length === 0 ? (
            <span className="v2-muted">{t("v2.tasks.cw.groupNone")}</span>
          ) : (
            options.map((g) => (
              <button
                type="button"
                key={g.name}
                className="mono"
                onClick={() => {
                  onAdd(g.name);
                  setInput("");
                }}
                data-testid="v2-task-cw-group-option"
              >
                <Plus size={12} aria-hidden="true" /> {g.name}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

/** Chosen log groups as removable tags + the search box that adds one (at most
 *  MAX_LOG_GROUPS) — shared by the task wizard and the 数据处理 运行日志 source. */
export function LogGroupsInput({ value, onChange }: { value: string[]; onChange: (groups: string[]) => void }) {
  const { t } = useTranslation();
  const add = (name: string) => {
    if (!value.includes(name) && value.length < MAX_LOG_GROUPS) onChange([...value, name]);
  };
  return (
    <div className="v2-stack" style={{ gap: 8 }}>
      {value.length > 0 && (
        <div className="v2-tags">
          {value.map((g) => (
            <Tag key={g} tone="outline">
              <span className="mono">{g}</span>
              <button type="button" className="v2-tag-x" aria-label={t("v2.common.delete")} onClick={() => onChange(value.filter((x) => x !== g))}>
                <X size={12} />
              </button>
            </Tag>
          ))}
        </div>
      )}
      {value.length < MAX_LOG_GROUPS && <LogGroupAdder taken={value} onAdd={add} />}
    </div>
  );
}

/**
 * 遥测数据源 of a task with no platform agent: the span `service.name` and the
 * input log groups (1–10) `StartBatchEvaluation` reads — spans (usually
 * `aws/spans`) plus the agent's content logs. The service list is discovered
 * from spans; picking one fills the log groups its resource names.
 */
export function LogSourceFields({
  serviceName,
  logGroups,
  onChange,
  onUseAgent,
}: {
  serviceName: string;
  logGroups: string[];
  onChange: (patch: { serviceName?: string; logGroups?: string[] }) => void;
  onUseAgent: (agentId: string) => void;
}) {
  const { t } = useTranslation();
  const extraGroups = logGroups.filter((g) => g !== SPANS_GROUP);
  const services = useLoad(() => api.v2LogServices(DISCOVERY_HOURS, extraGroups), `log-services:${extraGroups.join("|")}`);
  const [q, setQ] = useState("");
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (services.data?.services ?? []).filter((s) => !needle || s.service_name.toLowerCase().includes(needle));
  }, [services.data, q]);
  const paged = usePaged(rows, 5);
  const chosen = (services.data?.services ?? []).find((s) => s.service_name === serviceName) ?? null;
  const owned = chosen?.agent ?? null;
  const use = (s: V2LogService) => onChange({ serviceName: s.service_name, logGroups: s.log_group_names.slice(0, MAX_LOG_GROUPS) });
  const badName = serviceName.trim() !== "" && !SERVICE_NAME_RE.test(serviceName.trim());

  return (
    <>
      <div className="v2-form cols-2">
        <Field label={t("v2.tasks.cw.service")} required hint={t("v2.tasks.cw.serviceHint")} error={badName ? t("v2.tasks.cw.serviceInvalid") : null}>
          <input
            className="v2-input mono"
            value={serviceName}
            maxLength={256}
            placeholder="MyAgent.DEFAULT"
            onChange={(e) => onChange({ serviceName: e.target.value })}
            data-testid="v2-task-cw-service"
          />
        </Field>
        <Field label={t("v2.tasks.cw.groups", { count: logGroups.length, max: MAX_LOG_GROUPS })} required hint={t("v2.tasks.cw.groupsHint")}>
          <LogGroupsInput value={logGroups} onChange={(groups) => onChange({ logGroups: groups })} />
        </Field>
      </div>
      {chosen?.evaluable === false && (
        <div style={{ marginTop: 12 }}>
          <Alert tone="warn">{t("v2.tasks.cw.notEvaluable", { scopes: chosen.scopes.join(", ") })}</Alert>
        </div>
      )}
      {owned && (
        <div style={{ marginTop: 12 }}>
          <Alert action={<LinkButton onClick={() => onUseAgent(owned.id)}>{t("v2.tasks.cw.useAgent")}</LinkButton>}>
            {t("v2.tasks.cw.owned", { name: owned.name })}
          </Alert>
        </div>
      )}
      <h3 className="v2-sub-title">{t("v2.tasks.cw.discovered", { hours: DISCOVERY_HOURS })}</h3>
      <div className="v2-toolbar">
        <SearchInput value={q} onChange={setQ} placeholder={t("v2.tasks.cw.serviceSearch")} testId="v2-task-cw-service-q" />
        <Button onClick={services.reload}>{t("v2.common.refresh")}</Button>
      </div>
      <Table
        columns={[
          {
            key: "name",
            title: t("v2.tasks.cw.service"),
            render: (s: V2LogService) => (
              <>
                <span className="mono ellipsis" style={{ maxWidth: 320 }} title={s.service_name}>
                  {s.service_name}
                </span>
                <span className="sub mono ellipsis" style={{ maxWidth: 420 }} title={s.log_group_names.join(", ")}>
                  {s.log_group_names.join(", ")}
                </span>
              </>
            ),
          },
          {
            key: "agent",
            title: t("v2.tasks.cw.colAgent"),
            render: (s: V2LogService) => (s.agent ? <Tag tone="blue">{s.agent.name}</Tag> : <Tag tone="gray">{t("v2.tasks.cw.noAgent")}</Tag>),
          },
          {
            key: "scope",
            title: t("v2.tasks.cw.colScope"),
            render: (s: V2LogService) =>
              s.evaluable === false ? (
                <Tag tone="orange" title={s.scopes.join(", ")}>{t("v2.tasks.cw.scopeUnsupported")}</Tag>
              ) : s.evaluable ? (
                <Tag tone="green" title={s.scopes.join(", ")}>{t("v2.tasks.cw.scopeOk")}</Tag>
              ) : (
                <span className="v2-muted">—</span>
              ),
          },
          { key: "sessions", title: t("v2.taskDetail.sessions"), className: "num", render: (s: V2LogService) => s.sessions },
          { key: "last", title: t("v2.tasks.logs.colLast"), className: "nowrap", render: (s: V2LogService) => fmtTime(s.last_seen) },
          {
            key: "ops",
            title: t("v2.common.actions"),
            className: "right",
            render: (s: V2LogService) =>
              s.service_name === serviceName ? (
                <Tag tone="green">{t("v2.tasks.cw.picked")}</Tag>
              ) : (
                <LinkButton onClick={() => use(s)} testId="v2-task-cw-use">
                  {t("v2.tasks.cw.use")}
                </LinkButton>
              ),
          },
        ]}
        rows={paged.slice}
        rowKey={(s) => s.service_name}
        loading={services.loading}
        error={services.error}
        onRetry={services.reload}
        empty={t("v2.tasks.cw.noServices")}
        testId="v2-task-cw-services"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </>
  );
}
