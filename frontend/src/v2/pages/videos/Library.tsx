import { Inbox, PlayCircle } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type LibraryVideo, videoCatalog, videoCollections, type VideoLocale, videoTimestamp } from "../../../lib/videos";
import { Alert, Button, Card, SearchInput, Segmented, Tag } from "../../ui";

interface Result {
  id: string;
  title: string;
  description: string;
  videos: LibraryVideo[];
}

/**
 * 视频库 — collections grouped by category; a search flattens to matching episodes.
 * Filters live in `?q=` / `?category=` so returning from the player restores them.
 */
export function VideoLibrary({
  locale,
  query,
  category,
  invalidLink,
  onFilter,
  onClear,
  onWatch,
}: {
  locale: VideoLocale;
  query: string;
  category: string | null;
  invalidLink: boolean;
  onFilter: (name: "q" | "category", value: string) => void;
  onClear: () => void;
  onWatch: (id: string) => void;
}) {
  const { t } = useTranslation();
  const videos = videoCatalog.videos;
  const search = query.trim().toLocaleLowerCase(locale);
  const collections = videoCollections.filter((item) => !category || item.categoryId === category);
  const results: Result[] = collections.flatMap((item) =>
    search
      ? item.videos
          .filter((video) => `${item.title[locale]} ${video.title[locale]}`.toLocaleLowerCase(locale).includes(search))
          .map((video) => ({ id: video.id, title: video.title[locale], description: item.title[locale], videos: [video] }))
      : [{ id: item.id, title: item.title[locale], description: item.description[locale], videos: item.videos }],
  );

  if (videos.length === 0) {
    return (
      <Card>
        <div className="v2-table-empty" data-testid="videos-empty">
          <Inbox size={32} aria-hidden="true" />
          <div>{t("videos.empty")}</div>
          <div>{t("videos.emptyHint")}</div>
        </div>
      </Card>
    );
  }

  const countFor = (id: string) =>
    videoCollections.filter((c) => c.categoryId === id).reduce((n, c) => n + c.videos.length, 0);

  return (
    <section aria-label={t("videos.libraryTitle")} data-testid="video-library">
      {invalidLink && (
        <Alert tone="warn">
          <span data-testid="video-invalid-link">{t("videos.invalidLink")}</span>
        </Alert>
      )}
      <Card>
        <div className="v2-toolbar">
          <Segmented
            value={category ?? ""}
            onChange={(value) => onFilter("category", value)}
            options={[
              { value: "", label: `${t("videos.all")} · ${videos.length}` },
              ...videoCatalog.categories.map((item) => ({
                value: item.id,
                label: `${item.title[locale]} · ${countFor(item.id)}`,
              })),
            ]}
          />
          <div className="end">
            <SearchInput value={query} onChange={(value) => onFilter("q", value)} placeholder={t("videos.search")} testId="video-search" />
            <span className="v2-count" role="status">
              {search
                ? `${t("videos.searchResults")} · ${t("videos.count", { count: results.length })}`
                : t("videos.collectionCount", { count: results.length })}
            </span>
          </div>
        </div>
        {results.length > 0 ? (
          <ul className="v2-videos-grid">
            {results.map((item) => {
              const first = item.videos[0];
              const duration = item.videos.reduce((total, video) => total + video.durationSeconds, 0);
              return (
                <li key={item.id}>
                  <button type="button" className="v2-videos-card" onClick={() => onWatch(first.id)} data-testid={`video-card-${item.id}`}>
                    <span className="thumb">
                      <img src={first.posterUrl} alt="" loading="lazy" />
                      <PlayCircle className="play" size={40} aria-hidden="true" />
                      <span className="len">{videoTimestamp(duration)}</span>
                    </span>
                    <span className="text">
                      <strong>{item.title}</strong>
                      <span className="desc">{item.description}</span>
                      {item.videos.length > 1 && (
                        <span className="tags">
                          <Tag tone="blue">{t("videos.episodeCount", { count: item.videos.length })}</Tag>
                        </span>
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        ) : (
          <div className="v2-table-empty" data-testid="videos-no-results">
            <Inbox size={32} aria-hidden="true" />
            <div>{t("videos.noResults")}</div>
            <div>{t("videos.noResultsHint")}</div>
            <div style={{ marginTop: 12 }}>
              <Button onClick={onClear}>{t("videos.clearFilters")}</Button>
            </div>
          </div>
        )}
      </Card>
    </section>
  );
}
