import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type SkillLabArtifactListing } from "../../../lib/api";
import { saveBlob, sizeLabel } from "../../../lib/skillLab";
import { parentDir } from "../../../lib/skillLabArtifacts";
import { Alert, Button, LinkButton, Modal, Segmented, Table, Tag } from "../../ui";
import { ArtifactMarkdown } from "./ArtifactMarkdown";

type DirListing = Extract<SkillLabArtifactListing, { kind: "dir" }>;
type FileListing = Exclude<SkillLabArtifactListing, { kind: "dir" }>;

/** A successful load, stamped with the job/path it answers and when it landed. */
interface Stamped<T> {
  jobId: string;
  path: string;
  data: T;
  at: number;
}

type ViewMode = "preview" | "source";

interface Entry {
  kind: "dir" | "file";
  name: string;
  size: number | null;
}

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path);
const clock = (at: number) => new Date(at).toLocaleTimeString();

/**
 * Browser over a job's `out/` tree (results.json, report.md, one rollout dir
 * per task). Shown for every job status. A live job writes under us, so nothing
 * here polls: the listing and the open file refresh on demand, and a refresh
 * that fails keeps the last successful load on screen labelled with its time.
 * Every response is checked against the job, path and request generation it was
 * asked for, so a slow answer can never land on the current selection.
 */
export function ArtifactBrowser({ jobId, live = false }: { jobId: string; live?: boolean }) {
  const { t } = useTranslation();

  // Directory navigation is tagged with the job it belongs to: a job switch
  // derives back to the root on the same render.
  const [nav, setNav] = useState({ jobId, dir: "" });
  const dir = nav.jobId === jobId ? nav.dir : "";
  const setDir = useCallback((next: string) => setNav({ jobId, dir: next }), [jobId]);

  const [listing, setListing] = useState<Stamped<DirListing> | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [file, setFile] = useState<Stamped<FileListing> | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("preview");

  const jobRef = useRef(jobId);
  jobRef.current = jobId;
  const listGen = useRef(0);
  const fileGen = useRef(0);

  const shownListing = listing !== null && listing.jobId === jobId ? listing : null;
  const shownFile = file !== null && file.jobId === jobId ? file : null;
  const currentListing = shownListing !== null && shownListing.path === dir ? shownListing : null;

  const loadDir = useCallback(
    async (path: string, refresh: boolean) => {
      const gen = ++listGen.current;
      const forJob = jobRef.current;
      setListLoading(true);
      if (!refresh) {
        setListError(null);
        setListing(null);
      }
      try {
        const result = await api.skillLabJobArtifacts(forJob, path);
        if (gen !== listGen.current || jobRef.current !== forJob) return;
        if (result.kind === "dir") {
          setListing({ jobId: forJob, path, data: result, at: Date.now() });
          setListError(null);
        } else {
          setListError(t("skillLab.eval.artifacts.notADir"));
        }
      } catch (err) {
        if (gen !== listGen.current || jobRef.current !== forJob) return;
        setListError(errorMessage(err));
      } finally {
        if (gen === listGen.current && jobRef.current === forJob) setListLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    void loadDir(dir, false);
  }, [jobId, dir, loadDir]);

  useEffect(() => {
    fileGen.current += 1;
    setFileLoading(false);
    setFileError(null);
  }, [jobId]);

  const closeViewer = useCallback(() => {
    fileGen.current += 1; // an open/refresh still in flight can no longer reopen it
    setFile(null);
    setFileError(null);
    setFileLoading(false);
  }, []);

  /** Open a path from the listing or a relative Markdown link: a file fills the
   *  viewer, a directory navigates the listing (and closes the viewer). */
  const openPath = useCallback(async (path: string, refresh = false) => {
    const gen = ++fileGen.current;
    const forJob = jobRef.current;
    setFileLoading(true);
    if (!refresh) setFileError(null);
    try {
      const result = await api.skillLabJobArtifacts(forJob, path);
      if (gen !== fileGen.current || jobRef.current !== forJob) return;
      if (result.kind === "dir") {
        setFile(null);
        setFileError(null);
        setNav({ jobId: forJob, dir: path });
      } else {
        setFile({ jobId: forJob, path, data: result, at: Date.now() });
        setFileError(null);
      }
    } catch (err) {
      if (gen !== fileGen.current || jobRef.current !== forJob) return;
      setFileError(errorMessage(err));
    } finally {
      if (gen === fileGen.current && jobRef.current === forJob) setFileLoading(false);
    }
  }, []);

  const download = async (path: string, name: string) => {
    setListError(null);
    try {
      saveBlob(await api.skillLabJobArtifactRaw(jobId, path), name);
    } catch (err) {
      setListError(errorMessage(err));
    }
  };

  const segments = dir ? dir.split("/") : [];
  const viewerIsMarkdown = shownFile !== null && isMarkdown(shownFile.path);
  const viewerMode: ViewMode = viewerIsMarkdown ? mode : "source";
  const openFailed = shownFile === null && fileError !== null;
  const entries: Entry[] = currentListing
    ? [
        ...currentListing.data.dirs.map((name) => ({ kind: "dir" as const, name, size: null })),
        ...currentListing.data.files.map((f) => ({ kind: "file" as const, name: f.name, size: f.size })),
      ]
    : [];

  return (
    <div data-testid="v2-skilllab-artifacts">
      <div className="v2-toolbar">
        <div className="v2-skilllab-crumbs mono">
          <LinkButton disabled={dir === ""} onClick={() => setDir("")} testId="v2-artifact-crumb-root">
            out/
          </LinkButton>
          {segments.map((segment, index) => (
            <span key={`${segment}-${index}`}>
              <LinkButton
                disabled={index === segments.length - 1}
                onClick={() => setDir(segments.slice(0, index + 1).join("/"))}
              >
                {segment}
              </LinkButton>
              {index < segments.length - 1 && <span className="v2-muted">/</span>}
            </span>
          ))}
        </div>
        <div className="end">
          <span className="v2-count">
            {listLoading
              ? t("v2.common.loading")
              : currentListing
                ? t("skillLab.eval.artifacts.refreshedAt", { time: clock(currentListing.at) })
                : ""}
          </span>
          <Button size="sm" disabled={listLoading} onClick={() => void loadDir(dir, true)} testId="v2-artifact-refresh">
            {t("skillLab.eval.artifacts.refresh")}
          </Button>
        </div>
      </div>
      {live && <Alert>{t("skillLab.eval.artifacts.liveHint")}</Alert>}
      {listError !== null && (
        <Alert tone="error">
          {listError}
          {currentListing !== null && ` ${t("skillLab.eval.artifacts.staleListing", { time: clock(currentListing.at) })}`}
        </Alert>
      )}
      {openFailed && <Alert tone="error">{fileError}</Alert>}
      <Table
        density="dense"
        columns={[
          {
            key: "name",
            title: t("skillLab.eval.artifacts.name"),
            render: (e: Entry) =>
              e.kind === "dir" ? (
                <LinkButton onClick={() => setDir(join(dir, e.name))} testId={`v2-artifact-dir-${e.name}`}>
                  <span className="mono">{e.name}/</span>
                </LinkButton>
              ) : (
                <LinkButton onClick={() => void openPath(join(dir, e.name))} testId={`v2-artifact-open-${e.name}`}>
                  <span className="mono">{e.name}</span>
                </LinkButton>
              ),
          },
          {
            key: "size",
            title: t("skillLab.eval.artifacts.size"),
            className: "num",
            width: 120,
            render: (e: Entry) => (e.size === null ? "—" : sizeLabel(e.size)),
          },
          {
            key: "ops",
            title: t("v2.common.actions"),
            className: "right",
            width: 100,
            render: (e: Entry) =>
              e.kind === "file" ? (
                <LinkButton onClick={() => void download(join(dir, e.name), e.name)}>
                  {t("skillLab.eval.artifacts.download")}
                </LinkButton>
              ) : null,
          },
        ]}
        rows={entries}
        rowKey={(e) => `${e.kind}:${e.name}`}
        loading={listLoading && currentListing === null}
        empty={dir === "" && live ? t("skillLab.eval.artifacts.emptyLive") : t("skillLab.eval.artifacts.empty")}
        testId="v2-artifact-listing"
      />

      <Modal
        open={shownFile !== null}
        wide
        title={<span className="mono ellipsis">{shownFile?.path}</span>}
        onClose={closeViewer}
        testId="v2-artifact-viewer"
        footer={
          shownFile && (
            <>
              <Button disabled={fileLoading} onClick={() => void openPath(shownFile.path, true)}>
                {t("skillLab.eval.artifacts.refresh")}
              </Button>
              <Button onClick={() => void download(shownFile.path, shownFile.path.split("/").pop() || "artifact")}>
                {t("skillLab.eval.artifacts.download")}
              </Button>
              <Button kind="primary" onClick={closeViewer}>
                {t("v2.common.close")}
              </Button>
            </>
          )
        }
      >
        {shownFile && (
          <>
            <div className="v2-row" style={{ marginBottom: 10 }}>
              <Tag tone={shownFile.data.kind === "text" ? "blue" : "gray"}>{shownFile.data.kind}</Tag>
              <span className="v2-muted">{sizeLabel(shownFile.data.size)}</span>
              {shownFile.data.kind === "text" && shownFile.data.truncated && (
                <Tag tone="orange">{t("skillLab.eval.artifacts.truncated")}</Tag>
              )}
              {viewerIsMarkdown && (
                <Segmented
                  value={viewerMode}
                  onChange={setMode}
                  options={(["preview", "source"] as ViewMode[]).map((m) => ({
                    value: m,
                    label: t(`skillLab.eval.artifacts.${m}`),
                  }))}
                />
              )}
              <span className="v2-muted" style={{ marginLeft: "auto" }}>
                {fileLoading ? t("v2.common.loading") : t("skillLab.eval.artifacts.refreshedAt", { time: clock(shownFile.at) })}
              </span>
            </div>
            {fileError !== null && (
              <Alert tone="error">
                {fileError} {t("skillLab.eval.artifacts.staleFile", { time: clock(shownFile.at) })}
              </Alert>
            )}
            {shownFile.data.kind !== "text" ? (
              <Alert>{t("skillLab.eval.artifacts.binary")}</Alert>
            ) : viewerMode === "preview" ? (
              <ArtifactMarkdown
                text={shownFile.data.content}
                baseDir={parentDir(shownFile.path)}
                onOpenArtifact={(path) => void openPath(path)}
              />
            ) : (
              <pre className="v2-pre" style={{ maxHeight: "60vh" }} data-testid="v2-artifact-source">
                {shownFile.data.content}
              </pre>
            )}
          </>
        )}
      </Modal>
    </div>
  );
}
