"""Display one owned workshop account credential, only when explicitly requested."""

import argparse

from lab import load, secret

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("username")
args = parser.parse_args()
user = load()["users"].get(args.username)
if not user:
    raise SystemExit("Account is not recorded as owned by this workshop")
print(args.username)
print(secret(user["credential_parameter"]))
