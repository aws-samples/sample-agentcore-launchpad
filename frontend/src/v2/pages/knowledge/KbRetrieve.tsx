import { Search } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage, v2KnowledgeApi } from "../../../lib/api";
import { KB_QUERY_MAX_RESULTS, type QueryResultItem } from "../../../lib/knowledgeBases";
import { Alert, Button, Card, Field, LinkButton, Tag } from "../../ui";

const CLAMP = 320;

function ResultCard({ index, item }: { index: number; item: QueryResultItem }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const long = item.text.length > CLAMP;
  const meta = Object.entries(item.metadata ?? {});
  return (
    <div className="v2-knowledge-hit" data-testid="v2-kb-hit">
      <div className="head">
        <span className="rank">#{index + 1}</span>
        <span className="uri mono">{item.location_uri || "—"}</span>
        {typeof item.score === "number" && (
          <Tag tone="blue">
            {t("knowledge.detail.playground.score")} {item.score.toFixed(3)}
          </Tag>
        )}
      </div>
      <p className="text">{long && !open ? `${item.text.slice(0, CLAMP)}…` : item.text}</p>
      {long && (
        <LinkButton onClick={() => setOpen((v) => !v)}>
          {open ? t("knowledge.detail.playground.showLess") : t("knowledge.detail.playground.showMore")}
        </LinkButton>
      )}
      {meta.length > 0 && (
        <dl className="v2-knowledge-stats meta">
          {meta.map(([k, v]) => (
            <div key={k}>
              <dt>{k}</dt>
              <dd>{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

/** Retrieve playground — what an attached agent would get back for a query. */
export function KbRetrieve({ kbId, active }: { kbId: string; active: boolean }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [count, setCount] = useState(8);
  const [busy, setBusy] = useState(false);
  const [results, setResults] = useState<QueryResultItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    const q = text.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    setResults(null);
    try {
      const res = await v2KnowledgeApi.query(kbId, q, count);
      setResults(res.results ?? []);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title={t("v2.knowledge.retrieveTest")} sub={t("knowledge.detail.playground.sub")} testId="v2-kb-retrieve">
      {!active && <Alert tone="warn">{t("v2.knowledge.retrieveNotActive")}</Alert>}
      <div className="v2-knowledge-query">
        <Field label={t("knowledge.detail.playground.query")}>
          <input
            className="v2-input"
            value={text}
            maxLength={4000}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void run()}
            placeholder={t("knowledge.detail.playground.queryPlaceholder")}
            data-testid="v2-kb-query"
          />
        </Field>
        <Field label={t("knowledge.detail.playground.numResults")}>
          <input
            type="number"
            min={1}
            max={KB_QUERY_MAX_RESULTS}
            className="v2-input mono"
            value={count}
            onChange={(e) => setCount(Math.max(1, Math.min(KB_QUERY_MAX_RESULTS, Number(e.target.value) || 1)))}
            data-testid="v2-kb-nresults"
          />
        </Field>
        <Button kind="primary" disabled={busy || !text.trim()} onClick={() => void run()} testId="v2-kb-search-btn">
          <Search size={14} aria-hidden="true" />
          {busy ? t("knowledge.detail.playground.searching") : t("knowledge.detail.playground.search")}
        </Button>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {results && results.length === 0 && <Alert>{t("knowledge.detail.playground.noResults")}</Alert>}
      {results && results.length > 0 && (
        <div className="v2-stack" data-testid="v2-kb-hits">
          <span className="v2-muted">{t("v2.knowledge.hits", { count: results.length })}</span>
          {results.map((r, i) => (
            <ResultCard key={i} index={i} item={r} />
          ))}
        </div>
      )}
    </Card>
  );
}
