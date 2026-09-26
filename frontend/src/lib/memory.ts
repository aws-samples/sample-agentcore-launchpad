/** Memory resource rules shared by the classic and V2 memory consoles —
 *  CreateMemory / UpdateMemory constraints mirrored client-side so bad input
 *  gates the button instead of failing server-side. */

/** CreateMemory's own name constraint. */
export const MEMORY_NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;

/** Order matters: it is the order sent to CreateMemory and shown as chips. */
export const MEMORY_STRATEGY_KEYS = ["semantic", "user_preference", "summarization", "episodic"] as const;
export type MemoryStrategyKey = (typeof MEMORY_STRATEGY_KEYS)[number];
export const DEFAULT_MEMORY_STRATEGIES: MemoryStrategyKey[] = ["semantic", "user_preference"];

/** The namespace each strategy pick actually gets — mirrors the backend's
 *  canned layout (`services/memory_admin.py` STRATEGIES). */
export const MEMORY_STRATEGY_NAMESPACES: Record<MemoryStrategyKey, string> = {
  semantic: "/facts/{actorId}",
  user_preference: "/preferences/{actorId}",
  summarization: "/summaries/{actorId}/{sessionId}",
  episodic: "/episodes/{actorId}/{sessionId}",
};

/** Event-expiry range: CreateMemory accepts 3 days, UpdateMemory holds a floor of 7. */
export const CREATE_EXPIRY_MIN = 3;
export const EDIT_EXPIRY_MIN = 7;
export const EXPIRY_MAX = 365;
export const DEFAULT_EXPIRY_DAYS = 30;

export function expiryValid(days: number, min: number): boolean {
  return Number.isInteger(days) && days >= min && days <= EXPIRY_MAX;
}
