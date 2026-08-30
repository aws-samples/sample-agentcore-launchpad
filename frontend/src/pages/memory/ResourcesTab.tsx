import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, DataTable, Panel, useToast } from "../../components";
import type { MemoryResourceCreateInput, MemoryResourceRow } from "../../lib/api";
import { api } from "../../lib/api";
import { shortId, stamp, statusTone } from "./format";

/** CreateMemory's own name constraint — checked here so the button can gate. */
const NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** Order matters: it is the order sent to CreateMemory and shown as chips. */
const STRATEGY_KEYS = ["semantic", "user_preference", "summarization", "episodic"] as const;
const DEFAULT_STRATEGIES = ["semantic", "user_preference"];

/**
 * Memory resource management — create and manage AgentCore Memory resources.
 *
 * The workspace's bootstrap memory is the delete-protected default; additional
 * memories created here become selectable per agent in the Create wizard
 * (`spec.memory.memory_id`). Status is read live from AWS: a new memory shows
 * CREATING for a few minutes before extraction strategies become ACTIVE.
 */
export function ResourcesTab() {
  const { t } = useTranslation();
  const toast = useToast();

  const [rows, setRows] = useState<MemoryResourceRow[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [expiryDays, setExpiryDays] = useState(30);
  const [strategies, setStrategies] = useState<string[]>(DEFAULT_STRATEGIES);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<MemoryResourceRow | null>(null);

  const load = useCallback(() => {
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    api
      .memoryResources()
      .then((res) => {
        if (id !== seq.current) return;
        setRows(res.items);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== seq.current) return;
        setError(err instanceof Error ? err.message : String(err));
        setLoading(false);
      });
  }, []);

  useEffect(load, [load]);

  const nameValid = NAME_RE.test(name);
  const expiryValid = Number.isInteger(expiryDays) && expiryDays >= 3 && expiryDays <= 365;

  const toggleStrategy = (key: string) => {
    setStrategies((prev) =>
      prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key],
    );
  };

  const create = () => {
    if (!nameValid || !expiryValid || creating) return;
    const input: MemoryResourceCreateInput = {
      name,
      description: description.trim(),
      event_expiry_days: expiryDays,
      // keep the platform order — the backend maps keys onto CreateMemory
      strategies: STRATEGY_KEYS.filter((k) => strategies.includes(k)),
    };
    setCreating(true);
    api
      .memoryResourceCreate(input)
      .then((created) => {
        toast(t("memoryPage.resources.created", { id: created.id ?? name }), "good");
        setName("");
        setDescription("");
        setExpiryDays(30);
        setStrategies(DEFAULT_STRATEGIES);
        load();
      })
      .catch((err: unknown) => {
        toast(
          t("memoryPage.resources.createFailed", {
            msg: err instanceof Error ? err.message : String(err),
          }),
          "crit",
        );
      })
      .finally(() => setCreating(false));
  };

  const remove = (row: MemoryResourceRow) => {
    if (!row.id) return;
    api
      .memoryResourceDelete(row.id)
      .then(() => {
        toast(t("memoryPage.resources.deleted", { id: row.id }), "good");
        load();
      })
      .catch((err: unknown) => {
        toast(
          t("memoryPage.resources.deleteFailed", {
            msg: err instanceof Error ? err.message : String(err),
          }),
          "crit",
        );
      });
  };

  return (
    <>
      <div className="grid-2">
        <Panel
          title={t("memoryPage.resources.listTitle")}
          sub={t("memoryPage.resources.listSub")}
          end={
            <button className="refresh" onClick={load}>
              ⟳ {t("memoryPage.refresh")}
            </button>
          }
          pad={false}
        >
          {error ? (
            <div className="empty">{error}</div>
          ) : (
            <DataTable
              columns={[
                { key: "name", label: t("memoryPage.resources.colName") },
                { key: "status", label: t("memoryPage.resources.colStatus") },
                { key: "agents", label: t("memoryPage.resources.colAgents") },
                { key: "created", label: t("memoryPage.resources.colCreated") },
                { key: "actions", label: "" },
              ]}
              isEmpty={!rows || rows.length === 0}
              empty={loading ? t("common.loading") : t("memoryPage.resources.empty")}
            >
              {(rows ?? []).map((m) => (
                <tr key={m.id ?? m.arn} className={m.is_default ? "sel" : undefined}>
                  <td>
                    <div>
                      <b>{m.name ?? "—"}</b>{" "}
                      {m.is_default && (
                        <Chip tone="amber">{t("memoryPage.resources.defaultBadge")}</Chip>
                      )}
                    </div>
                    <div className="mono dim" title={m.arn ?? ""}>
                      {shortId(m.id, 18)}
                    </div>
                  </td>
                  <td>
                    <Chip tone={statusTone(m.status)}>{m.status ?? "—"}</Chip>
                  </td>
                  <td title={m.agents.map((a) => a.name).join(", ")}>
                    {m.is_default
                      ? t("memoryPage.resources.sharedDefault")
                      : t("memoryPage.resources.agentCount", { count: m.agents.length })}
                  </td>
                  <td className="mono">{stamp(m.created_at)}</td>
                  <td>
                    {!m.is_default && (
                      <Btn
                        onClick={() => setPendingDelete(m)}
                        disabled={m.agents.length > 0}
                        title={
                          m.agents.length > 0
                            ? t("memoryPage.resources.inUseHint")
                            : undefined
                        }
                      >
                        {t("memoryPage.resources.delete")}
                      </Btn>
                    )}
                  </td>
                </tr>
              ))}
            </DataTable>
          )}
        </Panel>

        <Panel
          title={t("memoryPage.resources.createTitle")}
          sub={t("memoryPage.resources.createSub")}
        >
          <div className="field">
            <label htmlFor="mem-res-name">{t("memoryPage.resources.name")}</label>
            <input
              id="mem-res-name"
              className="input mono"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="team_notes"
              data-testid="memory-resource-name"
            />
            {name.length > 0 && !nameValid && (
              <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                {t("memoryPage.resources.nameInvalid")}
              </div>
            )}
          </div>
          <div className="field">
            <label htmlFor="mem-res-desc">{t("memoryPage.resources.description")}</label>
            <textarea
              id="mem-res-desc"
              className="input"
              style={{ minHeight: 64, resize: "vertical" }}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("memoryPage.resources.descriptionPlaceholder")}
            />
          </div>
          <div className="field">
            <label htmlFor="mem-res-expiry">{t("memoryPage.resources.expiry")}</label>
            <input
              id="mem-res-expiry"
              className="input mono"
              type="number"
              min={3}
              max={365}
              value={expiryDays}
              onChange={(e) => setExpiryDays(Number(e.target.value))}
              style={{ width: 120 }}
            />
            <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
              {t("memoryPage.resources.expiryHint")}
            </div>
          </div>
          <div className="field">
            <label>{t("memoryPage.resources.strategies")}</label>
            <div className="selchips">
              {STRATEGY_KEYS.map((key) => (
                <button
                  key={key}
                  type="button"
                  className={`selchip${strategies.includes(key) ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => toggleStrategy(key)}
                >
                  {t(`memoryPage.resources.strategy.${key}`)}{" "}
                  {strategies.includes(key) ? "✓" : "+"}
                </button>
              ))}
            </div>
            <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
              {t("memoryPage.resources.strategiesHint")}
            </div>
          </div>
          <Btn
            primary
            onClick={create}
            disabled={!nameValid || !expiryValid || creating}
            data-testid="memory-resource-create"
          >
            {creating
              ? t("memoryPage.resources.creating")
              : t("memoryPage.resources.create")}
          </Btn>
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("memoryPage.resources.note")}</span>
          </div>
        </Panel>
      </div>

      <ConfirmDialog
        open={pendingDelete !== null}
        title={t("memoryPage.resources.deleteTitle")}
        body={t("memoryPage.resources.deleteBody", {
          id: pendingDelete?.id ?? "",
        })}
        confirmLabel={t("memoryPage.resources.delete")}
        onConfirm={() => {
          if (pendingDelete) remove(pendingDelete);
          setPendingDelete(null);
        }}
        onCancel={() => setPendingDelete(null)}
      />
    </>
  );
}
