import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip } from "../../components";
import type { SkillLabArtifactListing } from "../../lib/api";
import { api, ApiError } from "../../lib/api";

type DirListing = Extract<SkillLabArtifactListing, { kind: "dir" }>;
type FileListing = Exclude<SkillLabArtifactListing, { kind: "dir" }>;

const sizeLabel = (bytes: number) =>
  bytes < 1024 ? `${bytes} B` : `${(bytes / 1024).toFixed(1)} KB`;

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);

/**
 * Browser over a job's `out/` tree — the CLI writes results.json, report.md and
 * one rollout work dir per task there, and the per-task artifacts are the actual
 * output being judged, so they have to be readable from the console.
 */
export function ArtifactBrowser({ jobId }: { jobId: string }) {
  const { t } = useTranslation();
  const [dir, setDir] = useState("");
  const [listing, setListing] = useState<DirListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [file, setFile] = useState<FileListing | null>(null);

  useEffect(() => {
    setDir("");
    setFile(null);
  }, [jobId]);

  const load = useCallback(
    async (path: string) => {
      setError(null);
      try {
        const result = await api.skillLabJobArtifacts(jobId, path);
        if (result.kind === "dir") setListing(result);
        else setError(t("skillLab.eval.artifacts.notADir"));
      } catch (err) {
        setListing(null);
        setError(err instanceof ApiError ? err.message : String(err));
      }
    },
    [jobId, t],
  );

  useEffect(() => {
    void load(dir);
  }, [dir, load]);

  const openFile = async (path: string) => {
    setError(null);
    try {
      const result = await api.skillLabJobArtifacts(jobId, path);
      if (result.kind !== "dir") setFile(result);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const download = async (path: string, name: string) => {
    setError(null);
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
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  // Reads as the link it replaced; `.dim` (a class, so no inline color here)
  // still colors it.
  const downloadStyle = {
    background: "none",
    border: 0,
    padding: 0,
    cursor: "pointer",
    fontSize: 10.5,
    textDecoration: "underline",
  } as const;

  const segments = dir ? dir.split("/") : [];

  return (
    <div data-testid="skill-lab-artifacts">
      <div
        className="mono"
        style={{ fontSize: 10.5, display: "flex", gap: 5, flexWrap: "wrap", marginBottom: 8 }}
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
            style={{ cursor: "pointer" }}
            onClick={() => setDir(segments.slice(0, index + 1).join("/"))}
          >
            {segment}
          </button>
        ))}
      </div>

      {error !== null && (
        <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 8 }}>
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5 }}>
            {error}
          </span>
        </div>
      )}

      {listing !== null && (
        <table data-testid="artifact-listing">
          <thead>
            <tr>
              <th>{t("skillLab.eval.artifacts.name")}</th>
              <th>{t("skillLab.eval.artifacts.size")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {listing.dirs.map((name) => (
              <tr
                key={`d-${name}`}
                data-testid={`artifact-dir-${name}`}
                style={{ cursor: "pointer" }}
                onClick={() => setDir(join(dir, name))}
              >
                <td className="pri mono">▸ {name}/</td>
                <td className="mono dim">—</td>
                <td />
              </tr>
            ))}
            {listing.files.map((entry) => (
              <tr key={`f-${entry.name}`} data-testid={`artifact-file-${entry.name}`}>
                <td>
                  <button
                    type="button"
                    className="mono"
                    style={{
                      background: "none",
                      border: 0,
                      color: "var(--amber)",
                      cursor: "pointer",
                      padding: 0,
                      fontSize: 11,
                    }}
                    onClick={() => void openFile(join(dir, entry.name))}
                  >
                    {entry.name}
                  </button>
                </td>
                <td className="mono dim">{sizeLabel(entry.size)}</td>
                <td style={{ textAlign: "right" }}>
                  <button
                    type="button"
                    className="mono dim"
                    style={downloadStyle}
                    data-testid={`artifact-download-${entry.name}`}
                    onClick={() => void download(join(dir, entry.name), entry.name)}
                  >
                    {t("skillLab.eval.artifacts.download")}
                  </button>
                </td>
              </tr>
            ))}
            {listing.dirs.length === 0 && listing.files.length === 0 && (
              <tr>
                <td colSpan={3} className="dim mono" style={{ textAlign: "center" }}>
                  {t("skillLab.eval.artifacts.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}

      {file !== null && (
        <div className="confirm-backdrop" onClick={() => setFile(null)}>
          <div
            className="confirm-box"
            role="dialog"
            aria-modal="true"
            aria-label={file.path}
            data-testid="artifact-viewer"
            style={{ maxWidth: "min(880px, 90vw)", width: "min(880px, 90vw)" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="confirm-title" style={{ wordBreak: "break-all" }}>
              {file.path}
            </div>
            <div
              style={{ display: "flex", gap: 8, alignItems: "center", margin: "0 0 8px" }}
              className="mono dim"
            >
              <Chip tone={file.kind === "text" ? "aqua" : "muted"}>{file.kind}</Chip>
              <span style={{ fontSize: 10.5 }}>{sizeLabel(file.size)}</span>
              {file.kind === "text" && file.truncated && (
                <span style={{ fontSize: 10.5, color: "var(--warn)" }}>
                  {t("skillLab.eval.artifacts.truncated")}
                </span>
              )}
              <button
                type="button"
                className="mono dim"
                style={{ ...downloadStyle, marginLeft: "auto" }}
                onClick={() =>
                  void download(file.path, file.path.split("/").pop() || "artifact")
                }
              >
                {t("skillLab.eval.artifacts.download")}
              </button>
            </div>
            {file.kind === "text" ? (
              <pre
                className="code"
                style={{
                  maxHeight: "55vh",
                  overflow: "auto",
                  whiteSpace: "pre-wrap",
                  overflowWrap: "anywhere",
                  fontSize: 10.5,
                }}
              >
                {file.content}
              </pre>
            ) : (
              <div className="empty">{t("skillLab.eval.artifacts.binary")}</div>
            )}
            <div className="confirm-actions">
              <Btn onClick={() => setFile(null)}>{t("common.close")}</Btn>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
