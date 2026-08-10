import { useCallback, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { type Node, type Edge } from '@xyflow/react';
import { Settings, X, RefreshCw, Library } from 'lucide-react';
import {
  BEDROCK_MODELS,
  CUSTOM_MODEL_OPTION,
  CUSTOM_MODEL_NAME,
  isCustomModel,
  MANTLE_PROVIDER,
  MANTLE_MODELS,
  DEFAULT_MANTLE_REGION,
  DEFAULT_MANTLE_MODEL_ID,
  mantleBaseUrl,
  isCustomMantleModel,
} from './lib/models';

// Union of every field the node data objects can carry across all node types.
// Keeping them optional lets each render function read only what it needs while
// staying byte-faithful to the upstream data-key contract the generators read.
interface StudioNodeData {
  label?: string;
  // agent / orchestrator
  modelProvider?: string;
  modelId?: string;
  modelName?: string;
  systemPrompt?: string;
  maxTokens?: number;
  streaming?: boolean;
  apiKey?: string;
  baseUrl?: string;
  thinkingEnabled?: boolean;
  // thinkingBudgetTokens is no longer written (adaptive thinking); kept optional
  // so existing saved graphs that carry the key still type-check.
  thinkingBudgetTokens?: number;
  reasoningEffort?: string;
  // Bedrock prompt caching
  cacheMessages?: boolean;
  cacheTools?: boolean;
  cacheSystem?: boolean; // launchpad extension: system-prompt caching
  // Mantle (Amazon Bedrock via OpenAI Responses API)
  region?: string;
  coordinationPrompt?: string;
  // tool
  toolType?: string;
  toolName?: string;
  description?: string;
  // skill
  skillName?: string;
  // mcp-tool
  serverName?: string;
  transportType?: string;
  command?: string;
  args?: string[];
  argsText?: string;
  url?: string;
  headers?: Record<string, string>;
  headersText?: string;
  env?: Record<string, string>;
  envText?: string;
  timeout?: number;
  // custom-tool
  pythonCode?: string;
  // swarm
  maxHandoffs?: number;
  maxIterations?: number;
  executionTimeout?: number;
  nodeTimeout?: number;
  repetitiveHandoffDetectionWindow?: number;
  repetitiveHandoffMinUniqueAgents?: number;
  // graph-builder
  enableDebugLogs?: boolean;
}

interface PropertyPanelProps {
  selectedNode: Node | null;
  onClose: () => void;
  onUpdateNode: (nodeId: string, data: Record<string, unknown>) => void;
  edges?: Edge[];
  nodes?: Node[];
  className?: string;
}

// APPROVED AGENT_SKILLS registry records offered as skill sources. Shape matches
// GET /api/registry/attachables .skills[] (see pages/CreateAgent.tsx).
interface AttachableSkill {
  name: string;
  description: string;
  path?: string;
  record_id?: string;
}

// The skill node's picker lists launchpad's own registry skills (APPROVED only),
// replacing upstream's studio skill-library backend. It has its own fetch state,
// so it lives in a dedicated component (hooks can't run inside the switch).
function SkillNodeProperties({
  node,
  onUpdateNode,
}: {
  node: Node;
  onUpdateNode: (nodeId: string, data: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation();
  const data = node.data as StudioNodeData;
  const [skills, setSkills] = useState<AttachableSkill[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // bust=true skips the backend's 60s attachables cache (explicit refresh button).
  const loadSkills = useCallback((bust = false) => {
    setLoading(true);
    setLoadError(null);
    fetch(bust ? '/api/registry/attachables?refresh=1' : '/api/registry/attachables')
      .then((res) => (res.ok ? res.json() : { mcp_servers: [], skills: [] }))
      .then((d: { skills?: AttachableSkill[] }) => setSkills(d.skills || []))
      .catch(() => {
        setSkills([]);
        setLoadError(t('studio.prop.skillLoadError'));
      })
      .finally(() => setLoading(false));
  }, [t]);

  useEffect(() => {
    loadSkills();
  }, [loadSkills]);

  const handleSelect = (name: string) => {
    const skill = skills.find((s) => s.name === name);
    onUpdateNode(node.id, {
      ...node.data,
      skillName: name,
      description: skill?.description || '',
    });
  };

  const selectedMissing =
    !!data.skillName && !loading && !skills.some((s) => s.name === data.skillName);

  return (
    <div>
      <div className="studio-note">{t('studio.prop.skillTrustWarning')}</div>

      <div className="field">
        <label>{t('studio.prop.label')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => onUpdateNode(node.id, { ...node.data, label: e.target.value })}
          placeholder={t('studio.prop.skillLabelPlaceholder')}
        />
      </div>

      <div className="field">
        <label>{t('studio.prop.skill')}</label>
        <div className="studio-prop-inline">
          <select
            className="input"
            style={{ flex: 1 }}
            value={data.skillName || ''}
            onChange={(e) => handleSelect(e.target.value)}
            disabled={loading}
          >
            <option value="">{t('studio.prop.skillSelectPlaceholder')}</option>
            {skills.map((skill) => (
              <option key={skill.name} value={skill.name}>
                {skill.name}
                {skill.description ? ` — ${skill.description}` : ''}
              </option>
            ))}
            {selectedMissing && (
              <option value={data.skillName}>
                {t('studio.prop.skillMissingOption', { name: data.skillName })}
              </option>
            )}
          </select>
          <button
            type="button"
            className="btn"
            onClick={() => loadSkills(true)}
            disabled={loading}
            title={t('studio.prop.skillRefresh')}
          >
            <RefreshCw size={14} className={loading ? 'studio-spin' : undefined} />
          </button>
        </div>
        {loadError && <div className="studio-warn">{loadError}</div>}
        {selectedMissing && !loadError && (
          <div className="studio-warn">
            {t('studio.prop.skillMissingWarn', { name: data.skillName })}
          </div>
        )}
        {data.description && <div className="studio-prop-hint">{data.description}</div>}
      </div>

      <div className="field">
        <Link to="/registry" className="btn">
          <Library size={14} />
          {t('studio.prop.manageInRegistry')}
        </Link>
        <div className="studio-prop-hint">{t('studio.prop.skillEmptyHint')}</div>
      </div>
    </div>
  );
}

export function PropertyPanel({
  selectedNode,
  onClose,
  onUpdateNode,
  edges = [],
  nodes = [],
  className = '',
}: PropertyPanelProps) {
  const { t } = useTranslation();

  if (!selectedNode) {
    return null;
  }

  const node = selectedNode;

  // Check if the selected node has an output node connected
  const hasConnectedOutputNode = () => {
    if (node.type !== 'agent' && node.type !== 'orchestrator-agent') {
      return true; // For non-agent nodes, always allow streaming
    }

    // Find all edges where this node is the source from its output handle
    const outgoingEdges = edges.filter(
      (edge) => edge.source === node.id && edge.sourceHandle === 'output',
    );

    // For each outgoing edge, check if the target node is an output node
    return outgoingEdges.some((edge) => {
      const targetNode = nodes.find((n) => n.id === edge.target);
      return targetNode && targetNode.type === 'output';
    });
  };

  const handleInputChange = (field: string, value: unknown) => {
    try {
      onUpdateNode(node.id, {
        ...node.data,
        [field]: value,
      });
    } catch (error) {
      console.error('Failed to update node property:', error);
    }
  };

  const bedrockModels = BEDROCK_MODELS;

  // Seed provider-specific defaults when the provider dropdown changes so the
  // model field/codegen always has a coherent id (mirrors upstream :494-521).
  const applyProviderChange = (provider: string) => {
    if (provider === 'AWS Bedrock') {
      onUpdateNode(node.id, {
        ...node.data,
        modelProvider: provider,
        modelId: bedrockModels[0].model_id,
        modelName: bedrockModels[0].model_name,
      });
    } else if (provider === MANTLE_PROVIDER) {
      const region = (node.data as StudioNodeData).region || DEFAULT_MANTLE_REGION;
      onUpdateNode(node.id, {
        ...node.data,
        modelProvider: provider,
        region,
        baseUrl: mantleBaseUrl(region, DEFAULT_MANTLE_MODEL_ID),
        // Mantle model ids flow through the non-Bedrock (modelName) codegen path.
        modelId: DEFAULT_MANTLE_MODEL_ID,
        modelName: DEFAULT_MANTLE_MODEL_ID,
      });
    } else {
      // OpenAI / other free-text providers
      onUpdateNode(node.id, {
        ...node.data,
        modelProvider: provider,
        modelId: '',
        modelName: '',
      });
    }
  };

  // Mantle: region + model (both drive baseUrl) + an OPTIONAL BEDROCK_API_KEY;
  // without a key, codegen emits the IAM `bedrock_mantle_config` form instead.
  const renderMantleFields = (data: StudioNodeData) => {
    const region = data.region || DEFAULT_MANTLE_REGION;
    const custom = isCustomMantleModel(data.modelId, data.modelName);
    // The base URL path depends on the model id, so it is recomputed whenever
    // either input changes — a stale `/openai/v1` on a grok node would 404.
    const modelForUrl = data.modelId || DEFAULT_MANTLE_MODEL_ID;
    return (
      <>
        <div className="field">
          <label>{t('studio.prop.mantleRegion')}</label>
          <input
            className="input"
            type="text"
            value={region}
            onChange={(e) => {
              const r = e.target.value;
              onUpdateNode(node.id, {
                ...node.data,
                region: r,
                baseUrl: mantleBaseUrl(r, modelForUrl),
              });
            }}
            placeholder={DEFAULT_MANTLE_REGION}
          />
          <div className="studio-prop-hint mono">{mantleBaseUrl(region, modelForUrl)}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.model')}</label>
          <select
            className="input"
            value={custom ? CUSTOM_MODEL_OPTION : data.modelId || DEFAULT_MANTLE_MODEL_ID}
            onChange={(e) => {
              if (e.target.value === CUSTOM_MODEL_OPTION) {
                onUpdateNode(node.id, {
                  ...node.data,
                  modelId: '',
                  modelName: CUSTOM_MODEL_NAME,
                });
                return;
              }
              onUpdateNode(node.id, {
                ...node.data,
                modelId: e.target.value,
                modelName: e.target.value,
                baseUrl: mantleBaseUrl(region, e.target.value),
              });
            }}
          >
            {MANTLE_MODELS.map((m) => (
              <option key={m.model_id} value={m.model_id}>
                {m.model_name}
              </option>
            ))}
            <option value={CUSTOM_MODEL_OPTION}>{t('studio.prop.customModelOption')}</option>
          </select>
          {custom && (
            <input
              className="input"
              style={{ marginTop: 8 }}
              type="text"
              value={data.modelId || ''}
              onChange={(e) =>
                onUpdateNode(node.id, {
                  ...node.data,
                  modelId: e.target.value,
                  modelName: e.target.value ? e.target.value : CUSTOM_MODEL_NAME,
                  baseUrl: mantleBaseUrl(region, e.target.value),
                })
              }
              placeholder={t('studio.prop.mantleModelPlaceholder')}
            />
          )}
        </div>

        <div className="field">
          <label>{t('studio.prop.bedrockApiKey')}</label>
          <input
            className="input"
            type="password"
            value={data.apiKey || ''}
            onChange={(e) => handleInputChange('apiKey', e.target.value)}
            placeholder={t('studio.prop.bedrockApiKeyPlaceholder')}
          />
          <div className="studio-prop-hint">{t('studio.prop.bedrockApiKeyHint')}</div>
        </div>
      </>
    );
  };

  // Shared model block. `allowAnthropic` mirrors upstream: the orchestrator's
  // provider dropdown offered Anthropic (agent's did not). There is no Anthropic
  // codegen branch — it falls through to Bedrock — but the option value is kept
  // to stay faithful to the upstream data contract.
  const renderModelFields = (data: StudioNodeData, allowAnthropic = false) => (
    <>
      <div className="field">
        <label>{t('studio.prop.modelProvider')}</label>
        <select
          className="input"
          value={data.modelProvider || 'AWS Bedrock'}
          onChange={(e) => applyProviderChange(e.target.value)}
        >
          <option value="AWS Bedrock">AWS Bedrock</option>
          <option value={MANTLE_PROVIDER}>{MANTLE_PROVIDER}</option>
          <option value="OpenAI">OpenAI</option>
          {allowAnthropic && <option value="Anthropic">Anthropic</option>}
        </select>
      </div>

      {data.modelProvider === MANTLE_PROVIDER ? (
        renderMantleFields(data)
      ) : (
        <div className="field">
          <label>{t('studio.prop.model')}</label>
          {data.modelProvider === 'AWS Bedrock' || !data.modelProvider ? (
            <>
              <select
                className="input"
                value={
                  isCustomModel(data.modelId, data.modelName)
                    ? CUSTOM_MODEL_OPTION
                    : data.modelId || bedrockModels[0].model_id
                }
                onChange={(e) => {
                  if (e.target.value === CUSTOM_MODEL_OPTION) {
                    onUpdateNode(node.id, {
                      ...node.data,
                      modelId: '',
                      modelName: CUSTOM_MODEL_NAME,
                    });
                    return;
                  }
                  const selectedModel = bedrockModels.find((m) => m.model_id === e.target.value);
                  if (selectedModel) {
                    onUpdateNode(node.id, {
                      ...node.data,
                      modelId: selectedModel.model_id,
                      modelName: selectedModel.model_name,
                    });
                  }
                }}
              >
                {bedrockModels.map((model) => (
                  <option key={model.model_id} value={model.model_id}>
                    {model.model_name}
                  </option>
                ))}
                <option value={CUSTOM_MODEL_OPTION}>{t('studio.prop.customModelOption')}</option>
              </select>
              {isCustomModel(data.modelId, data.modelName) && (
                <input
                  className="input"
                  style={{ marginTop: 8 }}
                  type="text"
                  value={data.modelId || ''}
                  onChange={(e) =>
                    onUpdateNode(node.id, {
                      ...node.data,
                      modelId: e.target.value,
                      modelName: CUSTOM_MODEL_NAME,
                    })
                  }
                  placeholder={t('studio.prop.customModelPlaceholder')}
                />
              )}
            </>
          ) : (
            <input
              className="input"
              type="text"
              value={data.modelName || ''}
              onChange={(e) => handleInputChange('modelName', e.target.value)}
              placeholder={t('studio.prop.modelNamePlaceholder')}
            />
          )}
        </div>
      )}

      {data.modelProvider === 'OpenAI' && (
        <>
          <div className="field">
            <label>{t('studio.prop.apiKey')}</label>
            <input
              className="input"
              type="password"
              value={data.apiKey || ''}
              onChange={(e) => handleInputChange('apiKey', e.target.value)}
              placeholder={t('studio.prop.apiKeyPlaceholder')}
            />
            <div className="studio-prop-hint">{t('studio.prop.apiKeyHint')}</div>
          </div>

          <div className="field">
            <label>{t('studio.prop.baseUrl')}</label>
            <input
              className="input"
              type="url"
              value={data.baseUrl || ''}
              onChange={(e) => handleInputChange('baseUrl', e.target.value)}
              placeholder={t('studio.prop.baseUrlPlaceholder')}
            />
            <div className="studio-prop-hint">{t('studio.prop.baseUrlHint')}</div>
          </div>
        </>
      )}
    </>
  );

  const renderThinkingSection = (data: StudioNodeData) => {
    const isBedrock = data.modelProvider === 'AWS Bedrock' || !data.modelProvider;
    // legacy 'minimal' coerced to 'low' (upstream :845)
    const effortValue = data.reasoningEffort === 'minimal' ? 'low' : data.reasoningEffort || 'medium';
    // launchpad extension: xhigh effort is only accepted by Sonnet 5 / Opus 4.8 on Bedrock
    const modelId = data.modelId || '';
    const xhighCapable = modelId.includes('claude-sonnet-5') || modelId.includes('claude-opus-4-8');
    return (
      <div className="studio-prop-sect">
        <div className="kicker" style={{ marginBottom: 10 }}>
          {t('studio.prop.advancedSettings')}
        </div>
        <div className="field">
          <label className="studio-check">
            <input
              type="checkbox"
              checked={data.thinkingEnabled || false}
              onChange={(e) => handleInputChange('thinkingEnabled', e.target.checked)}
            />
            <span>{t('studio.prop.enableThinking')}</span>
          </label>
          <div className="studio-prop-hint">{t('studio.prop.thinkingHint')}</div>
        </div>

        {data.thinkingEnabled &&
          (isBedrock ? (
            // Bedrock/Claude uses adaptive thinking — no budget knob.
            // launchpad extension: expose the reasoning-effort tier for Bedrock (output_config.effort).
            <>
              <div className="studio-note">{t('studio.prop.adaptiveThinkingNote')}</div>
              <div className="field" style={{ marginTop: 10 }}>
                <label>{t('studio.prop.reasoningEffort')}</label>
                <select
                  className="input"
                  value={effortValue}
                  onChange={(e) => handleInputChange('reasoningEffort', e.target.value)}
                >
                  <option value="low">{t('studio.prop.effortLow')}</option>
                  <option value="medium">{t('studio.prop.effortMedium')}</option>
                  <option value="high">{t('studio.prop.effortHigh')}</option>
                  <option
                    value="xhigh"
                    disabled={!xhighCapable}
                    title={!xhighCapable ? t('studio.prop.effortXHighUnavailable') : undefined}
                  >
                    {t('studio.prop.effortXHigh')}
                  </option>
                </select>
                {effortValue === 'xhigh' && !xhighCapable && (
                  <div className="studio-prop-hint">{t('studio.prop.effortXHighClamped')}</div>
                )}
              </div>
            </>
          ) : (
            <div className="field">
              <label>{t('studio.prop.reasoningEffort')}</label>
              <select
                className="input"
                value={effortValue}
                onChange={(e) => handleInputChange('reasoningEffort', e.target.value)}
              >
                <option value="low">{t('studio.prop.effortLow')}</option>
                <option value="medium">{t('studio.prop.effortMedium')}</option>
                <option value="high">{t('studio.prop.effortHigh')}</option>
                <option value="xhigh">{t('studio.prop.effortXHigh')}</option>
                <option value="max">{t('studio.prop.effortMax')}</option>
              </select>
            </div>
          ))}

        {isBedrock && (
          <div className="field" style={{ marginTop: 14 }}>
            <label>{t('studio.prop.promptCaching')}</label>
            <label className="studio-check" style={{ marginTop: 6 }}>
              <input
                type="checkbox"
                checked={data.cacheMessages || false}
                onChange={(e) => handleInputChange('cacheMessages', e.target.checked)}
              />
              <span>{t('studio.prop.cacheConversation')}</span>
            </label>
            <label className="studio-check" style={{ marginTop: 6 }}>
              <input
                type="checkbox"
                checked={data.cacheTools || false}
                onChange={(e) => handleInputChange('cacheTools', e.target.checked)}
              />
              <span>{t('studio.prop.cacheTools')}</span>
            </label>
            {/* launchpad extension: system-prompt caching (third cache toggle) */}
            <label className="studio-check" style={{ marginTop: 6 }}>
              <input
                type="checkbox"
                checked={data.cacheSystem || false}
                onChange={(e) => handleInputChange('cacheSystem', e.target.checked)}
              />
              <span>{t('studio.prop.cacheSystem')}</span>
            </label>
            <div className="studio-prop-hint">{t('studio.prop.cacheSystemHint')}</div>
            <div className="studio-prop-hint">{t('studio.prop.cachingHint')}</div>
          </div>
        )}
      </div>
    );
  };

  // launchpad extension: surface per-model max_tokens caps (Nova Pro 10000, Nova Premier 32000)
  const renderMaxTokensCapHint = (data: StudioNodeData) => {
    const isBedrock = data.modelProvider === 'AWS Bedrock' || !data.modelProvider;
    if (!isBedrock) return null;
    const modelId = data.modelId || '';
    if (modelId.includes('nova-premier')) {
      return <div className="studio-prop-hint">{t('studio.prop.maxTokensCapNovaPremier')}</div>;
    }
    if (modelId.includes('nova-pro')) {
      return <div className="studio-prop-hint">{t('studio.prop.maxTokensCapNovaPro')}</div>;
    }
    return null;
  };

  const renderStreamingField = (data: StudioNodeData) => (
    <div className="field">
      <label className="studio-check">
        <input
          type="checkbox"
          // launchpad extension: streaming defaults ON for the main execution agent
          checked={data.streaming ?? true}
          disabled={!hasConnectedOutputNode()}
          onChange={(e) => handleInputChange('streaming', e.target.checked)}
        />
        <span>{t('studio.prop.enableStreaming')}</span>
      </label>
      <div className="studio-prop-hint">
        {hasConnectedOutputNode()
          ? t('studio.prop.streamingHintOn')
          : t('studio.prop.streamingHintOff')}
      </div>
    </div>
  );

  const renderAgentProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.agentName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder={t('studio.prop.agentNamePlaceholder')}
        />
      </div>

      {renderModelFields(data)}

      <div className="field">
        <label>{t('studio.prop.systemPrompt')}</label>
        <textarea
          className="input mono"
          style={{ minHeight: 88, resize: 'vertical' }}
          value={data.systemPrompt || ''}
          onChange={(e) => handleInputChange('systemPrompt', e.target.value)}
          placeholder={t('studio.prop.systemPromptPlaceholder')}
          rows={4}
        />
      </div>


      <div className="field">
        <label>{t('studio.prop.maxTokens')}</label>
        <input
          className="input"
          type="number"
          // launchpad extension: default 32000
          value={data.maxTokens || 32000}
          onChange={(e) => handleInputChange('maxTokens', parseInt(e.target.value))}
          min="1"
          max="100000"
        />
        {renderMaxTokensCapHint(data)}
      </div>

      {renderStreamingField(data)}
      {renderThinkingSection(data)}
    </div>
  );

  const renderOrchestratorAgentProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.orchestratorName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder={t('studio.prop.orchestratorNamePlaceholder')}
        />
      </div>

      {renderModelFields(data, true)}

      <div className="field">
        <label>{t('studio.prop.systemPrompt')}</label>
        <textarea
          className="input mono"
          style={{ minHeight: 88, resize: 'vertical' }}
          value={data.systemPrompt || ''}
          onChange={(e) => handleInputChange('systemPrompt', e.target.value)}
          placeholder={t('studio.prop.orchestratorPromptPlaceholder')}
          rows={4}
        />
      </div>

      <div className="studio-prop-sect">
        <div className="kicker" style={{ marginBottom: 10 }}>
          {t('studio.prop.orchestrationSettings')}
        </div>
        <div className="field">
          <label>{t('studio.prop.coordinationPrompt')}</label>
          <textarea
            className="input mono"
            style={{ minHeight: 66, resize: 'vertical' }}
            value={data.coordinationPrompt || ''}
            onChange={(e) => handleInputChange('coordinationPrompt', e.target.value)}
            placeholder={t('studio.prop.coordinationPlaceholder')}
            rows={3}
          />
        </div>
      </div>


      <div className="field">
        <label>{t('studio.prop.maxTokens')}</label>
        <input
          className="input"
          type="number"
          // launchpad extension: default 32000
          value={data.maxTokens || 32000}
          onChange={(e) => handleInputChange('maxTokens', parseInt(e.target.value))}
          min="100"
          max="100000"
        />
        {renderMaxTokensCapHint(data)}
      </div>

      {renderStreamingField(data)}
      {renderThinkingSection(data)}
    </div>
  );

  const renderToolProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.toolName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder={t('studio.prop.toolNamePlaceholder')}
        />
      </div>

      <div className="field">
        <label>{t('studio.prop.toolType')}</label>
        <select
          className="input"
          value={data.toolType || 'built-in'}
          onChange={(e) => handleInputChange('toolType', e.target.value)}
        >
          <option value="built-in">{t('studio.prop.builtIn')}</option>
        </select>
      </div>

      <div className="field">
        <label>{t('studio.prop.toolNameFunction')}</label>
        {data.toolType === 'built-in' || !data.toolType ? (
          <select
            className="input"
            value={data.toolName || 'calculator'}
            onChange={(e) => handleInputChange('toolName', e.target.value)}
          >
            <option value="calculator">Calculator</option>
            <option value="file_read">File Reader</option>
            <option value="file_write">File Write</option>
            <option value="shell">Shell Command</option>
            <option value="current_time">Current Time</option>
            <option value="http_request">Http Request</option>
            <option value="editor">Editor</option>
            <option value="retrieve">Retrieve (KB)</option>
          </select>
        ) : (
          <input
            className="input"
            type="text"
            value={data.toolName || ''}
            onChange={(e) => handleInputChange('toolName', e.target.value)}
            placeholder="custom_function_name"
          />
        )}
      </div>

      <div className="field">
        <label>{t('studio.prop.description')}</label>
        <textarea
          className="input"
          style={{ resize: 'vertical' }}
          value={data.description || ''}
          onChange={(e) => handleInputChange('description', e.target.value)}
          placeholder={t('studio.prop.toolDescPlaceholder')}
          rows={3}
        />
      </div>
    </div>
  );

  const renderInputProperties = () => (
    <div className="studio-prop-empty">
      {t('studio.prop.inputInfo')}
      <div style={{ marginTop: 8 }}>{t('studio.prop.inputNoConfig')}</div>
    </div>
  );

  const renderMCPToolProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.serverName')}</label>
        <input
          className="input"
          type="text"
          value={data.serverName || ''}
          onChange={(e) => handleInputChange('serverName', e.target.value)}
          placeholder={t('studio.prop.serverNamePlaceholder')}
        />
      </div>

      <div className="field">
        <label>{t('studio.prop.transportType')}</label>
        <select
          className="input"
          value={data.transportType || 'stdio'}
          onChange={(e) => handleInputChange('transportType', e.target.value)}
        >
          <option value="stdio">Standard I/O (stdio)</option>
          <option value="streamable_http">Streamable HTTP</option>
          <option value="sse">Server-Sent Events (SSE)</option>
        </select>
      </div>

      {data.transportType === 'stdio' && (
        <>
          <div className="field">
            <label>{t('studio.prop.command')}</label>
            <input
              className="input"
              type="text"
              value={data.command || ''}
              onChange={(e) => handleInputChange('command', e.target.value)}
              placeholder="uvx"
            />
          </div>

          <div className="field">
            <label>{t('studio.prop.arguments')}</label>
            <textarea
              className="input mono"
              style={{ resize: 'vertical' }}
              value={data.argsText !== undefined ? data.argsText : data.args ? data.args.join('\n') : ''}
              onChange={(e) => {
                const argsText = e.target.value;
                const args = argsText.split('\n').filter((arg) => arg.trim());
                onUpdateNode(node.id, {
                  ...node.data,
                  argsText: argsText,
                  args: args,
                });
              }}
              placeholder="server-name@latest"
              rows={3}
            />
            <div className="studio-prop-hint">{t('studio.prop.argumentsHint')}</div>
          </div>

          <div className="field">
            <label>{t('studio.prop.envVars')}</label>
            <textarea
              className="input mono"
              style={{ resize: 'vertical' }}
              value={
                data.envText ||
                (data.env && Object.keys(data.env).length > 0
                  ? JSON.stringify(data.env, null, 2)
                  : '')
              }
              onChange={(e) => {
                const envText = e.target.value.trim();
                try {
                  const env = envText ? JSON.parse(envText) : {};
                  handleInputChange('envText', envText);
                  handleInputChange('env', env);
                } catch {
                  // Keep the text even if JSON is invalid for user to continue editing
                  handleInputChange('envText', envText);
                }
              }}
              placeholder={'{\n  "PATH": "/usr/local/bin",\n  "API_KEY": "your-key"\n}'}
              rows={4}
            />
            <div className="studio-prop-hint">{t('studio.prop.envVarsHint')}</div>
          </div>
        </>
      )}

      {(data.transportType === 'streamable_http' || data.transportType === 'sse') && (
        <>
          <div className="field">
            <label>{t('studio.prop.serverUrl')}</label>
            <input
              className="input"
              type="url"
              value={data.url || ''}
              onChange={(e) => handleInputChange('url', e.target.value)}
              placeholder="http://localhost:8000/mcp"
            />
          </div>

          <div className="field">
            <label>{t('studio.prop.headers')}</label>
            <textarea
              className="input mono"
              style={{ resize: 'vertical' }}
              value={data.headersText || ''}
              onChange={(e) => {
                const headersText = e.target.value;
                try {
                  const headers = headersText ? JSON.parse(headersText) : {};
                  handleInputChange('headersText', headersText);
                  handleInputChange('headers', headers);
                } catch {
                  handleInputChange('headersText', headersText);
                }
              }}
              placeholder='{"Authorization": "Bearer token"}'
              rows={3}
            />
          </div>
        </>
      )}

      <div className="field">
        <label>{t('studio.prop.timeout')}</label>
        <input
          className="input"
          type="number"
          value={data.timeout || 30}
          onChange={(e) => handleInputChange('timeout', parseInt(e.target.value))}
          min="1"
          max="300"
        />
      </div>

      <div className="field">
        <label>{t('studio.prop.description')}</label>
        <textarea
          className="input"
          style={{ resize: 'vertical' }}
          value={data.description || ''}
          onChange={(e) => handleInputChange('description', e.target.value)}
          placeholder={t('studio.prop.mcpDescPlaceholder')}
          rows={3}
        />
      </div>
    </div>
  );

  const renderCustomToolProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.toolName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder={t('studio.prop.customToolPlaceholder')}
        />
      </div>

      <div className="field">
        <label>{t('studio.prop.pythonFunction')}</label>
        <textarea
          className="input mono"
          style={{ minHeight: 220, resize: 'vertical' }}
          value={data.pythonCode || ''}
          onChange={(e) => handleInputChange('pythonCode', e.target.value)}
          placeholder={
            'def word_counter(text: str) -> str:\n    """Count words in the provided text"""\n    word_count = len(text.split())\n    return f"Word count: {word_count}"'
          }
          rows={12}
        />
        <div className="studio-prop-hint">{t('studio.prop.pythonHint')}</div>
      </div>
    </div>
  );

  const renderGraphBuilderProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.graphName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder="Graph"
        />
        <div className="studio-prop-hint">{t('studio.prop.graphNameHint')}</div>
      </div>

      <div className="studio-prop-sect">
        <div className="kicker" style={{ marginBottom: 8 }}>
          {t('studio.prop.entryPoints')}
        </div>
        <div className="studio-prop-hint">{t('studio.prop.entryPointsHint')}</div>
      </div>

      <div className="studio-prop-sect">
        <div className="kicker" style={{ marginBottom: 8 }}>
          {t('studio.prop.agentDeps')}
        </div>
        <div className="studio-prop-hint">{t('studio.prop.agentDepsHint')}</div>
      </div>

      <div className="field">
        <label className="studio-check">
          <input
            type="checkbox"
            checked={data.enableDebugLogs || false}
            onChange={(e) => handleInputChange('enableDebugLogs', e.target.checked)}
          />
          <span>{t('studio.prop.enableDebugLogs')}</span>
        </label>
        <div className="studio-prop-hint">{t('studio.prop.debugLogsHint')}</div>
      </div>

      <div className="field">
        <label>{t('studio.prop.executionTimeout')}</label>
        <input
          className="input"
          type="number"
          value={data.executionTimeout || ''}
          onChange={(e) =>
            handleInputChange('executionTimeout', e.target.value ? parseInt(e.target.value) : undefined)
          }
          placeholder={t('studio.prop.optionalPlaceholder')}
          min="1"
        />
        <div className="studio-prop-hint">{t('studio.prop.noTimeoutHint')}</div>
      </div>
    </div>
  );

  const renderSwarmProperties = (data: StudioNodeData) => (
    <div>
      <div className="field">
        <label>{t('studio.prop.swarmName')}</label>
        <input
          className="input"
          type="text"
          value={data.label || ''}
          onChange={(e) => handleInputChange('label', e.target.value)}
          placeholder={t('studio.prop.swarmNamePlaceholder')}
        />
      </div>

      <div className="studio-prop-sect">
        <div className="kicker" style={{ marginBottom: 10 }}>
          {t('studio.prop.executionSettings')}
        </div>

        <div className="field">
          <label>{t('studio.prop.maxHandoffs')}</label>
          <input
            className="input"
            type="number"
            value={data.maxHandoffs || 20}
            onChange={(e) => handleInputChange('maxHandoffs', parseInt(e.target.value))}
            min="1"
            max="100"
          />
          <div className="studio-prop-hint">{t('studio.prop.maxHandoffsHint')}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.maxIterations')}</label>
          <input
            className="input"
            type="number"
            value={data.maxIterations || 20}
            onChange={(e) => handleInputChange('maxIterations', parseInt(e.target.value))}
            min="1"
            max="100"
          />
          <div className="studio-prop-hint">{t('studio.prop.maxIterationsHint')}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.executionTimeout')}</label>
          <input
            className="input"
            type="number"
            value={data.executionTimeout || 900}
            onChange={(e) => handleInputChange('executionTimeout', parseInt(e.target.value))}
            min="10"
            max="3600"
          />
          <div className="studio-prop-hint">{t('studio.prop.execTimeoutHint')}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.nodeTimeout')}</label>
          <input
            className="input"
            type="number"
            value={data.nodeTimeout || 300}
            onChange={(e) => handleInputChange('nodeTimeout', parseInt(e.target.value))}
            min="5"
            max="1800"
          />
          <div className="studio-prop-hint">{t('studio.prop.nodeTimeoutHint')}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.repetitiveWindow')}</label>
          <input
            className="input"
            type="number"
            value={data.repetitiveHandoffDetectionWindow || 0}
            onChange={(e) =>
              handleInputChange('repetitiveHandoffDetectionWindow', parseInt(e.target.value))
            }
            min="0"
            max="20"
          />
          <div className="studio-prop-hint">{t('studio.prop.repetitiveWindowHint')}</div>
        </div>

        <div className="field">
          <label>{t('studio.prop.minUniqueAgents')}</label>
          <input
            className="input"
            type="number"
            value={data.repetitiveHandoffMinUniqueAgents || 0}
            onChange={(e) =>
              handleInputChange('repetitiveHandoffMinUniqueAgents', parseInt(e.target.value))
            }
            min="0"
            max="10"
          />
          <div className="studio-prop-hint">{t('studio.prop.minUniqueAgentsHint')}</div>
        </div>
      </div>
    </div>
  );

  const renderProperties = () => {
    const data = node.data as StudioNodeData;
    switch (node.type) {
      case 'agent':
        return renderAgentProperties(data);
      case 'orchestrator-agent':
        return renderOrchestratorAgentProperties(data);
      case 'swarm':
        return renderSwarmProperties(data);
      case 'graph-builder':
        return renderGraphBuilderProperties(data);
      case 'tool':
        return renderToolProperties(data);
      case 'mcp-tool':
        return renderMCPToolProperties(data);
      case 'input':
        return renderInputProperties();
      case 'custom-tool':
        return renderCustomToolProperties(data);
      case 'skill':
        return <SkillNodeProperties key={node.id} node={node} onUpdateNode={onUpdateNode} />;
      default:
        return <div className="studio-prop-empty">{t('studio.prop.noProps')}</div>;
    }
  };

  return (
    <div className={`studio-prop ${className}`}>
      <div className="studio-prop-head">
        <Settings size={14} />
        <h3>{t('studio.prop.title')}</h3>
        <button className="studio-prop-x" onClick={onClose} title={t('studio.prop.close')}>
          <X size={14} />
        </button>
      </div>

      <div className="studio-prop-body">
        <div className="studio-prop-type">
          <label
            style={{
              display: 'block',
              fontFamily: 'var(--mono)',
              fontSize: 9.5,
              letterSpacing: '0.18em',
              color: 'var(--ink-3)',
              marginBottom: 6,
            }}
          >
            {t('studio.prop.nodeType')}
          </label>
          <div className="studio-prop-typev">{node.type}</div>
        </div>

        {renderProperties()}
      </div>
    </div>
  );
}
