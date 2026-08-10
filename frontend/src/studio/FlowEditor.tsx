import React, { useCallback, useRef } from 'react';
import { useTranslation } from 'react-i18next';
import {
  ReactFlow,
  useNodesState,
  useEdgesState,
  addEdge,
  Controls,
  MiniMap,
  Background,
  BackgroundVariant,
  type Node,
  type Edge,
  type Connection,
  type OnConnect,
  type OnNodesChange,
  type OnEdgesChange,
  type NodeChange,
  type EdgeChange,
  type ReactFlowInstance,
} from '@xyflow/react';

import '@xyflow/react/dist/style.css';
// studio.css is imported here (the canvas root); every studio class + React Flow
// dark-theme override lives in it. Importing once from the canvas entry is enough
// because Vite bundles it globally and the page always mounts FlowEditor.
import './studio.css';
import { Network } from 'lucide-react';

import {
  AgentNode,
  OrchestratorAgentNode,
  SwarmNode,
  ToolNode,
  InputNode,
  OutputNode,
  CustomToolNode,
  MCPToolNode,
  SkillNode,
} from './nodes';
import { isValidConnection } from './lib/connection-validator';
import {
  DEFAULT_MANTLE_MODEL_ID,
  DEFAULT_MANTLE_REGION,
  MANTLE_PROVIDER,
  mantleBaseUrl,
} from './lib/models';

const initialNodes: Node[] = [];
const initialEdges: Edge[] = [];

// graph-builder is intentionally NOT registered — it is not droppable; Graph Mode
// is a canvas-level toggle, not a container node.
const nodeTypes = {
  agent: AgentNode,
  'orchestrator-agent': OrchestratorAgentNode,
  swarm: SwarmNode,
  tool: ToolNode,
  'mcp-tool': MCPToolNode,
  input: InputNode,
  output: OutputNode,
  'custom-tool': CustomToolNode,
  skill: SkillNode,
};

interface FlowEditorProps {
  className?: string;
  onNodeSelect?: (node: Node | null) => void;
  nodes?: Node[];
  onNodesChange?: (nodes: Node[]) => void;
  edges?: Edge[];
  onEdgesChange?: (edges: Edge[]) => void;
  graphMode?: boolean;
  onGraphModeChange?: (enabled: boolean) => void;
  /** Called with a human-readable reason when a connection is rejected.
   *  The page wires this to its toast; FlowEditor stays toast-agnostic. */
  onInvalidConnection?: (message: string) => void;
}

export function FlowEditor({
  className = '',
  onNodeSelect,
  nodes: externalNodes,
  onNodesChange: externalOnNodesChange,
  edges: externalEdges,
  onEdgesChange: externalOnEdgesChange,
  graphMode = false,
  onGraphModeChange,
  onInvalidConnection,
}: FlowEditorProps) {
  const { t } = useTranslation();
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const [internalNodes, setInternalNodes, onInternalNodesChange]: [
    Node[],
    (nodes: Node[]) => void,
    OnNodesChange,
  ] = useNodesState(initialNodes);

  // Use external nodes if provided, otherwise use internal state
  const nodes = externalNodes || internalNodes;
  const setNodes = externalOnNodesChange || setInternalNodes;
  const onNodesChange = externalOnNodesChange
    ? (changes: NodeChange[]) => {
        // Get removed node IDs first
        const removedNodeIds = changes.flatMap((change) =>
          change.type === 'remove' ? [change.id] : [],
        );

        // Apply changes to external nodes
        const updatedNodes = nodes
          .map((node) => {
            const change = changes.find((c) => 'id' in c && c.id === node.id);
            if (!change) return node;

            switch (change.type) {
              case 'position':
                return { ...node, position: change.position ?? node.position };
              case 'select':
                return { ...node, selected: change.selected };
              case 'remove':
                return null;
              default:
                return node;
            }
          })
          .filter((n): n is Node => n !== null);

        externalOnNodesChange(updatedNodes);

        // Also remove connected edges when nodes are deleted
        if (removedNodeIds.length > 0 && externalOnEdgesChange) {
          const updatedEdges = edges.filter(
            (edge) => !removedNodeIds.includes(edge.source) && !removedNodeIds.includes(edge.target),
          );
          externalOnEdgesChange(updatedEdges);
        }
      }
    : onInternalNodesChange;

  const [internalEdges, setInternalEdges, onInternalEdgesChange]: [
    Edge[],
    (edges: Edge[]) => void,
    OnEdgesChange,
  ] = useEdgesState(initialEdges);

  // Use external edges if provided, otherwise use internal state
  const edges = externalEdges || internalEdges;
  const setEdges = externalOnEdgesChange || setInternalEdges;
  const onEdgesChange = externalOnEdgesChange
    ? (changes: EdgeChange[]) => {
        // Apply changes to external edges properly
        const updatedEdges = edges
          .map((edge) => {
            const change = changes.find((c) => 'id' in c && c.id === edge.id);
            if (!change) return edge;

            switch (change.type) {
              case 'remove':
                return null;
              case 'select':
                return { ...edge, selected: change.selected };
              default:
                return edge;
            }
          })
          .filter((e): e is Edge => e !== null);

        externalOnEdgesChange(updatedEdges);
      }
    : onInternalEdgesChange;
  const [reactFlowInstance, setReactFlowInstance] = React.useState<ReactFlowInstance | null>(null);

  const onConnect: OnConnect = useCallback(
    (params: Connection) => {
      const validation = isValidConnection(params, nodes, edges, graphMode);
      if (validation.valid) {
        setEdges(addEdge(params, edges));
      } else {
        // Surface a user-friendly reason via the page's toast (upstream used alert()).
        // validation.message comes from the pure connection-validator (English);
        // only the generic fallback is localized here.
        onInvalidConnection?.(validation.message ?? t('studio.flow.connectionNotAllowed'));
      }
    },
    [setEdges, nodes, edges, graphMode, onInvalidConnection, t],
  );

  const isValidConnectionCallback = useCallback(
    (connection: Connection) => {
      const validation = isValidConnection(connection, nodes, edges, graphMode);
      return validation.valid;
    },
    [nodes, edges, graphMode],
  );

  const onNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      onNodeSelect?.(node);
    },
    [onNodeSelect],
  );

  const onPaneClick = useCallback(() => {
    onNodeSelect?.(null);
  }, [onNodeSelect]);

  const onDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  const onDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();

      if (!reactFlowWrapper.current || !reactFlowInstance) {
        return;
      }

      const reactFlowBounds = reactFlowWrapper.current.getBoundingClientRect();
      const type = event.dataTransfer.getData('application/reactflow');

      if (!type) {
        return;
      }

      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX - reactFlowBounds.left,
        y: event.clientY - reactFlowBounds.top,
      });

      const defaultData: Record<string, unknown> = { label: `${type} node` };

      // Set default values for agent nodes
      if (type === 'agent') {
        Object.assign(defaultData, {
          label: 'Agent',
          // Bedrock Mantle GPT-5.6 Sol is the platform default. modelName must
          // repeat the id: for non-Bedrock providers the generators pick
          // modelIdentifier = modelName, not modelId.
          modelProvider: MANTLE_PROVIDER,
          modelId: DEFAULT_MANTLE_MODEL_ID,
          modelName: DEFAULT_MANTLE_MODEL_ID,
          // Mantle auth is IAM-only unless an apiKey is entered on the node.
          region: DEFAULT_MANTLE_REGION,
          baseUrl: mantleBaseUrl(DEFAULT_MANTLE_REGION, DEFAULT_MANTLE_MODEL_ID),
          systemPrompt: 'You are a helpful AI assistant.',
          // launchpad extension: new agents default to streaming ON + 32000 max output tokens
          maxTokens: 32000,
          streaming: true,
        });
      }

      // Set default values for skill nodes
      if (type === 'skill') {
        Object.assign(defaultData, {
          label: 'Skill',
          skillName: '',
          description: '',
        });
      }

      // Set default values for MCP tool nodes
      if (type === 'mcp-tool') {
        Object.assign(defaultData, {
          label: 'MCP Server',
          serverName: 'mcp_server',
          transportType: 'stdio',
          command: 'uvx',
          args: ['server-name@latest'],
          argsText: 'server-name@latest',
          url: 'http://localhost:8000/mcp',
          timeout: 30,
          description: 'MCP server for external tools',
          env: {},
          envText: '',
        });
      }

      const newNode: Node = {
        id: `${type}_${Date.now()}`,
        type,
        position,
        data: defaultData,
      };

      setNodes([...nodes, newNode]);
    },
    [reactFlowInstance, setNodes, nodes],
  );

  return (
    <div className={`studio-flow ${className}`} ref={reactFlowWrapper}>
      {/* Graph Mode Toggle */}
      <div className={`studio-graphmode${graphMode ? ' on' : ''}`}>
        <Network className="studio-graphmode-ic" size={14} />
        <span className="studio-graphmode-lbl">{t('studio.flow.graphMode')}</span>
        <button
          type="button"
          className={`studio-switch${graphMode ? ' on' : ''}`}
          onClick={() => onGraphModeChange?.(!graphMode)}
          title={t('studio.flow.graphModeTitle')}
          aria-pressed={graphMode}
        />
      </div>

      <ReactFlow
        colorMode="dark"
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onConnect={onConnect}
        isValidConnection={(edge) => isValidConnectionCallback(edge as Connection)}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        onInit={setReactFlowInstance}
        onDrop={onDrop}
        onDragOver={onDragOver}
        deleteKeyCode={['Delete', 'Backspace']}
        multiSelectionKeyCode={['Meta', 'Ctrl']}
        fitView
        attributionPosition="bottom-left"
      >
        <Controls />
        <MiniMap />
        <Background variant={BackgroundVariant.Dots} gap={12} size={1} color="#2E3833" />
      </ReactFlow>
    </div>
  );
}
