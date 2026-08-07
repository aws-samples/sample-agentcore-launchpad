import { createContext, useContext } from "react";

import type { AgentPermission, ConsoleRole } from "../lib/api";

export interface AuthContextValue {
  authRequired: boolean;
  username: string | null;
  role: ConsoleRole | null;
  email: string | null;
  /** ISO account-validity end for registered users; null = never expires */
  accountExpiresAt: string | null;
  /** admin-only surfaces (an open console keeps full local-operator access) */
  isAdmin: boolean;
  /** member-grantable agent-management capability (admins always pass) */
  can: (permission: AgentPermission) => boolean;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  authRequired: false,
  username: null,
  role: null,
  email: null,
  accountExpiresAt: null,
  isAdmin: true,
  can: () => true,
  logout: async () => undefined,
});

export function useAuth() {
  return useContext(AuthContext);
}
