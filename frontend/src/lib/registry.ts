// Registry record helpers shared by the classic Registry page and the Console V2
// registry module: descriptor parsing (AGENT_SKILLS bundle, A2A agent card, MCP
// server url, SKILL.md) and the naming rule the register/import forms enforce.
import type { RegistryRecordOut } from "./api";

export type RegistryRecordType = RegistryRecordOut["type"];

export const REGISTRY_TYPES: RegistryRecordType[] = ["A2A", "MCP", "AGENT_SKILLS"];

/** Control-plane record lifecycle (DEPRECATED is terminal; REJECTED is recoverable). */
export const REGISTRY_STATUSES = [
  "DRAFT",
  "PENDING_APPROVAL",
  "APPROVED",
  "REJECTED",
  "DEPRECATED",
] as const;

/** Record / skill names: the artifacts-bucket S3 prefix is keyed by it. */
export const REGISTRY_NAME_RE = /^[a-z][a-z0-9-]{2,63}$/;

export interface SkillSourceMeta {
  kind: string;
  url?: string;
  ref?: string;
  /** Full commit SHA the files came from; absent on pre-pinning records. */
  commit?: string;
  subdir?: string;
  imported_at?: string;
}

interface WithDescriptors {
  type: string;
  descriptors?: Record<string, unknown>;
}

/** Parse the AGENT_SKILLS skillDefinition JSON; returns file list + source when present. */
export function parseSkillDefinition(
  record: WithDescriptors,
): { files: string[]; source: SkillSourceMeta | null } | null {
  if (record.type !== "AGENT_SKILLS") return null;
  const skills = record.descriptors?.agentSkills as
    | { skillDefinition?: { inlineContent?: string } }
    | undefined;
  const raw = skills?.skillDefinition?.inlineContent;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { files?: unknown; source?: unknown };
    const files = Array.isArray(parsed.files)
      ? parsed.files.filter((f): f is string => typeof f === "string")
      : [];
    const source =
      parsed.source && typeof parsed.source === "object"
        ? (parsed.source as SkillSourceMeta)
        : null;
    if (files.length === 0 && !source) return null;
    return { files, source };
  } catch {
    return null;
  }
}

/** The mount path an AGENT_SKILLS record carries (`?skill=` prefill of the agent
 *  wizard); falls back to the record name. */
export function skillPath(record: WithDescriptors & { name: string }): string {
  try {
    const skills = record.descriptors?.agentSkills as
      | { skillDefinition?: { inlineContent?: string } }
      | undefined;
    const definition = JSON.parse(skills?.skillDefinition?.inlineContent ?? "{}") as {
      path?: string;
    };
    if (definition.path) return definition.path;
  } catch {
    /* fall back to the record name */
  }
  return record.name;
}

/** Parsed A2A AgentCard from the record descriptor. */
export interface AgentCardData {
  url?: string;
  description?: string;
  version?: string;
  skills?: { id?: string; name?: string; description?: string; tags?: string[] }[];
  capabilities?: { streaming?: boolean };
  metadata?: Record<string, string>;
}

export function parseAgentCard(record: WithDescriptors): AgentCardData | null {
  if (record.type !== "A2A") return null;
  const a2a = record.descriptors?.a2a as { agentCard?: { inlineContent?: string } } | undefined;
  const raw = a2a?.agentCard?.inlineContent;
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AgentCardData;
  } catch {
    return null;
  }
}

/** Descriptor JSON with nested `inlineContent` strings expanded, truncated for display. */
export function descriptorExcerpt(record: WithDescriptors, limit = 1800): string {
  const d = record.descriptors ?? {};
  try {
    const raw = JSON.stringify(d);
    const parsed = JSON.parse(raw, (key, value: unknown) => {
      if (key === "inlineContent" && typeof value === "string") {
        try {
          return JSON.parse(value) as unknown;
        } catch {
          return value.length > 400 ? value.slice(0, 400) + "…" : value;
        }
      }
      return value;
    }) as unknown;
    return JSON.stringify(parsed, null, 2).slice(0, limit);
  } catch {
    return JSON.stringify(d, null, 2).slice(0, limit);
  }
}

/** MCP server url lives in descriptors.mcp.server.inlineContent → remotes[0].url. */
export function parseMcpUrl(record: WithDescriptors): string {
  const mcp = record.descriptors?.mcp as { server?: { inlineContent?: string } } | undefined;
  const raw = mcp?.server?.inlineContent;
  if (!raw) return "";
  try {
    const parsed = JSON.parse(raw) as { remotes?: { url?: string }[]; url?: string };
    const remoteUrl = Array.isArray(parsed.remotes) ? parsed.remotes[0]?.url : undefined;
    return remoteUrl ?? parsed.url ?? "";
  } catch {
    return "";
  }
}

export function parseSkillMd(record: WithDescriptors): string {
  const skills = record.descriptors?.agentSkills as
    | { skillMd?: { inlineContent?: string } }
    | undefined;
  return skills?.skillMd?.inlineContent ?? "";
}
