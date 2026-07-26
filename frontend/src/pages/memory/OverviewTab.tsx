import { useTranslation } from "react-i18next";

import { Chip, DataTable, Panel, StatTile } from "../../components";
import type { MemoryOverview } from "../../lib/api";
import { shortId, stamp, statusTone } from "./format";

function Kv({ k, v, title }: { k: string; v: string; title?: string }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v" title={title ?? v}>
        {v}
      </span>
    </div>
  );
}

/** Memory resource configuration + the long-term strategies that drive extraction. */
export function OverviewTab({ overview }: { overview: MemoryOverview }) {
  const { t } = useTranslation();
  const mem = overview.memory;
  if (!mem) return null;

  return (
    <>
      <div className="tiles">
        <StatTile
          label={t("memoryPage.overview.actorsTile")}
          value={overview.actor_count}
          foot={
            overview.actor_count_truncated
              ? t("memoryPage.overview.actorsTruncated")
              : t("memoryPage.overview.actorsExact")
          }
        />
        <StatTile
          label={t("memoryPage.overview.strategiesTile")}
          value={overview.strategies.length}
          foot={t("memoryPage.overview.strategiesFoot")}
        />
        <StatTile
          label={t("memoryPage.overview.expiryTile")}
          value={mem.event_expiry_days ?? "—"}
          unit={mem.event_expiry_days != null ? t("memoryPage.overview.days") : undefined}
          foot={t("memoryPage.overview.expiryFoot")}
        />
        <StatTile
          label={t("memoryPage.overview.statusTile")}
          value={mem.status ?? "—"}
          foot={mem.failure_reason ?? t("memoryPage.overview.statusFoot")}
        />
      </div>

      <div className="grid-2">
        <Panel
          title={t("memoryPage.overview.resourceTitle")}
          sub={mem.name ?? undefined}
          end={<Chip tone={statusTone(mem.status)}>{mem.status ?? "—"}</Chip>}
        >
          <div className="gov-kv-list">
            <Kv k={t("memoryPage.overview.id")} v={mem.id} />
            <Kv
              k={t("memoryPage.overview.arn")}
              v={shortId(mem.arn, 18)}
              title={mem.arn ?? ""}
            />
            <Kv k={t("memoryPage.overview.description")} v={mem.description || "—"} />
            <Kv
              k={t("memoryPage.overview.expiry")}
              v={
                mem.event_expiry_days != null
                  ? t("memoryPage.overview.daysValue", { count: mem.event_expiry_days })
                  : "—"
              }
            />
            <Kv
              k={t("memoryPage.overview.encryption")}
              v={mem.encryption_key_arn ? shortId(mem.encryption_key_arn, 14) : t("memoryPage.overview.awsManagedKey")}
              title={mem.encryption_key_arn ?? ""}
            />
            <Kv
              k={t("memoryPage.overview.executionRole")}
              v={shortId(mem.execution_role_arn, 16)}
              title={mem.execution_role_arn ?? ""}
            />
            <Kv k={t("memoryPage.overview.createdAt")} v={stamp(mem.created_at)} />
            <Kv k={t("memoryPage.overview.updatedAt")} v={stamp(mem.updated_at)} />
            {mem.failure_reason && (
              <Kv k={t("memoryPage.overview.failureReason")} v={mem.failure_reason} />
            )}
          </div>
        </Panel>

        <Panel
          title={t("memoryPage.overview.strategiesTitle")}
          sub={t("memoryPage.overview.strategiesSub")}
        >
          {overview.strategies.length === 0 ? (
            <div className="empty">{t("memoryPage.overview.noStrategies")}</div>
          ) : (
            overview.strategies.map((s) => (
              <div key={s.strategy_id ?? s.name} className="mem-strategy">
                <div className="mem-strategy-head">
                  <b>{s.name ?? "—"}</b>
                  <Chip tone="blue">{s.type ?? "—"}</Chip>
                  <Chip tone={statusTone(s.status)}>{s.status ?? "—"}</Chip>
                </div>
                <div className="mono dim">{s.strategy_id ?? "—"}</div>
                {(s.namespace_templates.length ? s.namespace_templates : s.namespaces).map(
                  (ns) => (
                    <div key={ns} className="mono mem-ns">
                      {ns}
                    </div>
                  ),
                )}
                {s.description && <p className="sub">{s.description}</p>}
              </div>
            ))
          )}
        </Panel>
      </div>

      <Panel
        title={t("memoryPage.overview.siblingsTitle")}
        sub={t("memoryPage.overview.siblingsSub")}
        pad={false}
      >
        <DataTable
          columns={[
            { key: "id", label: t("memoryPage.overview.colMemoryId") },
            { key: "status", label: t("memoryPage.overview.colStatus") },
            { key: "created", label: t("memoryPage.overview.colCreated") },
            { key: "role", label: t("memoryPage.overview.colRole") },
          ]}
          isEmpty={overview.other_memories.length === 0}
          empty={t("memoryPage.overview.noSiblings")}
        >
          {overview.other_memories.map((m) => (
            <tr key={m.id ?? m.arn} className={m.is_platform ? "sel" : undefined}>
              <td className="mono" title={m.arn ?? ""}>
                {m.id ?? "—"}
              </td>
              <td>
                <Chip tone={statusTone(m.status)}>{m.status ?? "—"}</Chip>
              </td>
              <td className="mono">{stamp(m.created_at)}</td>
              <td>
                {m.is_platform ? (
                  <Chip tone="amber">{t("memoryPage.overview.platformMemory")}</Chip>
                ) : (
                  <span className="dim">{t("memoryPage.overview.externalMemory")}</span>
                )}
              </td>
            </tr>
          ))}
        </DataTable>
      </Panel>
    </>
  );
}
