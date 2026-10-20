# COA demo account for the context layer

Most Launchpad members have no account in the connected Context Ontology Accelerator (COA)
deployment, because COA's user pool only accepts admin-created users. This sample sets up one
**shared, read-only COA demo account**. Any signed-in member can then try the V2 **上下文层 /
Context layer** page and the COA-attached agents with it.

## How it fits together

| Piece | What it is |
|---|---|
| Cognito user | `launchpad-demo@coa-demo.example.com` in COA's user pool. Its email is left **unverified**, so "forgot password" cannot reset the shared account. |
| Cognito group | `launchpad-demo`. The COA grant names the group, not the user, so re-creating the user (which gives it a new `sub`) keeps its access. |
| COA grant | `Group::launchpad-demo` → `data-analyst` (read and query only) on the namespaces you choose. |
| SSM SecureString | `/<coa prefix>/launchpad/demo-account` in COA's region, holding `{"username", "password", "sub"}`. |

On the Launchpad side:

- An admin enters the parameter name in the context layer's connect form.
- Members then see 「用体验账号授权」. It reveals the username and password (with copy
  buttons) and opens COA's login page. Nothing is prefilled there: the token vault keys a
  token by the consent's `customParameters`, so Launchpad sends none (no `login_hint`).
- The member signs in with that password, and the existing per-member consent takes over.
  Launchpad never signs in on anyone's behalf.

## Commands

Run them from the repo root with the backend venv, using AWS credentials for COA's account:

```bash
P="backend/.venv/bin/python -I samples/coa-demo/demo_account.py"
A="--prefix coa-w2 --region us-west-2"

$P status $A            # read-only health check; the password is never printed
$P ensure $A            # create or repair the user, group, membership and parameter (idempotent)
$P rotate $A            # new password, written to Cognito and SSM
$P rotate $A --signout  # also end every existing session, which forces every member to re-consent

# one-time COA grant (needs a platform-admin ID token, e.g. from the COA console's local storage)
COA_ADMIN_TOKEN=<token> $P grant $A --namespace <namespace uuid>
```

`ensure` re-applies the stored password on every run. It is the repair step when someone
changes or deletes the account (see below).

## Risks of a shared account

- **Every console member can read the password.** Keep the grant read-only and limited to
  demo namespaces. Launchpad logs who revealed it, never the password itself.
- **The account can be broken.** COA's web and MCP clients allow direct password/SRP sign-in,
  which COA's own `scripts/serve/*.py` depends on. A token obtained that way can change the
  password or delete the user. The recovery is `ensure`; the group grant survives re-creation.
- **One COA identity.** COA's audit logs show a single principal for everyone using the demo
  account; Launchpad's logs record which member did what. Anyone who signs in to the COA
  console as the demo account can see the playground history of the other demo users, so do
  not type sensitive data.
- **A cached COA session wins.** A browser that is already signed in to COA consents as that
  user (COA's classic hosted UI, and Launchpad cannot send `prompt=login`). Launchpad detects this after consent
  and guides the member through signing out of COA.
- **Rotation without `--signout`** leaves consents already in the vault working until their
  refresh tokens expire (30 days).
