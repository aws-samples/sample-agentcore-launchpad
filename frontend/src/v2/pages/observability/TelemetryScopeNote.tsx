import { useTranslation } from "react-i18next";
import { useAuth } from "../../../auth/auth-context";

export function TelemetryScopeNote() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  return <p className="sub" data-testid="telemetry-scope-note">
    {t(isAdmin ? "v2.observability.scopeAdmin" : "v2.observability.scopeMember")}
  </p>;
}
