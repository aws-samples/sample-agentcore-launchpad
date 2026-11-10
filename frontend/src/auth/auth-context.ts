import { createContext, useContext } from "react";

import type { AgentPermission, ConsoleRole } from "../lib/api";

export interface AuthContextValue {
  authRequired: boolean;
  username: string | null;
  /** `users.id` of the signed-in account — the ownership key the server compares
   *  `created_by` against; null for the config admin and on an open console */
  userId: string | null;
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
  userId: null,
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
