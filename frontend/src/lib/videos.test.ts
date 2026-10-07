import { describe, expect, it } from "vitest";

import { activeNavItem } from "../v2/nav";
import { type LibraryVideo, moduleVideoCollection, type VideoCatalog } from "./videos";

const text = (value: string) => ({ en: value, "zh-CN": value });

function video(id: string, consoleVersion: LibraryVideo["consoleVersion"]): LibraryVideo {
  return {
    id, consoleVersion, title: text(id), description: text(`${id} intro`),
    publishedAt: "2026-10-01T00:00:00Z", durationSeconds: 60, language: "zh-CN",
    posterUrl: "", sources: [], captions: [], chapters: [],
  };
}

const catalog: VideoCatalog = {
  schemaVersion: 3,
  categories: [{ id: "build", title: text("Build") }, { id: "eval", title: text("Eval") }],
  collections: [
    {
      id: "agents", categoryId: "build", path: "/v2/agents", title: text("Agents"),
      description: text(""), videoIds: ["agents-v2", "agents-old", "byoc-v2"],
    },
    {
      id: "eval-data", categoryId: "eval", path: "/v2/eval/data", title: text("Data"),
      description: text(""), videoIds: ["datasets-old"],
    },
  ],
  videos: [
    video("agents-v2", "v2"), video("agents-old", "classic"), video("byoc-v2", "v2"),
    video("datasets-old", "classic"),
  ],
};

describe("moduleVideoCollection", () => {
  it("keeps only the module's V2 recordings in playlist order", () => {
    const collection = moduleVideoCollection(catalog, "/v2/agents");
    expect(collection?.videos.map((item) => item.id)).toEqual(["agents-v2", "byoc-v2"]);
  });

  it("returns null for a module with only classic recordings or none at all", () => {
    expect(moduleVideoCollection(catalog, "/v2/eval/data")).toBeNull();
    expect(moduleVideoCollection(catalog, "/v2/chat")).toBeNull();
  });
});

describe("activeNavItem", () => {
  it("maps sub-pages and classic flows to their sidebar module", () => {
    expect(activeNavItem("/v2/agents/abc", "")?.to).toBe("/v2/agents");
    expect(activeNavItem("/create/studio", "")?.to).toBe("/v2/agents");
    expect(activeNavItem("/v2/eval/tasks", "?view=detail&id=1")?.to).toBe("/v2/eval/tasks");
  });

  it("matches the workbench only exactly and leaves unknown paths unmatched", () => {
    expect(activeNavItem("/v2", "")?.to).toBe("/v2");
    expect(activeNavItem("/v2/nowhere", "")).toBeUndefined();
  });
});
