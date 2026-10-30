"""Roll the installed synthetic service and Gateway Lambda to this package's code and the
configured service-desk accounts. Idempotent: nothing restarts when nothing changed."""

import base64
import hashlib
import io
import shlex
import zipfile

from lab import (
    AWS,
    CONFIG,
    CONFIG_DIGEST,
    NAME,
    ROOT,
    SCENARIO,
    grant_publish_targets,
    load,
    merge_save,
    publish_targets,
    remote,
    staff_environment,
)

m = load()
if not m.get("service_installed") or not m.get("kb") or not m["kb"].get("data_sources"):
    raise RuntimeError("Run the tools stage first; the service and KB must exist")
service = m["service"]
# An environment installed under another naming scheme records its real unit and function in
# the manifest; prefer those over the derived NAME so a refresh rolls the live service.
UNIT = str(service.get("unit") or NAME + ".service").removesuffix(".service")
FUNCTION = str(m.get("lambda") or NAME).rsplit(":", 1)[-1]
expected_path = "/srv/launchpad-demo/scenarios/" + SCENARIO
if service["host"] != CONFIG["tools_host"] or service["path"] != expected_path:
    raise SystemExit("Service ownership does not match this run")
ds = m["kb"]["data_sources"][0]
prefix = ds["prefix"].strip("/")
shared = f"s3://{ds['bucket']}/" + (prefix + "/" if prefix else "")
env = staff_environment(shared)
# N17-obs525: a per-staff publish target is only writable once BOTH the Lambda role's inline
# policy and the S3 Gateway VPC endpoint policy name it (the Lambda reaches S3 through the
# endpoint); grant the full set before the service starts routing publishes to it.
targets = publish_targets(shared)
grant = grant_publish_targets(m, targets)
print(
    f"Publish grants: role {grant['role']} ({len(targets)} target(s)); S3 endpoint "
    + (f"{grant['endpoint']} " + ("updated" if grant["endpoint_changed"] else "unchanged")
       if grant["endpoint"] else "not in manifest, skipped"),
    flush=True,
)
env_lines = "\n".join(f"Environment='{k}={v}'" for k, v in env.items())
code_path = ROOT / "lab-assets/service.py"
code = base64.b64encode(code_path.read_bytes()).decode()
path = shlex.quote(expected_path)
dropin = f"/etc/systemd/system/{UNIT}.service.d/10-staff.conf"
user = CONFIG["tools_user"]
result = remote(
    f"""set -eu
test "$(cat {path}/.scenario)" = {shlex.quote(SCENARIO)}
test "$(cat {path}/.run)" = {shlex.quote(CONFIG_DIGEST)}
install -d -m 755 /etc/systemd/system/{UNIT}.service.d
cat > {dropin}.new <<'DROPIN'
[Service]
{env_lines}
DROPIN
printf '%s' '{code}' | base64 -d > {path}/service.py.new
CHANGED=0
cmp -s {path}/service.py.new {path}/service.py || CHANGED=1
cmp -s {dropin}.new {dropin} 2>/dev/null || CHANGED=1
if [ "$CHANGED" = 1 ]; then
  install -m 640 -o {user} -g {user} {path}/service.py.new {path}/service.py
  mv {dropin}.new {dropin}
  systemctl daemon-reload
  systemctl restart {UNIT}.service
  sleep 2
fi
rm -f {path}/service.py.new {dropin}.new
systemctl is-active {UNIT}.service
curl -fsS http://127.0.0.1:{CONFIG["tools_port"]}/health
echo
echo "changed=$CHANGED"
""",
    "service-refresh",
)
print(result["StandardOutputContent"].strip().splitlines()[-2], flush=True)
m["service"] = dict(
    service,
    staff_environment=env,
    publish_targets=targets,
    service_sha256=hashlib.sha256(code_path.read_bytes()).hexdigest(),
)
merge_save(m, ("service",))

if m.get("lambda"):
    bridge = (ROOT / "lab-assets/lambda_bridge.py").read_text()
    digest = hashlib.sha256(bridge.encode()).hexdigest()
    if m.get("lambda_code_sha256") != digest:
        lam = AWS.client("lambda")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("lambda_function.py", bridge)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        lam.update_function_code(FunctionName=FUNCTION, ZipFile=buf.getvalue())
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION)
        m["lambda_code_sha256"] = digest
        merge_save(m, ("lambda_code_sha256",))
        print("Lambda code updated", FUNCTION, flush=True)
    else:
        print("Lambda code unchanged", flush=True)
print("Service refreshed for staff accounts:", env["ITSM_STAFF_ACCOUNTS"], flush=True)
