import { ChevronDown, ChevronRight, File, Folder, FolderOpen, Loader2, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { codingApi, errorMessage } from "../../../lib/api";
import { Alert, LinkButton, Spin } from "../../ui";
import { emptyTree, mergeListing, refreshTargets, ROOT, type TreeState, visibleRows } from "./fileTree";

/**
 * The project's directory tree. The root is listed on mount; a directory is listed
 * the first time it is expanded. `version` bumps (an agent turn changed files, a
 * save) re-list the root and every expanded directory, keeping what is expanded.
 */
export function FileTree({
  projectId,
  version,
  activePath,
  onOpen,
}: {
  projectId: string;
  version: number;
  activePath: string | null;
  onOpen: (path: string) => void;
}) {
  const { t } = useTranslation();
  const [tree, setTree] = useState<TreeState>(emptyTree);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set());
  const [loadingDirs, setLoadingDirs] = useState<Set<string>>(() => new Set());
  const [error, setError] = useState<string | null>(null);
  const [rootLoaded, setRootLoaded] = useState(false);
  const treeRef = useRef(tree);
  treeRef.current = tree;
  const expandedRef = useRef(expanded);
  expandedRef.current = expanded;
  const alive = useRef(true);
  // the latest listing request per directory: a refresh can overlap an earlier listing
  // of the same directory, and an older answer landing last must not win
  const generation = useRef(new Map<string, number>());
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const list = useCallback(
    async (dir: string) => {
      const ticket = (generation.current.get(dir) ?? 0) + 1;
      generation.current.set(dir, ticket);
      const latest = () => alive.current && generation.current.get(dir) === ticket;
      setLoadingDirs((prev) => new Set(prev).add(dir));
      try {
        const res = await codingApi.tree(projectId, dir, 1);
        if (!latest()) return;
        setTree((prev) => mergeListing(prev, dir, res.entries, 1));
        setError(null);
        if (dir === ROOT) setRootLoaded(true);
      } catch (err) {
        if (latest()) setError(errorMessage(err));
      } finally {
        if (latest()) {
          setLoadingDirs((prev) => {
            const next = new Set(prev);
            next.delete(dir);
            return next;
          });
        }
      }
    },
    [projectId],
  );

  const refresh = useCallback(() => {
    for (const dir of refreshTargets(treeRef.current, expandedRef.current)) void list(dir);
  }, [list]);

  useEffect(() => {
    refresh();
  }, [version, refresh]);

  const toggle = (path: string) => {
    const open = !expanded.has(path);
    setExpanded((prev) => {
      const next = new Set(prev);
      if (open) next.add(path);
      else next.delete(path);
      return next;
    });
    if (open && tree.nodes[path]?.children === undefined) void list(path);
  };

  const rows = visibleRows(tree, expanded);

  return (
    <div className="v2-coding-tree" data-testid="v2-coding-tree">
      <div className="v2-coding-pane-head">
        <span>{t("v2.coding.ide.files")}</span>
        <LinkButton onClick={refresh} title={t("v2.common.refresh")} testId="v2-coding-tree-refresh">
          <RefreshCw size={13} aria-hidden="true" />
        </LinkButton>
      </div>
      {error && (
        <Alert tone="error" action={<LinkButton onClick={refresh}>{t("v2.common.retry")}</LinkButton>}>
          {error}
        </Alert>
      )}
      <div className="v2-coding-tree-body" role="tree" aria-label={t("v2.coding.ide.files")}>
        {!rootLoaded && loadingDirs.has(ROOT) && <Spin />}
        {rootLoaded && rows.length === 0 && <div className="v2-coding-tree-empty">{t("v2.coding.ide.noFiles")}</div>}
        {rows.map((row) => {
          const Icon = row.dir ? (row.expanded ? FolderOpen : Folder) : File;
          const Chevron = row.expanded ? ChevronDown : ChevronRight;
          return (
            <button
              key={row.path}
              type="button"
              role="treeitem"
              aria-level={row.depth + 1}
              aria-expanded={row.dir ? row.expanded : undefined}
              aria-selected={row.path === activePath}
              className={row.path === activePath ? "v2-coding-tree-row on" : "v2-coding-tree-row"}
              style={{ paddingLeft: 8 + row.depth * 14 }}
              title={row.path}
              onClick={() => (row.dir ? toggle(row.path) : onOpen(row.path))}
              data-testid={`v2-coding-tree-${row.path}`}
            >
              {row.dir ? (
                loadingDirs.has(row.path) ? (
                  <Loader2 size={12} className="v2-coding-spin" aria-hidden="true" />
                ) : (
                  <Chevron size={12} aria-hidden="true" />
                )
              ) : (
                <span className="v2-coding-tree-gap" />
              )}
              <Icon size={14} aria-hidden="true" className={row.dir ? "dir" : "file"} />
              <span className="name">{row.name}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
