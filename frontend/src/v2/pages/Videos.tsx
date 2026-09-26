import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { videoCatalog, videoCollections, type VideoLocale } from "../../lib/videos";
import { PageHeader } from "../ui";
import { VideoLibrary } from "./videos/Library";
import { VideoPlayer } from "./videos/Player";
import "./videos/videos.css";

/**
 * 视频 — the bundled product-demo catalog (`config/videos.json`), visible to every user.
 * `?view=watch&video=<id>` opens the player; `?q=` / `?category=` filter the library and
 * are kept while watching so going back restores them.
 */
export function V2Videos() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const requestedId = params.get("video");
  const selected = videoCatalog.videos.find((video) => video.id === requestedId);
  const collection = videoCollections.find((item) => item.videos.some((v) => v.id === selected?.id));
  const rawCategory = params.get("category");
  const category = videoCatalog.categories.some((item) => item.id === rawCategory) ? rawCategory : null;

  const withParams = (edit: (next: URLSearchParams) => void, replace = false) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        edit(next);
        return next;
      },
      { replace },
    );
  const watch = (id: string) =>
    withParams((next) => {
      next.set("view", "watch");
      next.set("video", id);
    });
  const back = () =>
    withParams((next) => {
      next.delete("view");
      next.delete("video");
    });

  if (selected && collection) {
    return (
      <VideoPlayer
        key={selected.id}
        video={selected}
        collection={collection}
        locale={locale}
        onSelect={watch}
        onBack={back}
      />
    );
  }
  return (
    <>
      <PageHeader
        title={t("videos.title")}
        desc={t("videos.description")}
        end={<span className="v2-count">{t("videos.count", { count: videoCatalog.videos.length })}</span>}
      />
      <VideoLibrary
        locale={locale}
        query={params.get("q") ?? ""}
        category={category}
        invalidLink={params.get("video") !== null}
        onFilter={(name, value) =>
          withParams((next) => {
            next.delete("view");
            next.delete("video");
            if (value) next.set(name, value);
            else next.delete(name);
          }, true)
        }
        onClear={() => withParams((next) => ["q", "category", "view", "video"].forEach((n) => next.delete(n)), true)}
        onWatch={watch}
      />
    </>
  );
}
