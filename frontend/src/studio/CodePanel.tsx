import { copyBrowserText } from "../lib/browser-compat";
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import Editor from '@monaco-editor/react';
import { Code, Download, Copy, AlertCircle } from 'lucide-react';
import { Btn, Chip } from '../components';

interface CodePanelProps {
  // The active code from the lifted CodeState (template-generated or AI-fixed).
  code: string;
  errors: string[];
  source?: 'template' | 'ai';
  flowStale?: boolean;
  className?: string;
}

export function CodePanel({
  code,
  errors,
  source = 'template',
  flowStale = false,
  className = '',
}: CodePanelProps) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);

  const handleDownload = () => {
    const blob = new Blob([code], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'strands_agent.py';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleCopy = () => {
    void copyBrowserText(code).then(
      (didCopy) => {
        if (!didCopy) return;
        setCopied(true);
        setTimeout(() => setCopied(false), 1500);
      },
      () => {
        /* clipboard unavailable — ignore */
      },
    );
  };

  return (
    <div className={`studio-codepanel ${className}`}>
      <div className="studio-code-head">
        <Code size={14} style={{ color: 'var(--amber)' }} />
        <h3>{t('studio.code.title')}</h3>
        {source === 'ai' && (
          <Chip tone="blue" icon="✦">
            {t('studio.code.aiFixed')}
          </Chip>
        )}
        {flowStale && (
          <Chip tone="warn" icon="⚠">
            {t('studio.code.flowStale')}
          </Chip>
        )}
        <div className="studio-code-actions">
          <Btn onClick={handleCopy}>
            <Copy size={12} /> {copied ? t('studio.code.copied') : t('studio.code.copy')}
          </Btn>
          <Btn onClick={handleDownload}>
            <Download size={12} /> {t('studio.code.download')}
          </Btn>
        </div>
      </div>

      {errors.length > 0 && (
        <div className="studio-code-errs">
          <div className="studio-code-errs-h">
            <AlertCircle size={12} /> {t('studio.code.errorsHeader')}
          </div>
          <ul>
            {errors.map((error, index) => (
              <li key={index}>{error}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="studio-code-monaco">
        <Editor
          height="100%"
          language="python"
          theme="vs-dark"
          value={code}
          options={{
            minimap: { enabled: false },
            fontSize: 12,
            lineNumbers: 'on',
            roundedSelection: false,
            scrollBeyondLastLine: false,
            automaticLayout: true,
            wordWrap: 'on',
            readOnly: true,
          }}
        />
      </div>

      <div className="studio-code-foot">
        <span>Python • Strands Agent SDK</span>
        <span>{t('studio.code.lines', { count: code.split('\n').length })}</span>
      </div>
    </div>
  );
}
