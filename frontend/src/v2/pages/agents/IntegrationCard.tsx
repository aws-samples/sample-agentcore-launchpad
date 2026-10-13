import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  buildSnippet,
  KEY_PLACEHOLDER,
  SNIPPET_LANGS,
  SNIPPET_MODES,
  type SnippetLang,
  type SnippetMode,
} from "../../../lib/snippets";
import { useV2Toast } from "../../hooks";
import { Button, Card, Segmented } from "../../ui";
import { copyText } from "../observability/common";

/** Copy-ready curl / Python / JavaScript for this agent's public `/v1` API.
 *  Pure client-side text: no route, and no key is ever shown here. */
export function IntegrationCard({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [lang, setLang] = useState<SnippetLang>("curl");
  const [mode, setMode] = useState<SnippetMode>("sync");
  const snippet = buildSnippet({ baseUrl: window.location.origin, agentId }, lang, mode);

  const copy = () => {
    copyText(snippet).then(
      () => toast("success", t("v2.agents.integration.copied")),
      () => toast("error", t("v2.agents.integration.copyFailed")),
    );
  };

  return (
    <Card
      title={t("v2.agents.integration.title")}
      sub={t("v2.agents.integration.sub", { key: KEY_PLACEHOLDER })}
      testId="v2-agent-integration"
    >
      <div className="v2-row" style={{ marginBottom: 12, gap: 12, flexWrap: "wrap" }}>
        <Segmented
          value={lang}
          ariaLabel={t("v2.agents.integration.language")}
          options={SNIPPET_LANGS.map((l) => ({ value: l, label: t(`v2.agents.integration.lang.${l}`) }))}
          onChange={setLang}
        />
        <Segmented
          value={mode}
          ariaLabel={t("v2.agents.integration.mode")}
          options={SNIPPET_MODES.map((m) => ({ value: m, label: t(`v2.agents.integration.modes.${m}`) }))}
          onChange={setMode}
        />
        <Button size="sm" onClick={copy} testId="v2-agent-integration-copy">
          {t("v2.agents.integration.copy")}
        </Button>
      </div>
      <pre className="v2-pre" data-testid="v2-agent-integration-snippet">
        {snippet}
      </pre>
      <p className="v2-muted">
        {t("v2.agents.integration.keyHint")}{" "}
        <Link
          to={`/v2/chat?agent=${encodeURIComponent(agentId)}&tab=api`}
          data-testid="v2-agent-integration-keys"
        >
          {t("v2.agents.integration.keysLink")}
        </Link>
      </p>
    </Card>
  );
}
