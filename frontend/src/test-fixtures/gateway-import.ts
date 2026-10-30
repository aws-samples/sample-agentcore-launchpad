/** Shared fixtures for the V2 and classic "Import Gateway targets" dialog tests. */
import type { GatewayImportPreview, GatewayImportResponse } from "../lib/api";

export const PREVIEW: GatewayImportPreview = {
  gateways: [
    {
      gateway_id: "gw-1",
      name: "launchpad-gateway",
      gateway_arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/gw-1",
      gateway_url: "https://gw-1.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
      auth: "aws_iam",
      auth_warning: null,
      platform: true,
      live_error: null,
      targets: [
        {
          target_id: "tgt0000001",
          target: "office-facts",
          target_status: "READY",
          tools: 2,
          tool_source: "live",
          record_id: null,
          status: null,
          imported: false,
          held: false,
        },
        {
          target_id: "tgt0000002",
          target: "weather",
          target_status: "READY",
          tools: 1,
          tool_source: "live",
          record_id: "rec-weather",
          status: "APPROVED",
          imported: true,
          held: false,
        },
      ],
    },
    {
      gateway_id: "gw-2",
      name: "partner-gateway",
      gateway_arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/gw-2",
      gateway_url: "https://gw-2.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp",
      auth: "oauth",
      auth_warning: "Gateway outbound auth is not mapped to a Launchpad-managed credential provider",
      platform: false,
      live_error: null,
      targets: [
        {
          target_id: "tgt0000003",
          target: "crm",
          target_status: "READY",
          tools: 2,
          tool_source: "schema",
          record_id: "rec-held",
          status: "DRAFT",
          imported: false,
          held: true,
        },
      ],
    },
  ],
};

export const IMPORTED: GatewayImportResponse = {
  gateway_id: "gw-1",
  gateway_name: "launchpad-gateway",
  gateway_arn: PREVIEW.gateways[0].gateway_arn,
  auth: "aws_iam",
  platform: true,
  live_error: null,
  results: [
    {
      name: "office-facts",
      target: "office-facts",
      target_id: "tgt0000001",
      gateway_id: "gw-1",
      record_id: "rec-office",
      created: true,
      reconciled: false,
      status: "APPROVED",
      tool_source: "live",
    },
    {
      name: "weather",
      target: "weather",
      target_id: "tgt0000002",
      gateway_id: "gw-1",
      record_id: "rec-weather",
      created: false,
      reconciled: false,
      status: "APPROVED",
      tool_source: "live",
    },
  ],
};
