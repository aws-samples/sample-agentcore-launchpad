import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, DataTable, Panel, useToast } from "../../components";
import type {
  MemoryNamespaceKeyInput,
  MemoryResourceCreateInput,
  MemoryResourceDetail,
  MemoryResourceRow,
  MemoryResourceUpdateInput,
} from "../../lib/api";
import { api } from "../../lib/api";
import { shortId, stamp, statusTone } from "./format";

/** CreateMemory's own name constraint — checked here so the button can gate. */
const NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** Order matters: it is the order sent to CreateMemory and shown as chips. */
const STRATEGY_KEYS = ["semantic", "user_preference", "summarization", "episodic"] as const;
const DEFAULT_STRATEGIES = ["semantic", "user_preference"];

/** The namespace each strategy pick actually gets — mirrors the backend's
 *  canned layout (`services/memory_admin.py` STRATEGIES), used for the
 *  namespace path preview. */
const STRATEGY_NAMESPACES: Record<(typeof STRATEGY_KEYS)[number], string> = {
  semantic: "/facts/{actorId}",
  user_preference: "/preferences/{actorId}",
  summarization: "/summaries/{actorId}/{sessionId}",
  episodic: "/episodes/{actorId}/{sessionId}",
};

/** Flexible namespace variables — CreateMemory `namespaceKeys` constraints,
 *  mirrored here so bad input gates the button instead of failing server-side. */
const NS_KEY_RE = /^[a-z][a-z0-9]{0,31}$/;
const NS_VALUE_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const NS_MAX_KEYS = 5;
const NS_MAX_VALUES = 10;
const NS_MAX_REGEX = 64;

/** Namespace-variables editor visibility. Hidden for now: the platform's
 *  canned strategy templates don't reference custom keys and the console's
 *  invoke path never supplies `extractionConfig.namespaceVariables` on
 *  CreateEvent, so keys defined here would only ever be pre-registered for
 *  externally managed strategies. Flip to true (the backend's
 *  `POST /api/memory/resources` still accepts `namespace_keys`) once the
 *  platform can wire keys into templates and supply values at event time. */
const SHOW_NS_KEYS: boolean = false;

/** One editable namespace-key row (allowed values kept comma-separated). */
interface NsKeyDraft {
  key: string;
  allowedValues: string;
  regexPattern: string;
}

const EMPTY_NS_ROW: NsKeyDraft = { key: "", allowedValues: "", regexPattern: "" };

/** UpdateMemory's event-expiry range — the edit form gates on it client-side. */
const EDIT_EXPIRY_MIN = 7;
const EXPIRY_MAX = 365;

/** Description textarea shared by the create form and the inline edit form. */
function DescriptionField({
  id,
  value,
  onChange,
  disabled = false,
}: {
  id: string;
  value: string;
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="field">
      <label htmlFor={id}>{t("memoryPage.resources.description")}</label>
      <textarea
        id={id}
        className="input"
        style={{ minHeight: 64, resize: "vertical" }}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("memoryPage.resources.descriptionPlaceholder")}
        disabled={disabled}
      />
    </div>
  );
}

/** Event-expiry (days) input shared by the create form and the inline edit form;
 *  the caller supplies the lower bound and the hint text because CreateMemory
 *  accepts 3 days while the edit form holds the UpdateMemory floor of 7. */
function ExpiryField({
  id,
  value,
  min,
  hint,
  onChange,
  disabled = false,
}: {
  id: string;
  value: number;
  min: number;
  hint: string;
  onChange: (value: number) => void;
  disabled?: boolean;
}) {
  const { t } = useTranslation();
  return (
    <div className="field">
      <label htmlFor={id}>{t("memoryPage.resources.expiry")}</label>
      <input
        id={id}
        className="input mono"
        type="number"
        min={min}
        max={EXPIRY_MAX}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        style={{ width: 120 }}
        disabled={disabled}
      />
      <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
        {hint}
      </div>
    </div>
  );
}

/** The row being edited inline plus its draft values. `detail` stays null until
 *  `GET /api/memory/resources/{id}` has answered — list rows carry neither the
 *  description nor the expiry (ListMemories is a summary), so the form is
 *  pre-filled from the detail read and disabled until it lands. */
interface EditDraft {
  row: MemoryResourceRow;
  detail: MemoryResourceDetail | null;
  description: string;
  expiryDays: number;
}

/**
 * Memory resource management — create, edit and manage AgentCore Memory resources.
 *
 * The workspace's bootstrap memory is the delete-protected default; additional
 * memories created here become selectable per agent in the Create wizard
 * (`spec.memory.memory_id`). Status is read live from AWS: a new memory shows
 * CREATING for a few minutes before extraction strategies become ACTIVE.
 *
 * EDIT on a row opens an inline form for the description and the short-term
 * event expiry only (UpdateMemory); strategies, namespace variables and the
 * execution role are fixed at creation. Editing is never blocked by referencing
 * agents — the confirm dialog names them instead, since a shorter expiry window
 * reaches every agent writing to the memory.
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
  const [nsKeys, setNsKeys] = useState<NsKeyDraft[]>([]);
  const [creating, setCreating] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<MemoryResourceRow | null>(null);
  const [edit, setEdit] = useState<EditDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirmSave, setConfirmSave] = useState(false);

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

  // namespace-key drafts → the API shape (empty pieces dropped)
  const nsParsed = nsKeys.map((row) => ({
    key: row.key.trim(),
    allowed: row.allowedValues
      .split(",")
      .map((v) => v.trim())
      .filter(Boolean),
    regex: row.regexPattern.trim(),
  }));
  const nsValid =
    nsParsed.every(
      (row) =>
        NS_KEY_RE.test(row.key) &&
        row.allowed.length <= NS_MAX_VALUES &&
        row.allowed.every((v) => NS_VALUE_RE.test(v)) &&
        row.regex.length <= NS_MAX_REGEX,
    ) && new Set(nsParsed.map((r) => r.key)).size === nsParsed.length;

  const setNsKey = (index: number, patch: Partial<NsKeyDraft>) => {
    setNsKeys((prev) => prev.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  };

  // Live namespace path preview, one entry per selected long-term strategy:
  // the custom `/key/{key}/…` pairs prefix the strategy's actual canned
  // namespace. Illustrative — the created strategies keep the platform layout
  // and do not reference the custom keys (see nsHint). The example line
  // substitutes each variable with its first allowed value when one is given,
  // and leaves `{key}` unresolved otherwise — mirroring how the service
  // resolves `extractionConfig.namespaceVariables` at extraction time.
  const nsPreviewRows = nsParsed.filter((row) => row.key);
  const nsSelectedStrategies = STRATEGY_KEYS.filter((k) => strategies.includes(k));
  const nsPrefixTemplate = nsPreviewRows.map((row) => `/${row.key}/{${row.key}}`).join("");
  const nsPrefixExample = nsPreviewRows
    .map((row) => `/${row.key}/${row.allowed[0] ?? `{${row.key}}`}`)
    .join("");
  const nsResolveBuiltins = (ns: string) =>
    ns.replace("{actorId}", "user-123").replace("{sessionId}", "session-456");

  const create = () => {
    if (!nameValid || !expiryValid || !nsValid || creating) return;
    const input: MemoryResourceCreateInput = {
      name,
      description: description.trim(),
      event_expiry_days: expiryDays,
      // keep the platform order — the backend maps keys onto CreateMemory
      strategies: STRATEGY_KEYS.filter((k) => strategies.includes(k)),
    };
    if (nsParsed.length > 0) {
      input.namespace_keys = nsParsed.map((row) => {
        const entry: MemoryNamespaceKeyInput = { key: row.key };
        if (row.allowed.length > 0) entry.allowed_values = row.allowed;
        if (row.regex) entry.regex_pattern = row.regex;
        return entry;
      });
    }
    setCreating(true);
    api
      .memoryResourceCreate(input)
      .then((created) => {
        toast(t("memoryPage.resources.created", { id: created.id ?? name }), "good");
        setName("");
        setDescription("");
        setExpiryDays(30);
        setStrategies(DEFAULT_STRATEGIES);
        setNsKeys([]);
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

  const openEdit = (row: MemoryResourceRow) => {
    const id = row.id;
    if (!id) return;
    setEdit({ row, detail: null, description: "", expiryDays: 30 });
    api
      .memoryResource(id)
      .then((detail) => {
        setEdit((cur) =>
          cur && cur.row.id === id
            ? {
                ...cur,
                detail,
                description: detail.description ?? "",
                expiryDays: detail.event_expiry_days ?? 30,
              }
            : cur,
        );
      })
      .catch((err: unknown) => {
        toast(
          t("memoryPage.resources.editLoadFailed", {
            id,
            msg: err instanceof Error ? err.message : String(err),
          }),
          "crit",
        );
        setEdit((cur) => (cur && cur.row.id === id ? null : cur));
      });
  };

  // Only the fields that actually changed go on the wire (the backend forwards
  // exactly those to UpdateMemory). Validity is judged per changed field so a
  // memory created with a 3-day expiry can still have its description edited.
  const editDescription = edit?.description.trim() ?? "";
  const editDescriptionChanged =
    edit?.detail != null && editDescription !== (edit.detail.description ?? "");
  const editExpiryChanged =
    edit?.detail != null && edit.expiryDays !== edit.detail.event_expiry_days;
  const editExpiryValid =
    edit != null &&
    Number.isInteger(edit.expiryDays) &&
    edit.expiryDays >= EDIT_EXPIRY_MIN &&
    edit.expiryDays <= EXPIRY_MAX;
  const editChanged = editDescriptionChanged || editExpiryChanged;
  const saveReason: string | undefined =
    edit == null
      ? undefined
      : edit.detail == null
        ? t("memoryPage.resources.editLoading")
        : saving
          ? t("memoryPage.resources.saving")
          : editDescriptionChanged && editDescription.length === 0
            ? t("memoryPage.resources.editDescriptionRequired")
            : editExpiryChanged && !editExpiryValid
              ? t("memoryPage.resources.editExpiryInvalid")
              : !editChanged
                ? t("memoryPage.resources.editNothingChanged")
                : undefined;

  const save = () => {
    if (!edit || !edit.detail || !edit.row.id || saveReason) return;
    const id = edit.row.id;
    const input: MemoryResourceUpdateInput = {};
    if (editDescriptionChanged) input.description = editDescription;
    if (editExpiryChanged) input.event_expiry_days = edit.expiryDays;
    setSaving(true);
    api
      .memoryResourceUpdate(id, input)
      .then((detail) => {
        toast(t("memoryPage.resources.updated", { id }), "good");
        // the row refreshes from the readback — no full reload needed
        setRows((prev) =>
          prev
            ? prev.map((r) =>
                r.id === id
                  ? {
                      ...r,
                      name: detail.name ?? r.name,
                      status: detail.status,
                      updated_at: detail.updated_at,
                    }
                  : r,
              )
            : prev,
        );
        setEdit(null);
      })
      .catch((err: unknown) => {
        toast(
          t("memoryPage.resources.updateFailed", {
            msg: err instanceof Error ? err.message : String(err),
          }),
          "crit",
        );
      })
      .finally(() => setSaving(false));
  };

  const saveBody = edit
    ? [
        t("memoryPage.resources.saveBody", { id: edit.row.id ?? "" }),
        editExpiryChanged
          ? t("memoryPage.resources.saveBodyExpiry", { days: edit.expiryDays })
          : null,
        edit.row.is_default ? t("memoryPage.resources.saveBodyDefault") : null,
        edit.row.agents.length > 0
          ? t("memoryPage.resources.saveBodyAgents", {
              count: edit.row.agents.length,
              names: edit.row.agents.map((a) => a.name).join(", "),
            })
          : null,
      ]
        .filter(Boolean)
        .join(" ")
    : "";

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
                <Fragment key={m.id ?? m.arn}>
                <tr className={m.is_default ? "sel" : undefined}>
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
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                      <Btn
                        onClick={() => (edit?.row.id === m.id ? setEdit(null) : openEdit(m))}
                        disabled={!m.id || saving}
                        data-testid="memory-resource-edit"
                      >
                        {t("memoryPage.resources.edit")}
                      </Btn>
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
                    </div>
                  </td>
                </tr>
                {edit && edit.row.id === m.id && (
                  <tr data-testid="memory-resource-edit-row">
                    <td colSpan={5}>
                      <div style={{ padding: "6px 0 4px" }}>
                        <div style={{ marginBottom: 8 }}>
                          <b>
                            {t("memoryPage.resources.editTitle", {
                              name: m.name ?? m.id ?? "",
                            })}
                          </b>
                          <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
                            {t("memoryPage.resources.editSub")}
                          </div>
                        </div>
                        <DescriptionField
                          id="mem-res-edit-desc"
                          value={edit.description}
                          onChange={(v) => setEdit((cur) => cur && { ...cur, description: v })}
                          disabled={edit.detail === null || saving}
                        />
                        <ExpiryField
                          id="mem-res-edit-expiry"
                          value={edit.expiryDays}
                          min={EDIT_EXPIRY_MIN}
                          hint={t("memoryPage.resources.editExpiryHint")}
                          onChange={(v) => setEdit((cur) => cur && { ...cur, expiryDays: v })}
                          disabled={edit.detail === null || saving}
                        />
                        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                          <Btn
                            primary
                            onClick={() => setConfirmSave(true)}
                            disabled={saveReason !== undefined}
                            disabledReason={saveReason}
                            data-testid="memory-resource-save"
                          >
                            {saving
                              ? t("memoryPage.resources.saving")
                              : t("memoryPage.resources.save")}
                          </Btn>
                          <Btn onClick={() => setEdit(null)} disabled={saving}>
                            {t("common.cancel")}
                          </Btn>
                        </div>
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
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
          <DescriptionField id="mem-res-desc" value={description} onChange={setDescription} />
          <ExpiryField
            id="mem-res-expiry"
            value={expiryDays}
            min={3}
            hint={t("memoryPage.resources.expiryHint")}
            onChange={setExpiryDays}
          />
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
          {SHOW_NS_KEYS && (
          <div className="field">
            <label>{t("memoryPage.resources.nsKeys")}</label>
            {nsKeys.map((row, i) => (
              <div
                key={i}
                style={{ display: "flex", gap: 6, marginBottom: 6, alignItems: "center" }}
              >
                <input
                  className="input mono"
                  style={{ width: 130 }}
                  value={row.key}
                  onChange={(e) => setNsKey(i, { key: e.target.value })}
                  placeholder={t("memoryPage.resources.nsKeyPlaceholder")}
                  aria-label={t("memoryPage.resources.nsKeyPlaceholder")}
                  data-testid={`memory-ns-key-${i}`}
                />
                <input
                  className="input mono"
                  style={{ flex: 1, minWidth: 0 }}
                  value={row.allowedValues}
                  onChange={(e) => setNsKey(i, { allowedValues: e.target.value })}
                  placeholder={t("memoryPage.resources.nsValuesPlaceholder")}
                  aria-label={t("memoryPage.resources.nsValuesPlaceholder")}
                />
                <input
                  className="input mono"
                  style={{ flex: 1, minWidth: 0 }}
                  value={row.regexPattern}
                  onChange={(e) => setNsKey(i, { regexPattern: e.target.value })}
                  placeholder={t("memoryPage.resources.nsRegexPlaceholder")}
                  aria-label={t("memoryPage.resources.nsRegexPlaceholder")}
                />
                <Btn
                  onClick={() => setNsKeys((prev) => prev.filter((_, j) => j !== i))}
                  title={t("memoryPage.resources.nsRemove")}
                >
                  ✕
                </Btn>
              </div>
            ))}
            {nsKeys.length < NS_MAX_KEYS && (
              <Btn
                onClick={() => setNsKeys((prev) => [...prev, { ...EMPTY_NS_ROW }])}
                data-testid="memory-ns-key-add"
              >
                {t("memoryPage.resources.nsAdd")}
              </Btn>
            )}
            {nsPreviewRows.length > 0 && (
              <div
                className="mono"
                style={{
                  fontSize: 11,
                  marginTop: 8,
                  padding: "8px 10px",
                  border: "1px solid var(--line)",
                  borderRadius: 4,
                  overflowWrap: "anywhere",
                }}
                data-testid="memory-ns-preview"
              >
                <div className="dim" style={{ marginBottom: 4 }}>
                  {t("memoryPage.resources.nsPreview")}
                </div>
                {nsSelectedStrategies.length === 0 ? (
                  <div className="dim">{t("memoryPage.resources.nsPreviewNoStrategies")}</div>
                ) : (
                  nsSelectedStrategies.map((key) => (
                    <div key={key} style={{ marginBottom: 6 }}>
                      <div className="dim">{t(`memoryPage.resources.strategy.${key}`)}</div>
                      <div>
                        <span className="dim">
                          {t("memoryPage.resources.nsPreviewTemplate")}{" "}
                        </span>
                        {nsPrefixTemplate + STRATEGY_NAMESPACES[key]}
                      </div>
                      <div>
                        <span className="dim">
                          {t("memoryPage.resources.nsPreviewExample")}{" "}
                        </span>
                        {nsPrefixExample + nsResolveBuiltins(STRATEGY_NAMESPACES[key])}
                      </div>
                    </div>
                  ))
                )}
              </div>
            )}
            {nsKeys.length > 0 && !nsValid && (
              <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                {t("memoryPage.resources.nsInvalid")}
              </div>
            )}
            <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
              {t("memoryPage.resources.nsHint")}
            </div>
          </div>
          )}
          <Btn
            primary
            onClick={create}
            disabled={!nameValid || !expiryValid || !nsValid || creating}
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

      <ConfirmDialog
        open={confirmSave && edit !== null}
        title={t("memoryPage.resources.saveTitle", {
          name: edit?.row.name ?? edit?.row.id ?? "",
        })}
        body={saveBody}
        confirmLabel={t("memoryPage.resources.save")}
        onConfirm={() => {
          setConfirmSave(false);
          save();
        }}
        onCancel={() => setConfirmSave(false)}
      />
    </>
  );
}
