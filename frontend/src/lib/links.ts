/** External documentation links surfaced in the console chrome. */

/** The public repo, `main` — the source of the two cross-account references the
 *  workspace registration form links to (a local file path is useless to the
 *  administrator of the target account, who is often a different person). */
const REPO_BLOB = "https://github.com/aws-samples/sample-agentcore-launchpad/blob/main";

/** Full cross-account setup guide, incl. the trust boundary and troubleshooting. */
export const CROSS_ACCOUNT_GUIDE_URL = `${REPO_BLOB}/docs/cross-account-workspaces.md`;

/** The spoke role's CloudFormation template, to hand to the target account. */
export const SPOKE_TEMPLATE_URL = `${REPO_BLOB}/infra/spoke/launchpad-workspace-role.yaml`;
