import type { KeyboardEvent } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip } from "../../components";
import type { SkillLabArtifactListing } from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import { parentDir } from "./artifactLinks";
import { ArtifactMarkdown } from "./ArtifactMarkdown";

type DirListing = Extract<SkillLabArtifactListing, { kind: "dir" }>;
type FileListing = Exclude<SkillLabArtifactListing, { kind: "dir" }>;

/** A successful load, stamped with the job/path it answers and when it landed. */
interface Loaded<T> {
  jobId: string;
  path: string;
  data: T;
  at: number;
}

type ViewMode = "preview" | "source";

const sizeLabel = (bytes: number) =>
  bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);

const isMarkdown = (path: string) => /\.(md|markdown)$/i.test(path);

const clock = (at: number) => new Date(at).toLocaleTimeString();

const messageOf = (err: unknown) => (err instanceof ApiError ? err.message : String(err));

// Reads as the link it replaced; `.dim` (a class, so no inline color here)
// still colors it.
const linkButtonStyle = {
  background: "none",
  border: 0,
  padding: 0,
  cursor: "pointer",
  fontSize: 10.5,
  textDecoration: "underline",
} as const;

const nameButtonStyle = {
  background: "none",
  border: 0,
  color: "var(--amber)",
  cursor: "pointer",
  padding: 0,
  fontSize: 11,
  textAlign: "left",
  overflowWrap: "anywhere",
} as const;

/**
 * Browser over a job's `out/` tree — the CLI writes results.json, report.md and
 * one rollout work dir per task there, and the per-task artifacts are the actual
 * output being judged, so they have to be readable from the console.
 *
 * Shown for every job status. A live job writes under us, so nothing here
 * polls: the listing and the open file are refreshed on demand, and a refresh
 * that fails keeps the last successful load on screen *labelled with its time*
 * rather than pretending it is current. Every response is checked against the
 * job, path and request generation it was asked for, so a slow answer for a
 * previous job, directory or file can never land on the current selection.
 */
export function ArtifactBrowser({ jobId, live = false }: { jobId: string; live?: boolean }) {
  const { t } = useTranslation();

  // Directory navigation is tagged with the job it belongs to: a job switch
  // derives back to the root on the same render, with no reset effect racing
  // the load effect.
  const [nav, setNav] = useState({ jobId, dir: "" });
  const dir = nav.jobId === jobId ? nav.dir : "";
  const setDir = useCallback((next: string) => setNav({ jobId, dir: next }), [jobId]);

  const [listing, setListing] = useState<Loaded<DirListing> | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [file, setFile] = useState<Loaded<FileListing> | null>(null);
  const [fileLoading, setFileLoading] = useState(false);
  const [fileError, setFileError] = useState<string | null>(null);
  const [mode, setMode] = useState<ViewMode>("preview");

  // Request generations. A response is applied only if it is still the newest
  // request of its kind *and* the job it was issued for is still selected.
  const jobRef = useRef(jobId);
  jobRef.current = jobId;
  const listGen = useRef(0);
  const fileGen = useRef(0);

  const dialogRef = useRef<HTMLDivElement>(null);
  const opener = useRef<HTMLElement | null>(null);

  const shownListing = listing !== null && listing.jobId === jobId ? listing : null;
  const shownFile = file !== null && file.jobId === jobId ? file : null;
  // Only a listing for the directory being viewed is shown; a stale one for the
  // previous directory is not "the listing", even before the new load lands.
  const currentListing =
    shownListing !== null && shownListing.path === dir ? shownListing : null;

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
        // On a refresh the previous listing stays up, marked with its own time.
        setListError(messageOf(err));
      } finally {
        if (gen === listGen.current && jobRef.current === forJob) setListLoading(false);
      }
    },
    [t],
  );

  useEffect(() => {
    void loadDir(dir, false);
  }, [jobId, dir, loadDir]);

  // A job switch retires every file request in flight; the viewer itself is
  // already gone on this render (`shownFile` is keyed on the job).
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
    const back = opener.current;
    opener.current = null;
    if (back && document.contains(back)) back.focus();
  }, []);

  /**
   * Open a path from the listing or from a relative Markdown link. The server
   * says what it is: a file replaces the viewer's content, a directory
   * navigates the listing (and closes the viewer).
   */
  const openPath = useCallback(
    async (path: string, refresh = false) => {
      const gen = ++fileGen.current;
      const forJob = jobRef.current;
      if (!refresh && shownFile === null) opener.current = document.activeElement as HTMLElement;
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
        setFileError(messageOf(err));
      } finally {
        if (gen === fileGen.current && jobRef.current === forJob) setFileLoading(false);
      }
    },
    [shownFile],
  );

  // Focus lands in the dialog when it opens, so Escape / Tab work from the
  // keyboard without a mouse click first.
  useEffect(() => {
    if (shownFile !== null) dialogRef.current?.focus();
  }, [shownFile]);

  const download = async (path: string, name: string) => {
    setListError(null);
    try {
      const blob = await api.skillLabJobArtifactRaw(jobId, path);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setListError(messageOf(err));
    }
  };

  const onDialogKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      closeViewer();
    }
  };

  const segments = dir ? dir.split("/") : [];
  const viewerIsMarkdown = shownFile !== null && isMarkdown(shownFile.path);
  const viewerMode: ViewMode = viewerIsMarkdown ? mode : "source";
  // Only a file-error that has no content behind it is a failed *open*; with
  // content it is a failed *refresh* and the content keeps its timestamp.
  const openFailed = shownFile === null && fileError !== null;

  return (
    <div data-testid="skill-lab-artifacts">
      <div
        style={{
          display: "flex",
          gap: 8,
          alignItems: "center",
          flexWrap: "wrap",
          marginBottom: 8,
        }}
      >
        <div
          className="mono"
          style={{ fontSize: 10.5, display: "flex", gap: 5, flexWrap: "wrap", minWidth: 0 }}
        >
          <button
            type="button"
            className={`selchip${dir === "" ? " on" : ""}`}
            style={{ cursor: "pointer" }}
            data-testid="artifact-crumb-root"
            onClick={() => setDir("")}
          >
            out/
          </button>
          {segments.map((segment, index) => (
            <button
              key={`${segment}-${index}`}
              type="button"
              className={`selchip${index === segments.length - 1 ? " on" : ""}`}
              style={{ cursor: "pointer", overflowWrap: "anywhere" }}
              onClick={() => setDir(segments.slice(0, index + 1).join("/"))}
            >
              {segment}
            </button>
          ))}
        </div>
        <div
          className="mono dim"
          style={{
            fontSize: 10.5,
            display: "flex",
            gap: 8,
            alignItems: "center",
            marginLeft: "auto",
            flexWrap: "wrap",
          }}
        >
          {listLoading ? (
            <span data-testid="artifact-listing-loading">{t("common.loading")}</span>
          ) : currentListing !== null ? (
            <span data-testid="artifact-listing-refreshed-at">
              {t("skillLab.eval.artifacts.refreshedAt", { time: clock(currentListing.at) })}
            </span>
          ) : null}
          <Btn
            data-testid="artifact-refresh"
            disabled={listLoading}
            onClick={() => void loadDir(dir, true)}
          >
            {t("skillLab.eval.artifacts.refresh")}
          </Btn>
        </div>
      </div>

      {live && (
        <div className="mono dim" style={{ fontSize: 10.5, marginBottom: 8 }} data-testid="artifact-live-hint">
          {t("skillLab.eval.artifacts.liveHint")}
        </div>
      )}

      {listError !== null && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginBottom: 8 }}
          data-testid="artifact-listing-error"
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5, overflowWrap: "anywhere" }}>
            {listError}
            {currentListing !== null && (
              <>
                {" "}
                <span data-testid="artifact-listing-stale">
                  {t("skillLab.eval.artifacts.staleListing", { time: clock(currentListing.at) })}
                </span>
              </>
            )}
          </span>
        </div>
      )}

      {currentListing !== null && (
        <table data-testid="artifact-listing">
          <thead>
            <tr>
              <th>{t("skillLab.eval.artifacts.name")}</th>
              <th>{t("skillLab.eval.artifacts.size")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {currentListing.data.dirs.map((name) => (
              <tr key={`d-${name}`}>
                <td className="pri mono">
                  <button
                    type="button"
                    className="mono pri"
                    style={{ ...nameButtonStyle, color: "inherit" }}
                    data-testid={`artifact-dir-${name}`}
                    onClick={() => setDir(join(dir, name))}
                  >
                    ▸ {name}/
                  </button>
                </td>
                <td className="mono dim">—</td>
                <td />
              </tr>
            ))}
            {currentListing.data.files.map((entry) => (
              <tr key={`f-${entry.name}`} data-testid={`artifact-file-${entry.name}`}>
                <td>
                  <button
                    type="button"
                    className="mono"
                    style={nameButtonStyle}
                    data-testid={`artifact-open-${entry.name}`}
                    onClick={() => void openPath(join(dir, entry.name))}
                  >
                    {entry.name}
                  </button>
                </td>
                <td className="mono dim">{sizeLabel(entry.size)}</td>
                <td style={{ textAlign: "right" }}>
                  <button
                    type="button"
                    className="mono dim"
                    style={linkButtonStyle}
                    data-testid={`artifact-download-${entry.name}`}
                    onClick={() => void download(join(dir, entry.name), entry.name)}
                  >
                    {t("skillLab.eval.artifacts.download")}
                  </button>
                </td>
              </tr>
            ))}
            {currentListing.data.dirs.length === 0 && currentListing.data.files.length === 0 && (
              <tr>
                <td
                  colSpan={3}
                  className="dim mono"
                  style={{ textAlign: "center" }}
                  data-testid="artifact-listing-empty"
                >
                  {dir === "" && live
                    ? t("skillLab.eval.artifacts.emptyLive")
                    : t("skillLab.eval.artifacts.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {openFailed && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", marginTop: 8 }}
          data-testid="artifact-open-error"
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5, overflowWrap: "anywhere" }}>
            {fileError}
          </span>
        </div>
      )}

      {shownFile !== null && (
        <div className="confirm-backdrop" onClick={closeViewer}>
          <div
            ref={dialogRef}
            className="confirm-box"
            role="dialog"
            aria-modal="true"
            aria-label={shownFile.path}
            tabIndex={-1}
            data-testid="artifact-viewer"
            data-artifact-path={shownFile.path}
            style={{ maxWidth: "min(880px, 92vw)", width: "min(880px, 92vw)", outline: "none" }}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={onDialogKey}
          >
            <div className="confirm-title" style={{ wordBreak: "break-all" }}>
              {shownFile.path}
            </div>
            <div
              style={{
                display: "flex",
                gap: 8,
                alignItems: "center",
                flexWrap: "wrap",
                margin: "0 0 8px",
              }}
              className="mono dim"
            >
              <Chip tone={shownFile.data.kind === "text" ? "aqua" : "muted"}>
                {shownFile.data.kind}
              </Chip>
              <span style={{ fontSize: 10.5 }}>{sizeLabel(shownFile.data.size)}</span>
              {shownFile.data.kind === "text" && shownFile.data.truncated && (
                <span
                  style={{ fontSize: 10.5, color: "var(--warn)" }}
                  data-testid="artifact-truncated"
                >
                  {t("skillLab.eval.artifacts.truncated")}
                </span>
              )}
              {viewerIsMarkdown && (
                <div
                  role="group"
                  aria-label={t("skillLab.eval.artifacts.modeLabel")}
                  style={{ display: "flex", gap: 4 }}
                  data-testid="artifact-mode"
                >
                  {(["preview", "source"] as ViewMode[]).map((option) => (
                    <button
                      key={option}
                      type="button"
                      className={`selchip${viewerMode === option ? " on" : ""}`}
                      style={{ cursor: "pointer", padding: "3px 8px" }}
                      aria-pressed={viewerMode === option}
                      data-testid={`artifact-mode-${option}`}
                      onClick={() => setMode(option)}
                    >
                      {t(`skillLab.eval.artifacts.${option}`)}
                    </button>
                  ))}
                </div>
              )}
              <span style={{ marginLeft: "auto", display: "flex", gap: 10, alignItems: "center" }}>
                {fileLoading ? (
                  <span style={{ fontSize: 10.5 }} data-testid="artifact-file-loading">
                    {t("common.loading")}
                  </span>
                ) : (
                  <span style={{ fontSize: 10.5 }} data-testid="artifact-file-refreshed-at">
                    {t("skillLab.eval.artifacts.refreshedAt", { time: clock(shownFile.at) })}
                  </span>
                )}
                <button
                  type="button"
                  className="mono dim"
                  style={linkButtonStyle}
                  disabled={fileLoading}
                  data-testid="artifact-file-refresh"
                  onClick={() => void openPath(shownFile.path, true)}
                >
                  {t("skillLab.eval.artifacts.refresh")}
                </button>
                <button
                  type="button"
                  className="mono dim"
                  style={linkButtonStyle}
                  data-testid="artifact-viewer-download"
                  onClick={() =>
                    void download(shownFile.path, shownFile.path.split("/").pop() || "artifact")
                  }
                >
                  {t("skillLab.eval.artifacts.download")}
                </button>
              </span>
            </div>
            {fileError !== null && (
              <div
                className="note"
                style={{ borderColor: "var(--crit)", marginBottom: 8 }}
                data-testid="artifact-file-error"
              >
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span className="mono" style={{ fontSize: 10.5, overflowWrap: "anywhere" }}>
                  {fileError}{" "}
                  <span data-testid="artifact-file-stale">
                    {t("skillLab.eval.artifacts.staleFile", { time: clock(shownFile.at) })}
                  </span>
                </span>
              </div>
            )}
            <div style={{ maxHeight: "55vh", overflow: "auto" }} data-testid="artifact-body">
              {shownFile.data.kind !== "text" ? (
                <div className="empty">{t("skillLab.eval.artifacts.binary")}</div>
              ) : viewerMode === "preview" ? (
                <ArtifactMarkdown
                  text={shownFile.data.content}
                  baseDir={parentDir(shownFile.path)}
                  onOpenArtifact={(path) => void openPath(path)}
                />
              ) : (
                <pre
                  className="code"
                  data-testid="artifact-source"
                  style={{
                    whiteSpace: "pre-wrap",
                    overflowWrap: "anywhere",
                    fontSize: 10.5,
                  }}
                >
                  {shownFile.data.content}
                </pre>
              )}
            </div>
            <div className="confirm-actions">
              <Btn data-testid="artifact-viewer-close" onClick={closeViewer}>
                {t("common.close")}
              </Btn>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
