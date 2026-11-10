import { useCallback } from "react";

import { useAuth } from "../auth/auth-context";

/** A stamped row as every list / detail response carries it (`services/creators.py`). */
export interface Stamped {
  created_by?: string | null;
  created_by_name?: string | null;
}

/**
 * The server's creator-or-admin rule (`services/ownership.py`, r13-g), mirrored so the
 * console can hide or disable the actions a member cannot perform instead of letting them
 * run into the 403: an administrator (and the open console's local operator) may mutate
 * every row; a member only the rows their account created; a row with no recorded creator
 * is an administrator's (the CN NULL-creator rule). The server still decides.
 */
export function mayMutate(row: Stamped | null | undefined, isAdmin: boolean, userId: string | null): boolean {
  if (isAdmin) return true;
  const creator = row?.created_by ?? null;
  return creator !== null && userId !== null && creator === userId;
}

/** `mayMutate` bound to the signed-in identity. */
export function useMayMutate(): (row: Stamped | null | undefined) => boolean {
  const { isAdmin, userId } = useAuth();
  return useCallback((row: Stamped | null | undefined) => mayMutate(row, isAdmin, userId), [isAdmin, userId]);
}
