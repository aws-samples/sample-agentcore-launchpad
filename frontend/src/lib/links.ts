/** External documentation links surfaced in the console chrome. */

/** Hands-on lab guide (AWS Workshop Studio) — rendered in the topbar
 *  and as the Overview call-to-action. */
export const LAB_GUIDE_URL =
  "https://catalog.us-east-1.prod.workshops.aws/workshops/3f18b2a3-fe79-4559-9efd-7c627088f601";

/** The public repo, `main` — the source of the two cross-account references the
 *  workspace registration form links to (a local file path is useless to the
 *  administrator of the target account, who is often a different person). */
const REPO_BLOB = "https://github.com/aws-samples/sample-agentcore-launchpad/blob/main";

/** Full cross-account setup guide, incl. the trust boundary and troubleshooting. */
export const CROSS_ACCOUNT_GUIDE_URL = `${REPO_BLOB}/docs/cross-account-workspaces.md`;

/** The spoke role's CloudFormation template, to hand to the target account. */
export const SPOKE_TEMPLATE_URL = `${REPO_BLOB}/infra/spoke/launchpad-workspace-role.yaml`;
