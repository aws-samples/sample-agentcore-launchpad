import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { LibraryVideo, VideoCollection, VideoLocale } from "../../../lib/videos";
import { Card, FlowHeader, LinkButton, Segmented, Tag } from "../../ui";
import { VideoChapterList, VideoEpisodeList, VideoSummary } from "./playback";
import { useVideoPlayback } from "./useVideoPlayback";

/**
 * 观看视频 (`?view=watch&video=`) — the shared playback (`./playback`) plus the
 * series playlist. Keyed on the video id by the router, so switching episodes remounts it.
 */
export function VideoPlayer({
  video,
  collection,
  locale,
  onSelect,
  onBack,
}: {
  video: LibraryVideo;
  collection: VideoCollection;
  locale: VideoLocale;
  onSelect: (id: string) => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const { media, alerts, mediaFailed, playChapter } = useVideoPlayback(video, locale);
  const hasSeries = collection.videos.length > 1;
  const [directory, setDirectory] = useState<"series" | "chapters">(hasSeries ? "series" : "chapters");
  const index = collection.videos.findIndex((item) => item.id === video.id);
  const previous = collection.videos[index - 1];
  const next = collection.videos[index + 1];

  return (
    <section aria-label={t("videos.player")} data-testid="video-watch">
      <FlowHeader
        onBack={onBack}
        title={collection.title[locale]}
        end={
          <div className="v2-row">
            <Tag tone={video.consoleVersion === "v2" ? "blue" : "gray"}>
              {t(`videos.version.${video.consoleVersion}`)}
            </Tag>
            {hasSeries && <Tag tone="blue">{t("videos.episodePosition", { index: index + 1, count: collection.videos.length })}</Tag>}
          </div>
        }
      />
      <div className="v2-videos-layout">
        <Card flush testId="video-player-panel">
          {media}
          <div className="v2-videos-details">
            {alerts}
            <VideoSummary video={video} locale={locale} />
            {hasSeries && (
              <nav className="v2-videos-pagination" aria-label={t("videos.series")}>
                {previous ? (
                  <button type="button" onClick={() => onSelect(previous.id)} data-testid="video-previous">
                    <span>
                      <ChevronLeft size={14} aria-hidden="true" />
                      {t("videos.previous")}
                    </span>
                    <strong>{previous.title[locale]}</strong>
                  </button>
                ) : (
                  <span />
                )}
                {next && (
                  <button type="button" className="next" onClick={() => onSelect(next.id)} data-testid="video-next">
                    <span>
                      {t("videos.next")}
                      <ChevronRight size={14} aria-hidden="true" />
                    </span>
                    <strong>{next.title[locale]}</strong>
                  </button>
                )}
              </nav>
            )}
          </div>
        </Card>

        <Card
          flush
          title={collection.title[locale]}
          sub={t("videos.episodeCount", { count: collection.videos.length })}
          testId="video-directory"
        >
          <div className="v2-videos-dir-head">
            {hasSeries ? (
              <Segmented
                value={directory}
                onChange={setDirectory}
                options={[
                  { value: "series", label: t("videos.series") },
                  { value: "chapters", label: t("videos.episodeChapters") },
                ]}
              />
            ) : (
              <span className="v2-videos-dir-title">{t("videos.episodeChapters")}</span>
            )}
          </div>
          {hasSeries && directory === "series" && (
            <VideoEpisodeList videos={collection.videos} currentId={video.id} locale={locale} onSelect={onSelect} />
          )}
          {directory === "chapters" && (
            <VideoChapterList video={video} locale={locale} disabled={mediaFailed} onSeek={playChapter} />
          )}
          <div className="v2-videos-dir-foot">
            <LinkButton onClick={onBack}>{t("videos.changeModule")}</LinkButton>
          </div>
        </Card>
      </div>
    </section>
  );
}
