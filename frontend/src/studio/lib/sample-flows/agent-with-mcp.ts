import type { SampleFlow } from './types';

export const agentWithMcp: SampleFlow = {
  id: 'agent-with-mcp',
  name: 'Agent with MCP Server',
  description:
    'An agent connected to the public AWS Knowledge MCP server over streamable HTTP (https://knowledge-mcp.global.api.aws, no auth). Ask it AWS documentation questions.',
  level: 'basic',
  graphMode: false,
  nodes: [
    {
      id: 'input-1003',
      type: 'input',
      position: { x: 40, y: 100 },
      data: {
        label: 'User Input',
        inputType: 'user-prompt',
      },
    },
    {
      id: 'agent-2003',
      type: 'agent',
      position: { x: 360, y: 80 },
      data: {
        label: 'Docs Agent',
        modelProvider: 'AWS Bedrock',
        modelId: 'global.anthropic.claude-sonnet-5',
        modelName: 'Claude Sonnet 4.6',
        systemPrompt: 'You are a documentation assistant. Use the available MCP tools to search and fetch documentation before answering.',
        temperature: 0.7,
        maxTokens: 4000,
        streaming: false,
      },
    },
    {
      id: 'mcp-6003',
      type: 'mcp-tool',
      position: { x: 40, y: 320 },
      data: {
        label: 'AWS Knowledge MCP',
        serverName: 'aws_knowledge',
        transportType: 'streamable_http',
        url: 'https://knowledge-mcp.global.api.aws',
        timeout: 30,
        description: 'AWS Knowledge MCP (public, no auth)',
      },
    },
    {
      id: 'output-3003',
      type: 'output',
      position: { x: 720, y: 100 },
      data: {
        label: 'Output',
      },
    },
  ],
  edges: [
    {
      id: 'e-1003-2003',
      source: 'input-1003',
      target: 'agent-2003',
      sourceHandle: 'output',
      targetHandle: 'user-input',
    },
    {
      id: 'e-6003-2003',
      source: 'mcp-6003',
      target: 'agent-2003',
      sourceHandle: 'mcp-tools',
      targetHandle: 'tools',
    },
    {
      id: 'e-2003-3003',
      source: 'agent-2003',
      target: 'output-3003',
      sourceHandle: 'output',
      targetHandle: 'input',
    },
  ],
};
