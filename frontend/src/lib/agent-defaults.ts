/** Ordinary agent defaults; system presets supply their own explicit budgets. */
export const DEFAULT_TIMEOUT_SECONDS = 600;

/** A new Managed Harness starts with both native tools ticked (shell + file operations). */
export const DEFAULT_HARNESS_NATIVE_TOOLS = ["shell", "file_operations"] as const;
