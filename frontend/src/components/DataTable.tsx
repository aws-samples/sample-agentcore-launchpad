import type { ReactNode } from "react";

import { LoadError } from "./LoadError";

export interface Column {
  key: string;
  label: ReactNode;
}

interface DataTableProps {
  columns: Column[];
  empty?: ReactNode;
  isEmpty?: boolean;
  /** Set when the rows failed to load: renders the shared error block in
   *  place of `empty` (a failed fetch must never read as an empty table). */
  error?: string | null;
  onRetry?: () => void;
  children?: ReactNode;
}

export function DataTable({
  columns,
  empty,
  isEmpty = false,
  error = null,
  onRetry,
  children,
}: DataTableProps) {
  if (error != null && isEmpty) {
    return (
      <div>
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key}>{c.label}</th>
              ))}
            </tr>
          </thead>
        </table>
        <LoadError message={error} onRetry={onRetry} inline />
      </div>
    );
  }
  if (isEmpty && empty != null) {
    return (
      <div>
        <table>
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key}>{c.label}</th>
              ))}
            </tr>
          </thead>
        </table>
        <div className="empty">{empty}</div>
      </div>
    );
  }
  return (
    <table>
      <thead>
        <tr>
          {columns.map((c) => (
            <th key={c.key}>{c.label}</th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  );
}
