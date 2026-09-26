import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useVideoCatalog } from "../../lib/useVideoCatalog";
import { videoCollections, videoCollectionsByVersion, type VideoLocale, type VideoVersionFilter } from "../../lib/videos";
import { Alert, Button, PageHeader, Spin } from "../ui";
import { VideoLibrary } from "./videos/Library";
import { VideoPlayer } from "./videos/Player";
import "./videos/videos.css";

/**
 * 视频 — published hub-global directory, visible to every signed-in user.
 * `?view=watch&video=<id>` opens the player; `?q=` / `?category=` filter the library and
 * are kept while watching so going back restores them.
 */
export function V2Videos() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const loaded = useVideoCatalog();
  const catalog = loaded.data;
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const requestedId = params.get("video");
  const collections = catalog ? videoCollections(catalog) : [];
  const selected = catalog?.videos.find((video) => video.id === requestedId);
  const rawVersion = params.get("version");
  const version: VideoVersionFilter = rawVersion === "classic" || rawVersion === "all" ? rawVersion : "v2";
  const visibleCollections = videoCollectionsByVersion(collections, version);
  const collection = selected && videoCollectionsByVersion(collections, selected.consoleVersion)
    .find((item) => item.videos.some((video) => video.id === selected.id));
  const rawCategory = params.get("category");
  const category = catalog?.categories.some((item) => item.id === rawCategory) ? rawCategory : null;
  const rawSection = params.get("section");
  const section = collections.some((item) => item.id === rawSection && (!category || item.categoryId === category))
    ? rawSection : null;

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
      if (next.get("version") !== "all") {
        const video = catalog?.videos.find((item) => item.id === id);
        if (video) next.set("version", video.consoleVersion);
      }
    });
  const back = () =>
    withParams((next) => {
      next.delete("view");
      next.delete("video");
      if (next.get("version") !== "all" && selected) next.set("version", selected.consoleVersion);
    });

  if (!catalog) {
    return (
      <>
        <PageHeader title={t("videos.title")} desc={t("videos.description")} />
        {loaded.loading ? <Spin /> : (
          <Alert tone="error" action={<Button onClick={loaded.reload}>{t("v2.common.retry")}</Button>}>
            {loaded.error ?? t("videos.loadFailed")}
          </Alert>
        )}
      </>
    );
  }

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
        end={<span className="v2-count">{t("videos.count", {
          count: version === "all" ? catalog.videos.length
            : catalog.videos.filter((video) => video.consoleVersion === version).length,
        })}</span>}
      />
      {loaded.error && (
        <Alert tone="error" action={<Button onClick={loaded.reload}>{t("v2.common.retry")}</Button>}>
          {loaded.error}
        </Alert>
      )}
      <VideoLibrary
        catalog={catalog}
        collections={visibleCollections}
        locale={locale}
        version={version}
        query={params.get("q") ?? ""}
        category={category}
        section={section}
        invalidLink={params.get("video") !== null}
        onFilter={(name, value) =>
          withParams((next) => {
            next.delete("view");
            next.delete("video");
            if (name === "category" || name === "version") next.delete("section");
            if (value) next.set(name, value);
            else next.delete(name);
          }, true)
        }
        onClear={() => withParams((next) => ["q", "category", "section", "version", "view", "video"].forEach((n) => next.delete(n)), true)}
        onWatch={watch}
      />
    </>
  );
}
