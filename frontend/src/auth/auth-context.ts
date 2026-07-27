import { createContext, useContext } from "react";

import type { ConsoleRole } from "../lib/api";

export interface AuthContextValue {
  authRequired: boolean;
  username: string | null;
  role: ConsoleRole | null;
  email: string | null;
  /** ISO account-validity end for registered users; null = never expires */
  accountExpiresAt: string | null;
  /** admin-only surfaces (an open console keeps full local-operator access) */
  isAdmin: boolean;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthContextValue>({
  authRequired: false,
  username: null,
  role: null,
  email: null,
  accountExpiresAt: null,
  isAdmin: true,
  logout: async () => undefined,
});

export function useAuth() {
  return useContext(AuthContext);
}
