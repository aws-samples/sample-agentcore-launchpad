// 数据处理 · 运行日志 source helpers: the editor keeps what is typed (empty
// strings, role lists split on every comma) and normalises it only on the way out,
// to the shape `pipeline_logs.LogFormat` validates.
import type { V2LogFormat, V2PipelineSource } from "./api";

export const LOG_PRESETS = ["genai", "message", "exchange"] as const;
export type LogPreset = (typeof LOG_PRESETS)[number];

export const DEFAULT_USER_ROLES = ["user", "human"];
export const DEFAULT_ASSISTANT_ROLES = ["assistant", "ai", "bot"];
export const STREAM_FIELD = "@logStream";
export const GENAI_SESSION_FIELD = "attributes.session.id";

/** mirrors `pipeline_logs.FIELD_PATH_RE` */
const FIELD_PATH_RE = /^(@logStream|[A-Za-z0-9_$-]+(\.[A-Za-z0-9_$-]+)*)$/;

export const defaultLogFormat = (): V2LogFormat => ({
  preset: "genai",
  session_field: "",
  role_field: "",
  text_field: "",
  user_roles: [...DEFAULT_USER_ROLES],
  assistant_roles: [...DEFAULT_ASSISTANT_ROLES],
  input_field: "",
  output_field: "",
});

const field = (value: string | null | undefined): string | null => {
  const v = (value ?? "").trim();
  return v ? v : null;
};

const roles = (value: string[] | undefined, fallback: string[]): string[] => {
  const out = (value ?? []).map((r) => r.trim()).filter(Boolean);
  return out.length ? out : fallback;
};

/** The rule as the API takes it: only the preset's own fields (another preset's
 *  leftovers are dropped), empty ones as null, role lists trimmed. */
export function cleanLogFormat(fmt: V2LogFormat): V2LogFormat {
  const message = fmt.preset === "message";
  const exchange = fmt.preset === "exchange";
  return {
    preset: fmt.preset,
    session_field: field(fmt.session_field),
    role_field: message ? field(fmt.role_field) : null,
    text_field: message ? field(fmt.text_field) : null,
    user_roles: roles(fmt.user_roles, DEFAULT_USER_ROLES),
    assistant_roles: roles(fmt.assistant_roles, DEFAULT_ASSISTANT_ROLES),
    input_field: exchange ? field(fmt.input_field) : null,
    output_field: exchange ? field(fmt.output_field) : null,
  };
}

/** Switch presets. The session field means the same for message and exchange,
 *  but genai's is a different default (attributes.session.id), so it is cleared
 *  whenever genai is entered or left. */
export function switchPreset(fmt: V2LogFormat, preset: V2LogFormat["preset"]): V2LogFormat {
  if (preset === fmt.preset) return fmt;
  const keepSession = preset !== "genai" && fmt.preset !== "genai";
  return { ...fmt, preset, session_field: keepSession ? fmt.session_field : "" };
}

/** The source as the API takes it: a logs source with its rule normalised and
 *  the keyword trimmed; a traces source drops the logs-only fields. */
export function cleanSource(src: V2PipelineSource): V2PipelineSource {
  if (src.type !== "logs") {
    return { type: "traces", agent: src.agent, range: src.range, status: src.status, max_sessions: src.max_sessions };
  }
  return {
    ...src,
    keyword: field(src.keyword),
    log_groups: src.log_groups ?? [],
    format: cleanLogFormat(src.format ?? defaultLogFormat()),
  };
}

export type LogSourceProblem = "noGroups" | "badPath" | "needSession" | "needRole" | "needText" | "needInput";

/** Problem → its message key (the save button and the preview share them). */
export const PROBLEM_KEY: Record<LogSourceProblem, string> = {
  noGroups: "v2.pipelines.logs.errGroups",
  badPath: "v2.pipelines.logs.errPath",
  needSession: "v2.pipelines.logs.errSession",
  needRole: "v2.pipelines.logs.errRole",
  needText: "v2.pipelines.logs.errText",
  needInput: "v2.pipelines.logs.errInput",
};

/** The first thing that stops a logs source from being saved or previewed. */
export function logSourceProblem(src: V2PipelineSource): LogSourceProblem | null {
  if (!src.log_groups?.length) return "noGroups";
  const fmt = cleanLogFormat(src.format ?? defaultLogFormat());
  const paths = [fmt.session_field, fmt.role_field, fmt.text_field, fmt.input_field, fmt.output_field];
  if (paths.some((p) => p != null && (p.length > 128 || !FIELD_PATH_RE.test(p)))) return "badPath";
  if (fmt.preset === "message") {
    if (!fmt.session_field) return "needSession";
    if (!fmt.role_field) return "needRole";
    if (!fmt.text_field) return "needText";
  }
  if (fmt.preset === "exchange" && !fmt.input_field) return "needInput";
  return null;
}

/** Field path suggestions: the preview's paths, with `@logStream` for session keys. */
export function pathOptions(paths: string[], sessionKey = false): string[] {
  return sessionKey ? [STREAM_FIELD, ...paths.filter((p) => p !== STREAM_FIELD)] : paths;
}
