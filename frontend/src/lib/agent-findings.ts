/** Failure Analysis snapshots and live patterns. Preview report trees stay in evaluation.ts. */
export { FAILURE_ANALYSIS_EVALUATOR as FAILURE_ANALYSIS_ID } from "./evaluators";
export interface FindingSample {
  sessionId: string;
  lastSeenAt?: string;
  findings?: { targetId?: string; targetType?: string; description?: string; category?: string }[];
}
export interface FailureFinding {
  name?: string;
  description?: string;
  affectedSessionCount?: number;
  sampleSessions?: FindingSample[];
  rootCauses?: {
    name?: string;
    explanation?: string;
    affectedSessionCount?: number;
    suggestedFix?: string;
    sampleSessions?: FindingSample[];
  }[];
}
export interface BatchFindings {
  source: "batch";
  state: "pending" | "ready" | "partial" | "unavailable" | "not_applicable" | "error";
  aws_status: string | null;
  findings: FailureFinding[];
  diagnostics: string[];
}
export interface AgentFinding extends FailureFinding {
  agentInsightId: string;
  applicationName: string;
  customerMetadataStatus?: "AVAILABLE" | "CUSTOMER_KEY_UNAVAILABLE" | "UNAVAILABLE";
  lastSeenAt?: string;
  updatedAt?: string;
}
export interface AgentFindingsPage {
  application_name: string;
  items: AgentFinding[];
  next_cursor: string | null;
}
export interface InsightStore {
  state: "ready" | "disabled" | "updating" | "error" | "denied" | "unsupported" | "unavailable";
}
