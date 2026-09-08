import { Component, Suspense } from "react";
import type { ErrorInfo, ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { LoadError } from "../components";

/**
 * A page chunk that never arrived — not a bug in the page.
 *
 * The two real causes are both recoverable by reloading: the box was rebuilt
 * under an open tab, so the content-hashed chunk the loaded shell asks for is
 * gone from `dist/assets` (prod serves a built `dist/` through `vite preview`,
 * see docs/agent-runbook-prod.md), or the dev/preview server is unreachable.
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

/** The shared `LoadError` block, with RELOAD in place of RETRY. */
function RouteChunkError() {
  const { t } = useTranslation();
  return (
    <LoadError
      message={t("routeChunk.failed")}
      onRetry={() => window.location.reload()}
      retryLabel={t("routeChunk.reload")}
      data-testid="route-chunk-error"
    />
  );
}

class ChunkErrorBoundary extends Component<{ children: ReactNode }, { error: unknown }> {
  state: { error: unknown } = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error };
  }

  componentDidCatch(error: unknown, info: ErrorInfo) {
    console.error("[route] page failed to render", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (!isChunkLoadError(error)) throw error;
    return <RouteChunkError />;
  }
}

/**
 * The Shell's content-area boundary for lazily loaded pages: a translated
 * pending line while the chunk is in flight, the reload state when it cannot be
 * fetched. Sits inside `.view` so the sidebar, topbar and footer never move.
 */
export function RouteChunk({ children }: { children: ReactNode }) {
  return (
    <ChunkErrorBoundary>
      <Suspense fallback={<RoutePending />}>{children}</Suspense>
    </ChunkErrorBoundary>
  );
}
