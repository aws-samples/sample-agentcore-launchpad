import { Component, Suspense, useEffect } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { LoadError } from "../components";
import {
  browserIsOffline,
  canReloadOnce,
  clearReloadMark,
  markReloaded,
  pageReloader,
} from "./chunk-reload";

/**
 * A page chunk that never arrived — not a bug in the page.
 *
 * The two real causes are both recoverable by reloading: the box was rebuilt
 * under an open tab, so the content-hashed chunk the loaded shell asks for is
 * gone from `dist/assets` (nginx answers 404 for it since WB-003 — see
 * infra/ec2/nginx-site.sh), or the dev/preview server is unreachable.
 * Anything else a page throws is re-thrown untouched, so a genuine render bug
 * still surfaces as it did before this boundary existed instead of being
 * mislabelled as staleness.
 */
function isChunkLoadError(error: unknown): boolean {
  const msg = error instanceof Error ? error.message : String(error);
  return /dynamically imported module|Importing a module script failed|Unable to preload/i.test(
    msg,
  );
}

/** Placeholder while a page chunk is in flight — content area only. */
function RoutePending() {
  const { t } = useTranslation();
  return (
    <div className="route-pending" role="status" data-testid="route-pending">
      {t("routeChunk.loading")}
    </div>
  );
}

/** Shown for the instant between the stale-chunk error and the automatic reload. */
function RouteReloading() {
  const { t } = useTranslation();
  return (
    <div className="route-pending" role="status" data-testid="route-chunk-reloading">
      {t("routeChunk.reloading")}
    </div>
  );
}

/** The shared `LoadError` block, with RELOAD in place of RETRY. */
function RouteChunkError() {
  const { t } = useTranslation();
  return (
    <LoadError
      message={t("routeChunk.failed")}
      onRetry={() => pageReloader.reload()}
      retryLabel={t("routeChunk.reload")}
      data-testid="route-chunk-error"
    />
  );
}

/** Mounted as the Suspense boundary's last child: with `createRoot`, its effect
 * runs only when every sibling resolved — i.e. the page chunk really loaded. */
function ChunkLoaded() {
  useEffect(() => {
    clearReloadMark(window.location.pathname);
  }, []);
  return null;
}

type BoundaryState = { error: unknown; reloading: boolean };

class ChunkErrorBoundary extends Component<{ children: ReactNode }, BoundaryState> {
  state: BoundaryState = { error: null, reloading: false };

  static getDerivedStateFromError(error: unknown): BoundaryState {
    return {
      error,
      reloading:
        isChunkLoadError(error) && !browserIsOffline() && canReloadOnce(window.location.pathname),
    };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    if (!isChunkLoadError(error)) {
      console.error("[route] page failed to render", error, info.componentStack);
      return;
    }
    const pathname = window.location.pathname;
    if (browserIsOffline()) {
      console.warn("[route] page chunk missing while offline — manual state", pathname);
      return;
    }
    if (!canReloadOnce(pathname)) {
      console.warn("[route] page chunk still missing after a reload — manual state", pathname);
      return;
    }
    console.warn("[route] page chunk missing — reloading once", pathname);
    markReloaded(pathname);
    pageReloader.reload();
  }

  render() {
    const { error, reloading } = this.state;
    if (!error) return this.props.children;
    if (!isChunkLoadError(error)) throw error;
    return reloading ? <RouteReloading /> : <RouteChunkError />;
  }
}

/**
 * The Shell's content-area boundary for lazily loaded pages: a translated
 * pending line while the chunk is in flight, one automatic reload when it
 * cannot be fetched (none while the browser is offline), the manual reload
 * state after that. Sits inside `.view`
 * so the sidebar, topbar and footer never move.
 */
export function RouteChunk({ children }: { children: ReactNode }) {
  return (
    <ChunkErrorBoundary>
      <Suspense fallback={<RoutePending />}>
        {children}
        <ChunkLoaded />
      </Suspense>
    </ChunkErrorBoundary>
  );
}
