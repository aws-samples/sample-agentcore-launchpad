import { Handle, Position, type NodeProps, useReactFlow } from '@xyflow/react';
import { useTranslation } from 'react-i18next';
import { Bot, Settings, X } from 'lucide-react';

interface AgentNodeData {
  label?: string;
  modelProvider?: string;
  modelId?: string;
  modelName?: string;
  systemPrompt?: string;
  maxTokens?: number;
  streaming?: boolean;
  // OpenAI-specific fields
  apiKey?: string;
  baseUrl?: string;
  // Thinking settings
  thinkingEnabled?: boolean;
  thinkingBudgetTokens?: number;
  reasoningEffort?: 'low' | 'medium' | 'high';
}

export function AgentNode({ data, selected, id }: NodeProps) {
  const { t } = useTranslation();
  const { deleteElements } = useReactFlow();
  const nodeData = (data ?? {}) as AgentNodeData;
  const {
    label = 'Agent',
    modelProvider = 'AWS Bedrock',
    modelName = 'Claude 3.7 Sonnet',
  } = nodeData;

  const handleDelete = (event: React.MouseEvent) => {
    event.stopPropagation();
    deleteElements({ nodes: [{ id }] });
  };

  return (
    <div className={`studio-node t-amber${selected ? ' sel' : ''}`}>
      <div className="studio-node-head">
        <Bot className="studio-node-ic" size={14} />
        <span className="studio-node-title">{label}</span>
        <span className="studio-node-tools">
          <Settings size={12} />
          {selected && (
            <button className="studio-node-del" onClick={handleDelete} title={t('studio.nodeCard.deleteTitle')}>
              <X size={12} />
            </button>
          )}
        </span>
      </div>

      <div className="studio-node-body">
        <div className="studio-node-row"><span className="studio-node-k">{t('studio.nodeCard.provider')}</span> {modelProvider}</div>
        <div className="studio-node-row"><span className="studio-node-k">{t('studio.nodeCard.model')}</span> {modelName}</div>
      </div>

      {/* Input Handle */}
      <Handle type="target" position={Position.Top} id="user-input" className="h-in" title="Input: user prompt" />

      {/* Tool Handles (left side) */}
      <Handle type="target" position={Position.Left} id="tools" className="h-tool" style={{ top: '38%' }} title="Tools" />
      <Handle
        type="target"
        position={Position.Left}
        id="orchestrator-input"
        className="h-out"
        style={{ top: '72%' }}
        title="Orchestrator input"
      />

      {/* Output Handle */}
      <Handle type="source" position={Position.Bottom} id="output" className="h-out" title="Output" />
    </div>
  );
}
