/** Registry → "Import Gateway targets" (WB-005): the one state machine behind the V2 and
 *  classic dialogs, so both consoles behave identically — load the inventory, pick a
 *  gateway (platform first), tick targets (all by default), run the import, show results.
 *  Pure helpers are exported for tests and for the per-item result labels. */
import { useCallback, useEffect, useMemo, useState } from "react";

import {
  api,
  errorMessage,
  type GatewayImportGateway,
  type GatewayImportPreview,
  type GatewayImportResponse,
  type GatewayImportResult,
  type GatewayImportTarget,
} from "./api";

export type ImportResultState = "created" | "already" | "reconciled" | "failed";

/** created / already imported / reconciled / failed — the label of one result row. */
export function resultState(item: GatewayImportResult): ImportResultState {
  if (item.error) return "failed";
  if (item.created) return "created";
  if (item.reconciled) return "reconciled";
  return "already";
}

export interface ImportSummary {
  created: number;
  already: number;
  reconciled: number;
  failed: number;
}

export function summarize(results: GatewayImportResult[]): ImportSummary {
  const out: ImportSummary = { created: 0, already: 0, reconciled: 0, failed: 0 };
  for (const item of results) out[resultState(item)] += 1;
  return out;
}

/** Targets the dialog can tick: AWS gave them an id (always, in practice). */
export function selectableIds(gateway: GatewayImportGateway | null): string[] {
  return (gateway?.targets ?? []).flatMap((t) => (t.target_id ? [t.target_id] : []));
}

export type PreviewTargetState = "new" | "imported" | "held";

export function previewState(target: GatewayImportTarget): PreviewTargetState {
  if (target.held) return "held";
  if (target.imported) return "imported";
  return "new";
}

/** N17-obs261: how many of the TICKED targets are member-submitted records the caller's
 *  import would approve — the dialog's one-line warning above the table. */
export function memberApprovals(gateway: GatewayImportGateway | null, selected: Set<string>): number {
  return (gateway?.targets ?? []).filter(
    (t) => t.will_approve && t.origin === "member" && !!t.target_id && selected.has(t.target_id),
  ).length;
}

export interface GatewayImportController {
  loading: boolean;
  loadError: string | null;
  preview: GatewayImportPreview | null;
  gateway: GatewayImportGateway | null;
  gatewayId: string;
  pickGateway: (gatewayId: string) => void;
  selected: Set<string>;
  toggle: (targetId: string) => void;
  selectAll: () => void;
  selectNone: () => void;
  importing: boolean;
  importError: string | null;
  result: GatewayImportResponse | null;
  /** `POST /api/registry/import-gateway` for the ticked targets of the picked gateway */
  run: () => Promise<void>;
}

export function useGatewayImport(open: boolean): GatewayImportController {
  const [preview, setPreview] = useState<GatewayImportPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [gatewayId, setGatewayId] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [result, setResult] = useState<GatewayImportResponse | null>(null);

  useEffect(() => {
    if (!open) return;
    let live = true;
    setPreview(null);
    setLoadError(null);
    setResult(null);
    setImportError(null);
    setLoading(true);
    api
      .registryImportGatewayPreview()
      .then((body) => {
        if (!live) return;
        const first = body.gateways[0] ?? null; // the server lists the platform gateway first
        setPreview(body);
        setGatewayId(first?.gateway_id ?? "");
        setSelected(new Set(selectableIds(first)));
      })
      .catch((err: unknown) => live && setLoadError(errorMessage(err)))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [open]);

  const gateway = useMemo(
    () => preview?.gateways.find((g) => g.gateway_id === gatewayId) ?? null,
    [preview, gatewayId],
  );

  const pickGateway = useCallback(
    (next: string) => {
      setGatewayId(next);
      setSelected(new Set(selectableIds(preview?.gateways.find((g) => g.gateway_id === next) ?? null)));
    },
    [preview],
  );
  const toggle = useCallback((targetId: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(targetId)) next.delete(targetId);
      else next.add(targetId);
      return next;
    });
  }, []);
  const selectAll = useCallback(() => setSelected(new Set(selectableIds(gateway))), [gateway]);
  const selectNone = useCallback(() => setSelected(new Set()), []);

  const run = useCallback(async () => {
    if (!gatewayId || selected.size === 0) return;
    setImporting(true);
    setImportError(null);
    try {
      const body = await api.registryImportGateway({ gateway_id: gatewayId, target_ids: [...selected] });
      setResult(body);
    } catch (err) {
      setImportError(errorMessage(err));
    } finally {
      setImporting(false);
    }
  }, [gatewayId, selected]);

  return {
    loading,
    loadError,
    preview,
    gateway,
    gatewayId,
    pickGateway,
    selected,
    toggle,
    selectAll,
    selectNone,
    importing,
    importError,
    result,
    run,
  };
}
