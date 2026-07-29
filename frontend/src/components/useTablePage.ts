import { useEffect, useState } from "react";

import { PAGE_SIZES } from "./Pager";

/**
 * Client-side paging for a table whose rows are already fully loaded.
 *
 * Returns the visible slice plus ready-made `<Pager>` props. `selectedIndex` is
 * the index of the row a `?param=` selection points at (-1 when nothing is
 * selected): a deep link may target a row on a later page, and the table must
 * follow it rather than strand the selection off-screen.
 */
export function useTablePage<T>(
  items: T[],
  selectedIndex = -1,
  initialSize: number = PAGE_SIZES[0],
) {
  const [size, setSize] = useState(initialSize);
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(items.length / size));
  const current = Math.min(page, pages);
  useEffect(() => {
    if (selectedIndex >= 0) setPage(Math.floor(selectedIndex / size) + 1);
  }, [selectedIndex, size]);
  return {
    rows: items.slice((current - 1) * size, current * size),
    pagerProps: {
      total: items.length,
      page: current,
      size,
      onPage: setPage,
      onSize: (next: number) => {
        setSize(next);
        setPage(1);
      },
    },
  };
}
