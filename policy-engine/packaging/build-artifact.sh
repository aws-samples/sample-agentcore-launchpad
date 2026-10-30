#!/usr/bin/env bash
# Build the arm64 engine artifact for the Graviton hub host (design §12.5 / §14.4 C2).
#
#   policy-engine/packaging/build-artifact.sh [out-dir]
#
# Runs where crates.io is reachable (the CN hosts never fetch crates): cross-builds with
# the pinned Cargo.lock (`--locked`), checks licences of the dependency tree, and writes
#   <out>/launchpad-policy-engine-<version>-aarch64.tar.gz  (+ .sha256)
# containing bin/launchpad-policy-engine and packaging/. Upload is a separate, explicit
# operator step (printed at the end) — this script never touches AWS.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
OUT="${1:-$ROOT/dist}"
TARGET=aarch64-unknown-linux-gnu

command -v aarch64-linux-gnu-gcc >/dev/null || {
  echo "needs gcc-aarch64-linux-gnu (apt-get install gcc-aarch64-linux-gnu)" >&2; exit 1; }
rustup target list --installed | grep -qx "$TARGET" || rustup target add "$TARGET"

cd "$ROOT"
cargo test --locked -q
cargo build --locked --release --target "$TARGET"

# licence gate: every crate in the tree must be under an allow-listed licence
cargo metadata --locked --format-version 1 | python3 -c '
import json, sys
ALLOW = {"MIT", "Apache-2.0", "MIT-0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "Unicode-3.0",
         "Unicode-DFS-2016", "Zlib", "CC0-1.0", "BSL-1.0", "MPL-2.0"}
bad = []
for p in json.load(sys.stdin)["packages"]:
    lic = (p.get("license") or "").replace("(", " ").replace(")", " ")
    terms = {t.rstrip("+") for t in lic.replace("/", " OR ").split() if t not in ("OR", "AND", "WITH")}
    if not terms or not (terms & ALLOW):
        bad.append("%s %s: %r" % (p["name"], p["version"], p.get("license")))
if bad:
    sys.exit("licence gate failed:\n  " + "\n  ".join(bad))
print("licence gate: OK")
'

VERSION="$(./target/release/launchpad-policy-engine version 2>/dev/null \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["engine"])' 2>/dev/null \
  || grep -m1 '^version' crates/lp-policy-engine/Cargo.toml | cut -d'"' -f2)"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/bin" "$STAGE/packaging" "$OUT"
cp "target/$TARGET/release/launchpad-policy-engine" "$STAGE/bin/"
aarch64-linux-gnu-strip "$STAGE/bin/launchpad-policy-engine"
cp "$HERE"/launchpad-policy-engine@.service "$HERE"/launchpad-policy-engine@.socket \
   "$HERE"/install.sh "$STAGE/packaging/"
NAME="launchpad-policy-engine-${VERSION}-aarch64.tar.gz"
tar -C "$STAGE" -czf "$OUT/$NAME" bin packaging
(cd "$OUT" && sha256sum "$NAME" > "$NAME.sha256")
echo "artifact: $OUT/$NAME"
cat "$OUT/$NAME.sha256"
echo "upload (operator, explicit): aws s3 cp $OUT/$NAME{,.sha256} s3://<artifacts-bucket>/policy-engine/ --profile <profile>"
