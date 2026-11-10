import { afterEach } from "vitest";

import { abandonRestartRetries } from "./lib/restartRetry";

// N17-obs471: a restart-window retry (lib/api.ts) never outlives the test that started it.
afterEach(() => abandonRestartRetries());
