import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  Pager,
  Panel,
  StaleLink,
  useStaleParam,
  useTablePage,
  useToast,
  ViewHead,
} from "../components";
import { EvaluationNav } from "../components/EvaluationNav";

interface CloudVersion {
  version: string | null;
  example_count: number | null;
  created_at: string | null;
}

// The row's one AWS Dataset: re-sync edits its DRAFT in place, PUBLISH VERSION
// snapshots the draft as an immutable numbered version.
interface CloudBlob {
  dataset_id: string;
  arn?: string | null;
  status: string;
  synced_at?: string | null;
  failure_reason?: string | null;
  draft_status?: string | null;
  example_count?: number | null;
  versions?: CloudVersion[];
}

interface DatasetRow {
  id: string;
  name: string;
  kind: string;
  locale: string;
  description: string;
  item_count: number;
  items: Record<string, unknown>[];
  cloud: CloudBlob | null;
  has_ground_truth: boolean;
}

interface CloudRow {
  datasetId: string;
  name: string | null;
  status: string | null;
  schemaType: string | null;
  exampleCount: number | null;
  draftStatus?: string | null;
  updatedAt: string | null;
}

// GET /api/eval/datasets/cloud/{id} — draft + versions for a cloud-only row.
interface CloudDetail {
  datasetId: string;
  status: string | null;
  draft_status: string | null;
  failure_reason: string | null;
  versions: CloudVersion[];
}

interface TurnDraft {
  input: string;
  expected_response: string;
}

interface ScenarioDraft {
  scenario_id: string;
  turns: TurnDraft[];
  assertions: string[];
  expected_trajectory: string; // comma-separated tool names
}

interface TraitDraft {
  key: string;
  value: string;
}

// Devguide user-simulation scenario (actor_profile persona). max_turns stays
// a string in the draft for friction-free number-input editing; parsed on emit.
interface SimScenarioDraft {
  scenario_id: string;
  scenario_description: string;
  context: string;
  goal: string;
  traits: TraitDraft[];
  input: string;
  max_turns: string;
  assertions: string[];
}

// Same "cloud:" id encoding as the New Run scope dropdown / runs-list rows.
const CLOUD_PREFIX = "cloud:";

type Selection =
  | { kind: "local"; row: DatasetRow }
  | { kind: "cloud"; row: CloudRow }
  | null;

const emptyScenario = (index: number): ScenarioDraft => ({
  scenario_id: `scenario_${index}`,
  turns: [{ input: "", expected_response: "" }],
  assertions: [],
  expected_trajectory: "",
});

const emptySimScenario = (index: number): SimScenarioDraft => ({
  scenario_id: `persona_${index}`,
  scenario_description: "",
  context: "",
  goal: "",
  traits: [],
  input: "",
  max_turns: "10",
  assertions: [],
});

// 2-persona support sample adapted from the devguide user-simulation examples
// (双语注释:LLM actor 扮演用户,goal 达成或到 max_turns 即停;assertions 是
// simulated 数据集唯一的真值)。
const SIM_SAMPLE_SCENARIOS = (): SimScenarioDraft[] => [
  {
    scenario_id: "frustrated_laptop_customer",
    scenario_description: "Customer with a cracked laptop screen wants a warranty fix",
    context:
      "You bought a laptop 3 weeks ago and the screen cracked on its own. " +
      "You already restarted it twice and searched the FAQ without luck.",
    goal: "Get a repair or replacement arranged under warranty",
    traits: [
      { key: "expertise", value: "non-technical" },
      { key: "tone", value: "frustrated but polite" },
    ],
    input: "My brand new laptop screen cracked on its own and I need this fixed.",
    max_turns: "8",
    assertions: [
      "The agent acknowledges the customer's frustration",
      "The agent offers a warranty repair or replacement path",
    ],
  },
  {
    scenario_id: "billing_duplicate_charge",
    scenario_description: "Calm customer asking about a duplicate subscription charge",
    context:
      "The same subscription charge appears twice on this month's statement. " +
      "You want an explanation and a refund of the extra charge.",
    goal: "Confirm the duplicate charge and get a refund initiated",
    traits: [
      { key: "expertise", value: "intermediate" },
      { key: "tone", value: "calm" },
    ],
    input: "Hi, I think I was charged twice this month — can you check my invoice?",
    max_turns: "6",
    assertions: ["The agent verifies the charge before promising a refund"],
  },
];

// 3-scenario math sample with ground truth (expected_trajectory names the
// zip template's real `calculator` tool) — sync-ready for dataset runs.
const SAMPLE_SCENARIOS = (): ScenarioDraft[] => [
  {
    scenario_id: "add_two_numbers",
    turns: [{ input: "What is 17 + 25? Use your calculator tool.", expected_response: "42" }],
    assertions: ["The agent returns the exact sum 42"],
    expected_trajectory: "calculator",
  },
  {
    scenario_id: "multiply_then_add",
    turns: [
      { input: "Multiply 6 by 7 with your calculator tool.", expected_response: "42" },
      { input: "Now add 8 to that result.", expected_response: "50" },
    ],
    assertions: ["The agent keeps the running result across turns"],
    expected_trajectory: "calculator",
  },
  {
    scenario_id: "plain_greeting",
    turns: [{ input: "Say hello in one short sentence.", expected_response: "" }],
    assertions: [],
    expected_trajectory: "",
  },
];

// Any stored item (legacy prompt or devguide scenario) → editor draft.
function toDrafts(items: Record<string, unknown>[]): ScenarioDraft[] {
  return items.map((item, i) => {
    if ("turns" in item) {
      const turns = (item.turns as Record<string, unknown>[]).map((turn) => {
        const raw = turn.input;
        const input =
          typeof raw === "object" && raw !== null
            ? String(
                (raw as Record<string, unknown>).content ??
                  (raw as Record<string, unknown>).prompt ??
                  "",
              )
            : String(raw ?? "");
        return { input, expected_response: String(turn.expected_response ?? "") };
      });
      return {
        scenario_id: String(item.scenario_id ?? `scenario_${i + 1}`),
        turns,
        assertions: ((item.assertions as string[] | undefined) ?? []).map(String),
        expected_trajectory: ((item.expected_trajectory as string[] | undefined) ?? []).join(", "),
      };
    }
    return {
      scenario_id: `item_${i + 1}`,
      turns: [
        { input: String(item.prompt ?? ""), expected_response: String(item.expected ?? "") },
      ],
      assertions: [],
      expected_trajectory: "",
    };
  });
}

// Editor drafts → items to store. Legacy datasets keep their shape when the
// content still fits it (kind is immutable server-side).
function toItems(scenarios: ScenarioDraft[], kind: string): Record<string, unknown>[] {
  const fitsLegacy = scenarios.every(
    (s) =>
      s.turns.length === 1 &&
      !s.assertions.some((a) => a.trim()) &&
      !s.expected_trajectory.trim(),
  );
  if (kind === "legacy" && fitsLegacy) {
    return scenarios.map((s) => ({
      prompt: s.turns[0].input,
      ...(s.turns[0].expected_response.trim()
        ? { expected: s.turns[0].expected_response.trim() }
        : {}),
    }));
  }
  return scenarios.map((s) => {
    const assertions = s.assertions.map((a) => a.trim()).filter(Boolean);
    const trajectory = s.expected_trajectory
      .split(",")
      .map((x) => x.trim())
      .filter(Boolean);
    return {
      scenario_id: s.scenario_id.trim(),
      turns: s.turns.map((turn) => ({
        input: turn.input,
        ...(turn.expected_response.trim()
          ? { expected_response: turn.expected_response.trim() }
          : {}),
      })),
      ...(assertions.length ? { assertions } : {}),
      ...(trajectory.length ? { expected_trajectory: trajectory } : {}),
    };
  });
}

// Stored simulated items (devguide actor_profile shape) → editor drafts.
function toSimDrafts(items: Record<string, unknown>[]): SimScenarioDraft[] {
  return items
    .filter((item) => "actor_profile" in item)
    .map((item, i) => {
      const profile = (item.actor_profile ?? {}) as Record<string, unknown>;
      const traits = (profile.traits ?? {}) as Record<string, unknown>;
      return {
        scenario_id: String(item.scenario_id ?? `persona_${i + 1}`),
        scenario_description: String(item.scenario_description ?? ""),
        context: String(profile.context ?? ""),
        goal: String(profile.goal ?? ""),
        traits: Object.entries(traits).map(([key, value]) => ({
          key: String(key),
          value: String(value),
        })),
        input: String(item.input ?? ""),
        max_turns: String(item.max_turns ?? 10),
        assertions: ((item.assertions as string[] | undefined) ?? []).map(String),
      };
    });
}

// Sim drafts → devguide user-simulation items. Optional fields only when
// non-empty; max_turns only when it differs from the schema default 10.
function toSimItems(drafts: SimScenarioDraft[]): Record<string, unknown>[] {
  return drafts.map((s) => {
    const traits = Object.fromEntries(
      s.traits.filter((tr) => tr.key.trim()).map((tr) => [tr.key.trim(), tr.value]),
    );
    const assertions = s.assertions.map((a) => a.trim()).filter(Boolean);
    const maxTurns = Number.parseInt(s.max_turns, 10);
    return {
      scenario_id: s.scenario_id.trim(),
      ...(s.scenario_description.trim()
        ? { scenario_description: s.scenario_description.trim() }
        : {}),
      actor_profile: {
        context: s.context,
        goal: s.goal,
        ...(Object.keys(traits).length ? { traits } : {}),
      },
      input: s.input,
      ...(Number.isFinite(maxTurns) && maxTurns >= 1 && maxTurns !== 10
        ? { max_turns: maxTurns }
        : {}),
      ...(assertions.length ? { assertions } : {}),
    };
  });
}

export function DatasetsView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [rows, setRows] = useState<DatasetRow[]>([]);
  const [cloudRows, setCloudRows] = useState<CloudRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [cloudError, setCloudError] = useState(false);
  const [cloudLoaded, setCloudLoaded] = useState(false);
  const [syncingId, setSyncingId] = useState<string | null>(null);
  const [syncError, setSyncError] = useState<string | null>(null);
  // publish-in-flight key: local row id or "cloud:<datasetId>"
  const [publishingKey, setPublishingKey] = useState<string | null>(null);
  const [cloudDetail, setCloudDetail] = useState<CloudDetail | null>(null);
  const [confirmVersion, setConfirmVersion] = useState<{
    cloudId: string;
    version: string;
  } | null>(null);
  const [confirmLocal, setConfirmLocal] = useState<DatasetRow | null>(null);
  const [confirmCloud, setConfirmCloud] = useState<CloudRow | null>(null);

  // editor state
  const [editorMode, setEditorMode] = useState<"form" | "import">("form");
  const [editingKind, setEditingKind] = useState("predefined");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scenarios, setScenarios] = useState<ScenarioDraft[]>([emptyScenario(1)]);
  const [scenarioType, setScenarioType] = useState<"predefined" | "simulated">("predefined");
  const [simScenarios, setSimScenarios] = useState<SimScenarioDraft[]>([emptySimScenario(1)]);
  const [importText, setImportText] = useState("");
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/eval/datasets");
      if (res.ok) {
        setRows(((await res.json()) as { datasets: DatasetRow[] }).datasets);
      }
    } catch {
      /* backend offline */
    } finally {
      setLoading(false);
    }
    try {
      const res = await fetch("/api/eval/datasets/cloud");
      if (!res.ok) throw new Error(`http ${res.status}`);
      setCloudRows(((await res.json()) as { datasets: CloudRow[] }).datasets);
      setCloudError(false);
    } catch {
      setCloudError(true);
    }
    setCloudLoaded(true);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  // "?ds=<id>" selects a local row, "?ds=cloud:<datasetId>" a cloud-only row,
  // "?ds=new" the create form (linkable, back-button friendly).
  const [searchParams, setSearchParams] = useSearchParams();
  const dsParam = searchParams.get("ds");
  const creatingNew = dsParam === "new";
  const selectDs = (id: string | null) => {
    setSearchParams(id ? { view: "datasets", ds: id } : { view: "datasets" });
  };
  const backToList = () => {
    setSearchParams({ view: "datasets" }, { replace: true });
  };

  // Cloud rows that are the synced copy of a local dataset render as that
  // row's CLOUD chip — only cloud-ONLY snapshots get their own table row.
  const cloudOnly = useMemo(
    () => cloudRows.filter((c) => !rows.some((r) => r.cloud?.dataset_id === c.datasetId)),
    [cloudRows, rows],
  );

  const selection = useMemo<Selection>(() => {
    if (creatingNew) return null;
    if (dsParam?.startsWith(CLOUD_PREFIX)) {
      // no local fallback while the cloud list loads — hydrating the editor
      // with an unrelated local row would be worse than a brief empty panel
      const id = dsParam.slice(CLOUD_PREFIX.length);
      const row = cloudRows.find((r) => r.datasetId === id);
      return row ? { kind: "cloud", row } : null;
    }
    const row = (dsParam ? rows.find((r) => r.id === dsParam) : undefined) ?? rows[0];
    return row ? { kind: "local", row } : null;
  }, [creatingNew, dsParam, rows, cloudRows]);

  // A stale "?ds=" (list loaded, id absent) says so at the top of the page and
  // drops the param — the row fallback below then reads as a plain list visit.
  const dsIsCloud = !!dsParam?.startsWith(CLOUD_PREFIX);
  const dsSettled = dsIsCloud ? cloudLoaded && !cloudError : !loading;
  const dsFound =
    creatingNew ||
    (dsIsCloud
      ? selection?.kind === "cloud"
      : selection?.kind === "local" && selection.row.id === dsParam);
  const staleDs = useStaleParam(dsParam, dsSettled && !dsFound, backToList);

  const local = selection?.kind === "local" ? selection.row : null;
  const cloud = selection?.kind === "cloud" ? selection.row : null;
  const cloudId = cloud?.datasetId ?? null;

  // cloud-only rows: the list carries no versions — read the detail on selection
  const loadCloudDetail = useCallback(async (id: string) => {
    try {
      const res = await fetch(`/api/eval/datasets/cloud/${id}`);
      if (!res.ok) throw new Error(`http ${res.status}`);
      setCloudDetail((await res.json()) as CloudDetail);
    } catch {
      setCloudDetail(null);
    }
  }, []);

  useEffect(() => {
    setCloudDetail(null);
    if (cloudId) void loadCloudDetail(cloudId);
  }, [cloudId, loadCloudDetail]);
  const editingId = local?.id ?? null;

  // local rows then cloud-only rows are ONE list as far as the reader (and the
  // pager) is concerned, so they page together instead of each table half
  // paging on its own.
  const entries = useMemo(
    () => [
      ...rows.map((row) => ({ kind: "local" as const, row })),
      ...cloudOnly.map((row) => ({ kind: "cloud" as const, row })),
    ],
    [rows, cloudOnly],
  );
  const selectedEntry = entries.findIndex((entry) =>
    entry.kind === "local"
      ? entry.row.id === local?.id
      : entry.row.datasetId === cloud?.datasetId,
  );
  const { rows: pageEntries, pagerProps } = useTablePage(entries, selectedEntry);

  // kind=simulated with non-actor items can only come from import — the form
  // editor cannot represent those rows, so degrade to a warning (no save).
  const mixedSimulated =
    local?.kind === "simulated" && local.items.some((item) => !("actor_profile" in item));
  // Editing pins the type to the row's immutable kind; creating follows the chips.
  const activeType = local
    ? local.kind === "simulated"
      ? "simulated"
      : "predefined"
    : scenarioType;

  // Editor hydration keys off the selected KEY, not the row object: sync()
  // and save() both re-load(), which replaces row identities — re-hydrating
  // then would wipe unsaved edits. The ref carries the current row into the
  // effect without widening its dependency list.
  const selKey =
    selection === null
      ? "new"
      : selection.kind === "local"
        ? `local:${selection.row.id}`
        : `cloud:${selection.row.datasetId}`;
  const selRef = useRef<Selection>(null);
  selRef.current = selection;
  useEffect(() => {
    const sel = selRef.current;
    setEditorMode("form");
    setFormError(null);
    setSyncError(null);
    if (sel?.kind === "local") {
      setEditingKind(sel.row.kind);
      setName(sel.row.name);
      setDescription(sel.row.description ?? "");
      if (sel.row.kind === "simulated") {
        setScenarioType("simulated");
        setSimScenarios(toSimDrafts(sel.row.items));
        setScenarios([emptyScenario(1)]);
      } else {
        setScenarioType("predefined");
        setScenarios(toDrafts(sel.row.items));
        setSimScenarios([emptySimScenario(1)]);
      }
    } else {
      setEditingKind("predefined");
      setScenarioType("predefined");
      setName("");
      setDescription("");
      setScenarios([emptyScenario(1)]);
      setSimScenarios([emptySimScenario(1)]);
      setImportText("");
    }
  }, [selKey]);

  const importPreview = useMemo((): { items: Record<string, unknown>[]; error: string | null } => {
    const trimmed = importText.trim();
    if (!trimmed) return { items: [], error: null };
    // Whole-document JSON first: {scenarios:[...]}, a bare array, or one
    // object. Legacy JSONL also starts with "{" but fails this parse on the
    // second line, so it falls through to the per-line branch below.
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return { items: parsed as Record<string, unknown>[], error: null };
      if (parsed && typeof parsed === "object") {
        const scen = (parsed as { scenarios?: unknown }).scenarios;
        if (Array.isArray(scen)) return { items: scen as Record<string, unknown>[], error: null };
        if (scen !== undefined) {
          return { items: [], error: t("evalPage.datasets.importNoScenarios") };
        }
        return { items: [parsed as Record<string, unknown>], error: null };
      }
      return { items: [], error: t("evalPage.datasets.importNoScenarios") };
    } catch {
      /* not a single JSON document — try JSONL */
    }
    const items: Record<string, unknown>[] = [];
    const lines = trimmed.split("\n");
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].trim()) continue;
      try {
        items.push(JSON.parse(lines[i]) as Record<string, unknown>);
      } catch {
        return { items: [], error: t("evalPage.datasets.importBadLine", { line: i + 1 }) };
      }
    }
    return { items, error: null };
  }, [importText, t]);

  const patchScenario = (index: number, patch: Partial<ScenarioDraft>) => {
    setScenarios((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  };

  const patchSim = (index: number, patch: Partial<SimScenarioDraft>) => {
    setSimScenarios((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  };

  const save = async () => {
    setFormError(null);
    if (!name.trim()) {
      setFormError(t("evalPage.datasets.nameRequired"));
      return;
    }
    const items =
      editorMode === "import"
        ? importPreview.items
        : activeType === "simulated"
          ? toSimItems(simScenarios)
          : toItems(scenarios, editingId ? editingKind : "predefined");
    if (editorMode === "import" && (importPreview.error || items.length === 0)) {
      setFormError(importPreview.error ?? t("evalPage.datasets.importEmpty"));
      return;
    }
    setBusy(true);
    try {
      const res = editingId
        ? await fetch(`/api/eval/datasets/${editingId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim(), description, items }),
          })
        : await fetch("/api/eval/datasets", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ name: name.trim(), description, items }),
          });
      if (!res.ok) {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        setFormError(env.message ?? `HTTP ${res.status}`);
        return;
      }
      if (editingId) {
        toast(t("evalPage.datasets.updated"));
        await load(); // selKey unchanged — the saved edits stay on screen
      } else {
        toast(t("evalPage.datasets.created"));
        const created = (await res.json()) as { id: string };
        await load();
        selectDs(created.id);
      }
    } finally {
      setBusy(false);
    }
  };

  const sync = async (row: DatasetRow) => {
    setSyncingId(row.id);
    setSyncError(null);
    try {
      const res = await fetch(`/api/eval/datasets/${row.id}/sync-to-aws`, { method: "POST" });
      if (!res.ok) {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        setSyncError(env.message ?? `HTTP ${res.status}`);
      } else {
        toast(t("evalPage.datasets.synced"));
      }
      await load();
    } finally {
      setSyncingId(null);
    }
  };

  const deleteLocal = async (row: DatasetRow) => {
    const res = await fetch(`/api/eval/datasets/${row.id}`, { method: "DELETE" });
    if (!res.ok) {
      const env = (await res.json().catch(() => ({}))) as { message?: string };
      toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      return;
    }
    toast(t("evalPage.datasets.deleted"));
    if (dsParam === row.id) selectDs(null);
    await load();
  };

  const deleteCloud = async (row: CloudRow) => {
    const res = await fetch(`/api/eval/datasets/cloud/${row.datasetId}`, { method: "DELETE" });
    if (!res.ok) {
      const env = (await res.json().catch(() => ({}))) as { message?: string };
      toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      return;
    }
    toast(t("evalPage.datasets.cloudDeleted"));
    if (dsParam === CLOUD_PREFIX + row.datasetId) selectDs(null);
    await load();
  };

  const publish = async (key: string, url: string) => {
    setPublishingKey(key);
    setSyncError(null);
    try {
      const res = await fetch(url, { method: "POST" });
      if (!res.ok) {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        setSyncError(env.message ?? `HTTP ${res.status}`);
      } else {
        const body = (await res.json()) as { cloud?: CloudBlob; versions?: CloudVersion[] };
        const versions = body.cloud?.versions ?? body.versions ?? [];
        toast(t("evalPage.datasets.published", { version: versions[0]?.version ?? "" }));
      }
      await load();
      if (cloudId) await loadCloudDetail(cloudId);
    } finally {
      setPublishingKey(null);
    }
  };

  const deleteVersion = async (id: string, version: string) => {
    const res = await fetch(`/api/eval/datasets/cloud/${id}/versions/${version}`, {
      method: "DELETE",
    });
    if (!res.ok) {
      const env = (await res.json().catch(() => ({}))) as { message?: string };
      toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      return;
    }
    toast(t("evalPage.datasets.versionDeleted", { version }));
    await load();
    if (cloudId) await loadCloudDetail(cloudId);
  };

  const cloudChip = (row: DatasetRow) => {
    if (!row.cloud) return <Chip tone="muted">{t("evalPage.datasets.notSynced")}</Chip>;
    if (row.cloud.status === "ACTIVE") return <Chip tone="good" icon="●">ACTIVE</Chip>;
    if (row.cloud.status === "deleted")
      return <Chip tone="muted">{t("evalPage.datasets.cloudGone")}</Chip>;
    return <Chip tone="crit" icon="✕">{row.cloud.status}</Chip>;
  };

  // DRAFT status of the cloud dataset: MODIFIED = edits not yet published as a
  // version; UNMODIFIED = the draft equals the newest version.
  const draftChip = (draft: string | null | undefined) => {
    if (!draft) return null;
    const modified = draft === "MODIFIED";
    return (
      <Chip tone={modified ? "warn" : "good"}>
        {modified
          ? t("evalPage.datasets.draftModified")
          : t("evalPage.datasets.draftUnmodified")}
      </Chip>
    );
  };

  // Why PUBLISH VERSION is disabled right now, or null when it is allowed.
  const publishBlocker = (status: string | null | undefined): string | null => {
    if (publishingKey !== null || syncingId !== null)
      return t("evalPage.datasets.publishBusy");
    if (status !== "ACTIVE")
      return t("evalPage.datasets.publishNotActive", { status: status ?? "—" });
    return null;
  };

  const versionsList = (id: string, versions: CloudVersion[] | undefined) => (
    <div style={{ marginTop: 8 }}>
      <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".06em", marginBottom: 4 }}>
        {t("evalPage.datasets.versions.title")}
      </div>
      {!versions || versions.length === 0 ? (
        <div className="mono dim" style={{ fontSize: 10.5 }}>
          {t("evalPage.datasets.versions.none")}
        </div>
      ) : (
        <table className="tbl" style={{ fontSize: 10.5 }} data-testid={`versions-${id}`}>
          <thead>
            <tr>
              <th>{t("evalPage.datasets.versions.col.version")}</th>
              <th>{t("evalPage.datasets.versions.col.examples")}</th>
              <th>{t("evalPage.datasets.versions.col.created")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version ?? ""}>
                <td className="mono">v{v.version ?? "?"}</td>
                <td className="mono dim">{v.example_count ?? "—"}</td>
                <td className="mono dim">
                  {v.created_at ? new Date(v.created_at).toLocaleString() : "—"}
                </td>
                <td style={{ textAlign: "right" }}>
                  <button
                    type="button"
                    className="mono dim"
                    title={t("evalPage.datasets.versions.delete")}
                    aria-label={t("evalPage.datasets.versions.delete")}
                    style={{ background: "none", border: 0, cursor: "pointer" }}
                    onClick={() =>
                      v.version && setConfirmVersion({ cloudId: id, version: v.version })
                    }
                  >
                    ✕
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );

  const scenarioEditor = (
    <>
      {scenarios.map((scenario, si) => (
        <div
          key={si}
          style={{
            border: "1px solid rgba(255,255,255,.08)",
            borderRadius: 4,
            padding: "10px 12px",
            marginBottom: 10,
          }}
        >
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
            <input
              className="input mono"
              value={scenario.scenario_id}
              aria-label={t("evalPage.datasets.scenarioId")}
              style={{ maxWidth: 220 }}
              onChange={(e) => patchScenario(si, { scenario_id: e.target.value })}
            />
            <Btn
              disabled={scenarios.length <= 1}
              style={{ marginLeft: "auto" }}
              title={t("evalPage.datasets.removeScenario")}
              onClick={() => setScenarios((prev) => prev.filter((_, i) => i !== si))}
            >
              ✕
            </Btn>
          </div>
          {scenario.turns.map((turn, ti) => (
            <div key={ti} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
              <span className="mono dim" style={{ fontSize: 9.5, paddingTop: 8 }}>
                T{ti + 1}
              </span>
              <textarea
                className="input"
                rows={1}
                placeholder={t("evalPage.datasets.turnInput")}
                value={turn.input}
                style={{ flex: 2, resize: "vertical", fontSize: 11.5 }}
                onChange={(e) =>
                  patchScenario(si, {
                    turns: scenario.turns.map((x, i) =>
                      i === ti ? { ...x, input: e.target.value } : x,
                    ),
                  })
                }
              />
              <input
                className="input"
                placeholder={t("evalPage.datasets.turnExpected")}
                value={turn.expected_response}
                style={{ flex: 1, fontSize: 11.5 }}
                onChange={(e) =>
                  patchScenario(si, {
                    turns: scenario.turns.map((x, i) =>
                      i === ti ? { ...x, expected_response: e.target.value } : x,
                    ),
                  })
                }
              />
              <Btn
                disabled={scenario.turns.length <= 1}
                title={t("evalPage.datasets.removeTurn")}
                onClick={() =>
                  patchScenario(si, {
                    turns: scenario.turns.filter((_, i) => i !== ti),
                  })
                }
              >
                ✕
              </Btn>
            </div>
          ))}
          <Btn
            onClick={() =>
              patchScenario(si, {
                turns: [...scenario.turns, { input: "", expected_response: "" }],
              })
            }
          >
            + {t("evalPage.datasets.addTurn")}
          </Btn>
          <div className="field" style={{ marginTop: 8 }}>
            <label>{t("evalPage.datasets.assertions")}</label>
            {scenario.assertions.map((assertion, ai) => (
              <div key={ai} style={{ display: "flex", gap: 6, marginBottom: 4 }}>
                <input
                  className="input"
                  value={assertion}
                  style={{ fontSize: 11.5 }}
                  onChange={(e) =>
                    patchScenario(si, {
                      assertions: scenario.assertions.map((x, i) =>
                        i === ai ? e.target.value : x,
                      ),
                    })
                  }
                />
                <Btn
                  title={t("evalPage.datasets.removeAssertion")}
                  onClick={() =>
                    patchScenario(si, {
                      assertions: scenario.assertions.filter((_, i) => i !== ai),
                    })
                  }
                >
                  ✕
                </Btn>
              </div>
            ))}
            <Btn
              onClick={() =>
                patchScenario(si, { assertions: [...scenario.assertions, ""] })
              }
            >
              + {t("evalPage.datasets.addAssertion")}
            </Btn>
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>{t("evalPage.datasets.trajectory")}</label>
            <input
              className="input mono"
              placeholder="calculator, current_time"
              value={scenario.expected_trajectory}
              style={{ fontSize: 11 }}
              onChange={(e) =>
                patchScenario(si, { expected_trajectory: e.target.value })
              }
            />
          </div>
        </div>
      ))}
      <Btn
        onClick={() => setScenarios((prev) => [...prev, emptyScenario(prev.length + 1)])}
      >
        + {t("evalPage.datasets.addScenario")}
      </Btn>
    </>
  );

  const simScenarioEditor = (
    <>
      {simScenarios.map((scenario, si) => (
        <div
          key={si}
          style={{
            border: "1px solid rgba(255,255,255,.08)",
            borderRadius: 4,
            padding: "10px 12px",
            marginBottom: 10,
          }}
        >
          <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 8 }}>
            <input
              className="input mono"
              value={scenario.scenario_id}
              aria-label={t("evalPage.datasets.scenarioId")}
              style={{ maxWidth: 220 }}
              onChange={(e) => patchSim(si, { scenario_id: e.target.value })}
            />
            <Btn
              disabled={simScenarios.length <= 1}
              style={{ marginLeft: "auto" }}
              title={t("evalPage.datasets.removeScenario")}
              onClick={() => setSimScenarios((prev) => prev.filter((_, i) => i !== si))}
            >
              ✕
            </Btn>
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simDescription")}</label>
            <input
              className="input"
              value={scenario.scenario_description}
              style={{ fontSize: 11.5 }}
              onChange={(e) => patchSim(si, { scenario_description: e.target.value })}
            />
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simContext")}</label>
            <textarea
              className="input"
              rows={2}
              value={scenario.context}
              style={{ resize: "vertical", fontSize: 11.5 }}
              onChange={(e) => patchSim(si, { context: e.target.value })}
            />
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simGoal")}</label>
            <textarea
              className="input"
              rows={2}
              value={scenario.goal}
              style={{ resize: "vertical", fontSize: 11.5 }}
              onChange={(e) => patchSim(si, { goal: e.target.value })}
            />
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simTraits")}</label>
            {scenario.traits.map((trait, ti) => (
              <div key={ti} style={{ display: "flex", gap: 6, marginBottom: 4 }}>
                <input
                  className="input mono"
                  placeholder={t("evalPage.datasets.simTraitKey")}
                  value={trait.key}
                  style={{ width: 130, fontSize: 11 }}
                  onChange={(e) =>
                    patchSim(si, {
                      traits: scenario.traits.map((x, i) =>
                        i === ti ? { ...x, key: e.target.value } : x,
                      ),
                    })
                  }
                />
                <input
                  className="input"
                  placeholder={t("evalPage.datasets.simTraitValue")}
                  value={trait.value}
                  style={{ flex: 1, fontSize: 11.5 }}
                  onChange={(e) =>
                    patchSim(si, {
                      traits: scenario.traits.map((x, i) =>
                        i === ti ? { ...x, value: e.target.value } : x,
                      ),
                    })
                  }
                />
                <Btn
                  title={t("evalPage.datasets.removeTrait")}
                  onClick={() =>
                    patchSim(si, { traits: scenario.traits.filter((_, i) => i !== ti) })
                  }
                >
                  ✕
                </Btn>
              </div>
            ))}
            <Btn
              onClick={() =>
                patchSim(si, { traits: [...scenario.traits, { key: "", value: "" }] })
              }
            >
              + {t("evalPage.datasets.addTrait")}
            </Btn>
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simInput")}</label>
            <textarea
              className="input"
              rows={2}
              value={scenario.input}
              style={{ resize: "vertical", fontSize: 11.5 }}
              onChange={(e) => patchSim(si, { input: e.target.value })}
            />
          </div>
          <div className="field">
            <label>{t("evalPage.datasets.simMaxTurns")}</label>
            <input
              className="input mono"
              type="number"
              min={1}
              aria-label={t("evalPage.datasets.simMaxTurns")}
              value={scenario.max_turns}
              style={{ width: 92 }}
              onChange={(e) => patchSim(si, { max_turns: e.target.value })}
            />
          </div>
          <div className="field" style={{ marginBottom: 0 }}>
            <label>{t("evalPage.datasets.assertions")}</label>
            {scenario.assertions.map((assertion, ai) => (
              <div key={ai} style={{ display: "flex", gap: 6, marginBottom: 4 }}>
                <input
                  className="input"
                  value={assertion}
                  style={{ fontSize: 11.5 }}
                  onChange={(e) =>
                    patchSim(si, {
                      assertions: scenario.assertions.map((x, i) =>
                        i === ai ? e.target.value : x,
                      ),
                    })
                  }
                />
                <Btn
                  title={t("evalPage.datasets.removeAssertion")}
                  onClick={() =>
                    patchSim(si, {
                      assertions: scenario.assertions.filter((_, i) => i !== ai),
                    })
                  }
                >
                  ✕
                </Btn>
              </div>
            ))}
            <Btn onClick={() => patchSim(si, { assertions: [...scenario.assertions, ""] })}>
              + {t("evalPage.datasets.addAssertion")}
            </Btn>
          </div>
        </div>
      ))}
      <Btn
        onClick={() => setSimScenarios((prev) => [...prev, emptySimScenario(prev.length + 1)])}
      >
        + {t("evalPage.datasets.addScenario")}
      </Btn>
      <div className="note" style={{ marginTop: 10 }}>
        <span className="i">[i]</span>
        <span>{t("evalPage.datasets.simHint")}</span>
      </div>
    </>
  );

  return (
    <section>
      <ViewHead
        kicker={t("evaluation.kicker")}
        title={t(
          creatingNew ? "evalPage.datasets.formTitleCreate" : "evalPage.datasets.title",
        )}
        meta={t(
          creatingNew ? "evalPage.datasets.formSub" : "evalPage.datasets.meta",
        )}
      />
      <EvaluationNav />
      {staleDs.staleId !== null && (
        <StaleLink
          kind={t("staleLink.kind.dataset")}
          id={staleDs.staleId}
          onDismiss={staleDs.dismiss}
        />
      )}
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={creatingNew ? backToList : onBack}>
          ◂ {t(creatingNew ? "evalPage.datasets.title" : "evalPage.backToRuns")}
        </Btn>
      </div>

      {!creatingNew && (
        <Panel
          brk
          pad={false}
          title={t("evalPage.datasets.listTitle")}
          sub={t("evalPage.datasets.listSub")}
          end={
            <Btn primary data-testid="new-dataset-btn" onClick={() => selectDs("new")}>
              + {t("evalPage.datasets.new")}
            </Btn>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table>
            <thead>
              <tr>
                <th>{t("evalPage.datasets.col.name")}</th>
                <th>{t("evalPage.datasets.col.items")}</th>
                <th>{t("evalPage.datasets.col.kind")}</th>
                <th>{t("evalPage.datasets.col.gt")}</th>
                <th>{t("evalPage.datasets.col.cloud")}</th>
              </tr>
            </thead>
            <tbody>
              {pageEntries.filter((entry) => entry.kind === "local").map(({ row }) => (
                <tr
                  key={row.id}
                  data-testid={`dataset-row-${row.id}`}
                  onClick={() => selectDs(row.id)}
                  style={{
                    cursor: "pointer",
                    background:
                      local?.id === row.id ? "rgba(255,176,0,.045)" : undefined,
                  }}
                >
                  <td className="pri">{row.name}</td>
                  <td className="mono dim">{row.item_count}</td>
                  <td className="mono dim">{row.kind}</td>
                  <td>
                    {row.has_ground_truth ? (
                      <Chip tone="aqua" icon="◆">{t("evalPage.datasets.gt")}</Chip>
                    ) : (
                      <span className="mono dim">—</span>
                    )}
                  </td>
                  <td>
                    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                      {cloudChip(row)}
                      {row.cloud?.status !== "deleted" && draftChip(row.cloud?.draft_status)}
                    </span>
                  </td>
                </tr>
              ))}
              {pageEntries.filter((entry) => entry.kind === "cloud").map(({ row }) => (
                <tr
                  key={row.datasetId}
                  data-testid={`dataset-row-cloud-${row.datasetId}`}
                  onClick={() => selectDs(CLOUD_PREFIX + row.datasetId)}
                  style={{
                    cursor: "pointer",
                    background:
                      cloud?.datasetId === row.datasetId ? "rgba(255,176,0,.045)" : undefined,
                  }}
                >
                  <td className="mono">☁ {row.name ?? row.datasetId}</td>
                  <td className="mono dim">{row.exampleCount ?? "—"}</td>
                  <td className="mono dim">
                    {(row.schemaType ?? "").replace("AGENTCORE_EVALUATION_", "")}
                  </td>
                  <td className="mono dim">—</td>
                  <td>
                    <span style={{ display: "inline-flex", gap: 4, flexWrap: "wrap" }}>
                      <Chip tone={row.status === "ACTIVE" ? "good" : "warn"}>{row.status}</Chip>
                      {draftChip(row.draftStatus)}
                    </span>
                  </td>
                </tr>
              ))}
              {loading && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && cloudOnly.length === 0 && !cloudError && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {t("evalPage.datasets.empty")}
                  </td>
                </tr>
              )}
              {cloudError && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {t("evalPage.datasets.cloudUnavailable")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager {...pagerProps} always />
        </Panel>
      )}

      <div className="eval-grid">
        <Panel
          brk
          title={
            local
              ? t("evalPage.datasets.formTitleEdit")
              : cloud
                ? `☁ ${cloud.name ?? cloud.datasetId}`
                : t("evalPage.datasets.formTitleCreate")
          }
          sub={
            local
              ? `${local.id} · ${local.kind}`
              : cloud
                ? (cloud.schemaType ?? "").replace("AGENTCORE_EVALUATION_", "")
                : t("evalPage.datasets.formSub")
          }
          end={
            local ? (
              <>
                <Btn
                  disabled={syncingId !== null}
                  onClick={() => void sync(local)}
                  data-testid={`sync-${local.name}`}
                >
                  {syncingId === local.id
                    ? `◐ ${t("evalPage.datasets.syncing")}`
                    : t("evalPage.datasets.sync")}
                </Btn>
                {local.cloud && local.cloud.status !== "deleted" && (
                  <Btn
                    disabled={publishBlocker(local.cloud.status) !== null}
                    disabledReason={publishBlocker(local.cloud.status) ?? undefined}
                    onClick={() =>
                      void publish(local.id, `/api/eval/datasets/${local.id}/publish-version`)
                    }
                    data-testid={`publish-${local.name}`}
                  >
                    {publishingKey === local.id
                      ? `◐ ${t("evalPage.datasets.publishing")}`
                      : t("evalPage.datasets.publish")}
                  </Btn>
                )}
                <Btn onClick={() => setConfirmLocal(local)}>
                  {t("evalPage.datasets.delete")}
                </Btn>
              </>
            ) : cloud ? (
              <>
                <Btn
                  disabled={publishBlocker(cloudDetail?.status ?? cloud.status) !== null}
                  disabledReason={publishBlocker(cloudDetail?.status ?? cloud.status) ?? undefined}
                  onClick={() =>
                    void publish(
                      CLOUD_PREFIX + cloud.datasetId,
                      `/api/eval/datasets/cloud/${cloud.datasetId}/publish-version`,
                    )
                  }
                  data-testid={`publish-cloud-${cloud.datasetId}`}
                >
                  {publishingKey === CLOUD_PREFIX + cloud.datasetId
                    ? `◐ ${t("evalPage.datasets.publishing")}`
                    : t("evalPage.datasets.publish")}
                </Btn>
                <Btn onClick={() => setConfirmCloud(cloud)}>
                  {t("evalPage.datasets.delete")}
                </Btn>
              </>
            ) : (
              <Btn
                onClick={() => {
                  setEditorMode("form");
                  if (scenarioType === "simulated") {
                    setName("support-personas-sample");
                    setDescription("2 simulated personas (support + billing)");
                    setSimScenarios(SIM_SAMPLE_SCENARIOS());
                  } else {
                    setName("math-gt-sample");
                    setDescription("3 scenarios with ground truth (calculator trajectory)");
                    setScenarios(SAMPLE_SCENARIOS());
                  }
                }}
              >
                {t("evalPage.datasets.prefill")}
              </Btn>
            )
          }
          style={{ "--i": 1 } as CSSProperties}
        >
          {cloud ? (
            <>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.id")}</span>
                <span className="v mono">{cloud.datasetId}</span>
              </div>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.schema")}</span>
                <span className="v mono">{cloud.schemaType ?? "—"}</span>
              </div>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.examples")}</span>
                <span className="v mono">{cloud.exampleCount ?? "—"}</span>
              </div>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.status")}</span>
                <span className="v">
                  <Chip tone={cloud.status === "ACTIVE" ? "good" : "warn"}>{cloud.status}</Chip>
                </span>
              </div>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.draft")}</span>
                <span className="v">
                  {draftChip(cloudDetail?.draft_status ?? cloud.draftStatus) ?? (
                    <span className="mono dim">—</span>
                  )}
                </span>
              </div>
              <div className="kv">
                <span className="k mono">{t("evalPage.datasets.detail.updated")}</span>
                <span className="v mono">
                  {cloud.updatedAt ? new Date(cloud.updatedAt).toLocaleString() : "—"}
                </span>
              </div>
              {versionsList(cloud.datasetId, cloudDetail?.versions)}
              {cloudDetail?.failure_reason && (
                <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span className="mono" style={{ fontSize: 10 }}>{cloudDetail.failure_reason}</span>
                </div>
              )}
              {syncError && (
                <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span>{syncError}</span>
                </div>
              )}
              <div className="note" style={{ marginTop: 10 }}>
                <span className="i">[i]</span>
                <span>{t("evalPage.datasets.cloudReadonly")}</span>
              </div>
            </>
          ) : (
            <>
              {/* import stays create-only: kind is inferred once at creation */}
              {!local && (
                <div className="field">
                  <div className="selchips">
                    <button
                      type="button"
                      className={`selchip${editorMode === "form" ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      onClick={() => setEditorMode("form")}
                    >
                      {t("evalPage.datasets.modeForm")}
                    </button>
                    <button
                      type="button"
                      className={`selchip${editorMode === "import" ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      onClick={() => setEditorMode("import")}
                    >
                      {t("evalPage.datasets.modeImport")}
                    </button>
                  </div>
                </div>
              )}
              {/* type is only choosable at creation — kind is immutable server-side */}
              {!local && editorMode === "form" && (
                <div className="field">
                  <div className="selchips">
                    <button
                      type="button"
                      className={`selchip${scenarioType === "predefined" ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      data-testid="type-predefined"
                      onClick={() => setScenarioType("predefined")}
                    >
                      {t("evalPage.datasets.typePredefined")}
                    </button>
                    <button
                      type="button"
                      className={`selchip${scenarioType === "simulated" ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      data-testid="type-simulated"
                      onClick={() => setScenarioType("simulated")}
                    >
                      {t("evalPage.datasets.typeSimulated")}
                    </button>
                  </div>
                </div>
              )}
              {mixedSimulated ? (
                <div className="note" style={{ borderColor: "var(--warn)" }}>
                  <span className="i" style={{ color: "var(--warn)" }}>[!]</span>
                  <span>{t("evalPage.datasets.simMixedGuard")}</span>
                </div>
              ) : (
                <>
                  <div className="field">
                    <label>{t("evalPage.datasets.name")}</label>
                    <input
                      className="input"
                      aria-label={t("evalPage.datasets.name")}
                      value={name}
                      onChange={(e) => setName(e.target.value)}
                    />
                  </div>
                  <div className="field">
                    <label>{t("evalPage.datasets.description")}</label>
                    <input
                      className="input"
                      aria-label={t("evalPage.datasets.description")}
                      value={description}
                      onChange={(e) => setDescription(e.target.value)}
                    />
                  </div>

                  {editorMode === "import" && !local ? (
                    <div className="field">
                      <label>{t("evalPage.datasets.importLabel")}</label>
                      <textarea
                        className="input mono"
                        rows={9}
                        style={{ fontSize: 10.5, lineHeight: 1.5, resize: "vertical" }}
                        placeholder={'{"scenarios": [...]}  |  {"prompt": "...", "expected": "..."} per line'}
                        value={importText}
                        onChange={(e) => setImportText(e.target.value)}
                      />
                      {importPreview.error ? (
                        <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }}>
                          <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                          <span className="mono" style={{ fontSize: 10.5 }}>{importPreview.error}</span>
                        </div>
                      ) : (
                        <div className="mono dim" style={{ fontSize: 10, marginTop: 6 }}>
                          {t("evalPage.datasets.importPreview", { count: importPreview.items.length })}
                        </div>
                      )}
                    </div>
                  ) : activeType === "simulated" ? (
                    simScenarioEditor
                  ) : (
                    scenarioEditor
                  )}
                </>
              )}

              {local?.cloud && local.cloud.status !== "deleted" && (
                <div
                  data-testid="cloud-copy"
                  style={{
                    border: "1px solid rgba(255,255,255,.08)",
                    borderRadius: 4,
                    padding: "10px 12px",
                    marginTop: 10,
                  }}
                >
                  <div style={{ display: "flex", gap: 6, alignItems: "center", flexWrap: "wrap" }}>
                    <span className="mono dim" style={{ fontSize: 10, letterSpacing: ".06em" }}>
                      {t("evalPage.datasets.cloudCopy")}
                    </span>
                    <span className="mono" style={{ fontSize: 10.5 }}>{local.cloud.dataset_id}</span>
                    {cloudChip(local)}
                    {draftChip(local.cloud.draft_status)}
                    {typeof local.cloud.example_count === "number" && (
                      <span className="mono dim" style={{ fontSize: 10.5 }}>
                        {t("evalPage.datasets.cloudExamples", { count: local.cloud.example_count })}
                      </span>
                    )}
                  </div>
                  <div className="mono dim" style={{ fontSize: 10, marginTop: 4 }}>
                    {t("evalPage.datasets.cloudCopyHint")}
                  </div>
                  {versionsList(local.cloud.dataset_id, local.cloud.versions)}
                </div>
              )}
              {local?.cloud?.failure_reason && (
                <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span className="mono" style={{ fontSize: 10 }}>
                    {local.cloud.failure_reason}
                  </span>
                </div>
              )}
              {syncError && (
                <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span>{syncError}</span>
                </div>
              )}
              {formError && (
                <div className="note" style={{ borderColor: "var(--crit)", margin: "10px 0" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span>{formError}</span>
                </div>
              )}
              {!mixedSimulated && (
                <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 10 }}>
                  <Btn primary disabled={busy || !name.trim()} onClick={() => void save()}>
                    ▸ {local ? t("evalPage.datasets.save") : t("evalPage.datasets.create")}
                  </Btn>
                </div>
              )}
            </>
          )}
        </Panel>

        <Panel
          title={t("evalPage.datasets.how.title")}
          sub={t("evalPage.datasets.how.sub")}
          style={{ "--i": 2 } as CSSProperties}
        >
          {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
            <div className="kv" key={step}>
              <span className="k mono">{`0${i + 1}`}</span>
              <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                {t(`evalPage.datasets.how.${step}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("evalPage.datasets.how.note")}</span>
          </div>
        </Panel>
      </div>

      <ConfirmDialog
        open={confirmLocal !== null}
        title={t("evalPage.datasets.confirmDelete.title")}
        body={t("evalPage.datasets.confirmDelete.body", { name: confirmLocal?.name ?? "" })}
        confirmLabel={t("evalPage.datasets.delete")}
        onConfirm={() => {
          const row = confirmLocal;
          setConfirmLocal(null);
          if (row) void deleteLocal(row);
        }}
        onCancel={() => setConfirmLocal(null)}
      />
      <ConfirmDialog
        open={confirmCloud !== null}
        title={t("evalPage.datasets.confirmCloudDelete.title")}
        body={t("evalPage.datasets.confirmCloudDelete.body", {
          name: confirmCloud?.name ?? confirmCloud?.datasetId ?? "",
        })}
        confirmLabel={t("evalPage.datasets.delete")}
        onConfirm={() => {
          const row = confirmCloud;
          setConfirmCloud(null);
          if (row) void deleteCloud(row);
        }}
        onCancel={() => setConfirmCloud(null)}
      />
      <ConfirmDialog
        open={confirmVersion !== null}
        title={t("evalPage.datasets.confirmVersionDelete.title")}
        body={t("evalPage.datasets.confirmVersionDelete.body", {
          version: confirmVersion?.version ?? "",
        })}
        confirmLabel={t("evalPage.datasets.delete")}
        onConfirm={() => {
          const target = confirmVersion;
          setConfirmVersion(null);
          if (target) void deleteVersion(target.cloudId, target.version);
        }}
        onCancel={() => setConfirmVersion(null)}
      />
    </section>
  );
}
