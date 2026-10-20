"""Shared read-only COA demo account for Launchpad's context layer.

Creates or repairs one Cognito user in a COA deployment's user pool, adds it to the
``launchpad-demo`` group, and keeps its credentials in an SSM SecureString that
Launchpad reveals to signed-in members (V2 上下文层 → 用体验账号授权). Launchpad itself
never creates or edits COA users; this operator script does.

Run with the backend venv (it has boto3), from the repo root:

  backend/.venv/bin/python -I samples/coa-demo/demo_account.py status --prefix coa-w2 --region us-west-2
  backend/.venv/bin/python -I samples/coa-demo/demo_account.py ensure --prefix coa-w2 --region us-west-2
  backend/.venv/bin/python -I samples/coa-demo/demo_account.py rotate --prefix coa-w2 --region us-west-2 [--signout]
  COA_ADMIN_TOKEN=<platform-admin ID token> backend/.venv/bin/python -I samples/coa-demo/demo_account.py \
      grant --prefix coa-w2 --region us-west-2 --namespace <namespace uuid>

The password is never printed. See README.md for the risks of a shared account.
"""

import argparse
import json
import os
import secrets
import string
import sys
import urllib.error
import urllib.request

import boto3
from botocore.exceptions import ClientError

USERNAME = "launchpad-demo@coa-demo.example.com"
DISPLAY_NAME = "Launchpad 体验账号"
GROUP = "launchpad-demo"
GROUP_DESCRIPTION = "Launchpad shared demo account (read-only)"
ROLE = "data-analyst"
TAGS = [{"Key": "managed-by", "Value": "launchpad-samples/coa-demo"}]
# Cognito accepts these symbols; quotes, backslashes and spaces are left out so the
# password survives copy-paste into the hosted UI.
SYMBOLS = "!@#%^*-_=+"


def param_name(prefix: str) -> str:
    return f"/{prefix}/launchpad/demo-account"


def new_password(length: int = 24) -> str:
    """A password that satisfies the COA pool policy (>= 12, upper, lower, digit, symbol)."""
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while True:
        value = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in value) and any(c.isupper() for c in value)
                and any(c.isdigit() for c in value) and any(c in SYMBOLS for c in value)):
            return value


class Coa:
    def __init__(self, prefix: str, region: str, env: str, pool: str | None):
        session = boto3.Session(region_name=region)
        self.prefix, self.env = prefix, env
        self.ssm = session.client("ssm")
        self.cognito = session.client("cognito-idp")
        self.cfn = session.client("cloudformation")
        self.pool = pool or self.ssm.get_parameter(Name=f"/{prefix}/userpool-id")["Parameter"]["Value"]
        self.param = param_name(prefix)

    # ── SSM ────────────────────────────────────────────────────────────────
    def stored(self) -> dict | None:
        try:
            parameter = self.ssm.get_parameter(Name=self.param, WithDecryption=True)["Parameter"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ParameterNotFound":
                return None
            raise
        if parameter.get("Type") != "SecureString":
            sys.exit(f"{self.param} exists but is a {parameter.get('Type')}, not a SecureString")
        return json.loads(parameter["Value"])

    def store(self, value: dict, existing: dict | None) -> bool:
        if existing == value:
            return False
        self.ssm.put_parameter(
            Name=self.param, Type="SecureString", Value=json.dumps(value), Overwrite=True,
            Description="Launchpad shared COA demo account (samples/coa-demo/demo_account.py)",
        )
        self.ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=self.param, Tags=TAGS)
        return True

    # ── Cognito ────────────────────────────────────────────────────────────
    def user(self) -> dict | None:
        try:
            return self.cognito.admin_get_user(UserPoolId=self.pool, Username=USERNAME)
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "UserNotFoundException":
                return None
            raise

    @staticmethod
    def attr(user: dict, name: str) -> str:
        return next((a["Value"] for a in user.get("UserAttributes", []) if a["Name"] == name), "")

    def create_user(self) -> dict:
        self.cognito.admin_create_user(
            UserPoolId=self.pool, Username=USERNAME, MessageAction="SUPPRESS",
            # email left unverified: with no verified channel, "forgot password" cannot
            # reset the shared account
            UserAttributes=[{"Name": "email", "Value": USERNAME},
                            {"Name": "email_verified", "Value": "false"},
                            {"Name": "name", "Value": DISPLAY_NAME}],
        )
        return self.require_user()

    def require_user(self) -> dict:
        user = self.user()
        if user is None:
            sys.exit(f"{USERNAME} is missing — run ensure")
        return user

    def ensure_group(self) -> bool:
        try:
            self.cognito.create_group(UserPoolId=self.pool, GroupName=GROUP, Description=GROUP_DESCRIPTION)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "GroupExistsException":
                return False
            raise

    def in_group(self) -> bool:
        groups = self.cognito.admin_list_groups_for_user(UserPoolId=self.pool, Username=USERNAME)
        return any(g["GroupName"] == GROUP for g in groups.get("Groups", []))

    def set_password(self, password: str) -> None:
        self.cognito.admin_set_user_password(
            UserPoolId=self.pool, Username=USERNAME, Password=password, Permanent=True
        )

    # ── COA REST ───────────────────────────────────────────────────────────
    def api_endpoint(self) -> str:
        stack = self.cfn.describe_stacks(StackName=f"{self.prefix}-{self.env}-api")["Stacks"][0]
        for output in stack.get("Outputs", []):
            if output["OutputKey"] == "ApiEndpoint":
                return output["OutputValue"].rstrip("/")
        sys.exit(f"stack {self.prefix}-{self.env}-api has no ApiEndpoint output")


def cmd_status(coa: Coa, _args) -> int:
    user = coa.user()
    stored = coa.stored()
    ok = True
    if user is None:
        print(f"user      {USERNAME}: MISSING")
        ok = False
    else:
        verified = coa.attr(user, "email_verified")
        print(f"user      {USERNAME}: {user['UserStatus']}, enabled={user['Enabled']}, "
              f"email_verified={verified or 'false'}, sub={coa.attr(user, 'sub')}")
        ok &= user["UserStatus"] == "CONFIRMED" and user["Enabled"] and verified != "true"
        member = coa.in_group()
        print(f"group     {GROUP}: {'member' if member else 'NOT a member'}")
        ok &= member
    if stored is None:
        print(f"parameter {coa.param}: MISSING")
        ok = False
    else:
        keys = sorted(stored)
        matches = user is not None and stored.get("sub") == coa.attr(user, "sub")
        print(f"parameter {coa.param}: SecureString, keys={keys}, "
              f"username={'ok' if stored.get('username') == USERNAME else 'MISMATCH'}, "
              f"sub={'ok' if matches else 'MISMATCH'}")
        ok &= stored.get("username") == USERNAME and matches and bool(stored.get("password"))
    print("status: OK" if ok else "status: NEEDS ensure")
    return 0 if ok else 1


def cmd_ensure(coa: Coa, _args) -> int:
    changes = []
    stored = coa.stored()
    user = coa.user()
    if user is None:
        user = coa.create_user()
        changes.append("created the user")
    elif coa.attr(user, "email_verified") == "true":
        coa.cognito.admin_update_user_attributes(
            UserPoolId=coa.pool, Username=USERNAME,
            UserAttributes=[{"Name": "email_verified", "Value": "false"}],
        )
        changes.append("unverified the email")
    if not user["Enabled"]:
        coa.cognito.admin_enable_user(UserPoolId=coa.pool, Username=USERNAME)
        changes.append("re-enabled the user")
    password = (stored or {}).get("password") or new_password()
    # re-applied every run: repairs a password someone changed through COA's own clients
    coa.set_password(password)
    if coa.ensure_group():
        changes.append(f"created group {GROUP}")
    if not coa.in_group():
        coa.cognito.admin_add_user_to_group(UserPoolId=coa.pool, Username=USERNAME, GroupName=GROUP)
        changes.append(f"added the user to {GROUP}")
    value = {"username": USERNAME, "password": password, "sub": coa.attr(coa.require_user(), "sub")}
    if coa.store(value, stored):
        changes.append(f"wrote {coa.param}")
    print("ensure: " + ("; ".join(changes) if changes else "nothing to change (password re-applied)"))
    return 0


def cmd_rotate(coa: Coa, args) -> int:
    stored = coa.stored()
    user = coa.user()
    if user is None or stored is None:
        sys.exit("run ensure first")
    password = new_password()
    coa.set_password(password)
    coa.store({**stored, "password": password}, stored)
    print(f"rotate: new password set and stored in {coa.param}")
    if args.signout:
        coa.cognito.admin_user_global_sign_out(UserPoolId=coa.pool, Username=USERNAME)
        print("rotate: signed the account out everywhere — every Launchpad demo consent must be redone")
    else:
        print("rotate: existing consents keep working until their refresh tokens expire (use --signout to end them)")
    return 0


def cmd_grant(coa: Coa, args) -> int:
    token = os.environ.get("COA_ADMIN_TOKEN", "").strip()
    if not token:
        sys.exit("COA_ADMIN_TOKEN must hold a platform-admin COA ID token")
    base = f"{coa.api_endpoint()}/namespaces/{args.namespace}/grants"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    def call(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            return exc.code, {"message": exc.read().decode()[:300]}

    status, created = call("POST", base, {"principalType": "Group", "principalId": GROUP, "role": ROLE})
    if status == 409:  # COA refuses a duplicate (principal, role) grant: already in place
        print(f"grant: Group::{GROUP} already has {ROLE} on {args.namespace}")
    elif 200 <= status < 300:
        grant = created.get("grant") or created
        print(f"grant: created {grant.get('grantId')} (Group::{GROUP} → {ROLE} on {args.namespace})")
    else:
        sys.exit(f"COA POST {base} → {status}: {created.get('message')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Shared read-only COA demo account for Launchpad.")
    parser.add_argument("command", choices=["status", "ensure", "rotate", "grant"])
    parser.add_argument("--prefix", required=True, help="COA resource prefix, e.g. coa-w2")
    parser.add_argument("--region", required=True, help="COA region, e.g. us-west-2")
    parser.add_argument("--env", default="dev", help="COA env (stack name part), default dev")
    parser.add_argument("--user-pool", default=None, help="defaults to SSM /<prefix>/userpool-id")
    parser.add_argument("--signout", action="store_true", help="rotate: end every existing session")
    parser.add_argument("--namespace", help="grant: the COA namespace id")
    args = parser.parse_args()
    if args.command == "grant" and not args.namespace:
        parser.error("grant needs --namespace")
    coa = Coa(args.prefix, args.region, args.env, args.user_pool)
    return {"status": cmd_status, "ensure": cmd_ensure, "rotate": cmd_rotate, "grant": cmd_grant}[
        args.command](coa, args)


if __name__ == "__main__":
    sys.exit(main())
