import type { MemoryNamespace, MemoryRecord } from "./api";

/** Read-only summary identity and stale-response guards shared by both consoles. */
export function summaryNamespaceKey(namespace: MemoryNamespace): string {
  return JSON.stringify([namespace.source ?? "", namespace.strategy_id, namespace.namespace]);
}

export function summaryNamespaces(namespaces: MemoryNamespace[]): MemoryNamespace[] {
  return namespaces.filter((n) => n.strategy_type === "SUMMARIZATION" && n.resolvable && !n.prefix);
}

export function isFullSession(record: MemoryRecord): boolean {
  return record.record_type === "FULL_SESSION";
}

export function summaryPageStatus(records: readonly MemoryRecord[]): "ready" | "pending" {
  return records.length ? "ready" : "pending";
}

export interface MemoryRecordSelection { key: string; record: MemoryRecord }

/** Do not expose a previous scope's open drawer while reset effects are pending. */
export function scopedMemoryRecord(selection: MemoryRecordSelection | null, key: string): MemoryRecord | null {
  return selection?.key === key ? selection.record : null;
}

export class MemoryRequestScope {
  private key = "";
  private generation = 0;

  enter(key: string) {
    if (this.key !== key) {
      this.key = key;
      this.invalidate();
    }
  }

  invalidate() { this.generation += 1; }

  begin(): () => boolean {
    const generation = ++this.generation;
    return () => this.generation === generation;
  }
}
