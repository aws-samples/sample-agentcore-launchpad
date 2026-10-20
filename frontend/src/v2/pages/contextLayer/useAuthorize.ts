import { useEffect, useRef, useState } from "react";

import { api, type ContextLayerIdentity, errorMessage } from "../../../lib/api";
import { effectiveStatus } from "../../../lib/user-grants";
import { useV2Toast } from "../../hooks";
import { pollAuthorized } from "./state";

/**
 * Start the signed-in member's COA consent (`POST /api/context-layer/authorize`).
 *
 * The consent tab is opened synchronously, inside the click (a tab opened after
 * an await is popup-blocked), then pointed at the single-use authorization URL.
 * `/auth/return` finishes the binding there and closes itself; meanwhile this
 * polls the console grant (`grants/{connection}/status`, a ledger read — not
 * `GET /api/context-layer`, whose live AWS probes are too heavy to poll) and calls
 * `onAuthorized` once it flips. A blocked tab leaves the URL in `pendingUrl` for a
 * plain link. `account` is an explicit choice of COA account (`"demo"` is refused
 * without a demo account); the consent is the same either way and COA uses whatever
 * session the browser holds, else asks for credentials.
 */
export function useAuthorize(onAuthorized: () => void) {
  const toast = useV2Toast();
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [pendingUrl, setPendingUrl] = useState<string | null>(null);
  const cancel = useRef<(() => void) | null>(null);
  const done = useRef(onAuthorized);
  done.current = onAuthorized;
  useEffect(() => () => cancel.current?.(), []);

  const waitForConsent = (connection: string) => {
    cancel.current?.();
    setWaiting(true);
    cancel.current = pollAuthorized({
      // the console grant has no agent (`agent_id=""`)
      check: async () => effectiveStatus(await api.userTokenStatus(connection, "")),
      onAuthorized: () => {
        setWaiting(false);
        setPendingUrl(null);
        done.current();
      },
      onExpired: () => setWaiting(false),
    });
  };

  const authorize = async (account?: ContextLayerIdentity) => {
    const tab = window.open("about:blank", "_blank");
    setBusy(true);
    try {
      const res = await api.authorizeContextLayer(account);
      if (res.authorized || !res.authorization_url) {
        tab?.close();
        done.current();
        return;
      }
      const url = res.authorization_url;
      if (tab && !tab.closed) {
        tab.opener = null;
        tab.location.href = url;
      } else {
        setPendingUrl(url);
      }
      waitForConsent(res.connection);
    } catch (err) {
      tab?.close();
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return { authorize, busy, waiting, pendingUrl };
}
