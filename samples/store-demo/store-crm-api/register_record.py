"""Register + approve the store-crm Gateway record in the default workspace's Registry.

  cd backend && uv run python ../samples/store-demo/store-crm-api/register_record.py <region> <description.md>
"""
import pathlib
import sys

from app.services import mcp_client
from app.services.agentcore import registry as reg
from app.services.registry_console import _registry_id, registry_control_client
from app.services.workspace import default_workspace_context

ws = default_workspace_context()
assert ws.region == sys.argv[1], ws.region
desc = pathlib.Path(sys.argv[2]).read_text(encoding="utf-8").strip()
assert len(desc) <= 4096, len(desc)
tools = [
    {"name": t["name"], "description": t.get("description", ""),
     "inputSchema": t.get("inputSchema", {})}
    for t in mcp_client.tools_list(ws) if t["name"].startswith("store-crm___")
]
print("tools", [t["name"] for t in tools])
assert len(tools) == 7
client, rid = registry_control_client(ws), _registry_id(ws)
record, created = reg.upsert_record(
    client, rid, name="store-crm", description=desc, descriptor_type="MCP",
    descriptors=reg.build_mcp_descriptors(
        target="store-crm", description="无人机授权体验店 CRM（测试环境）：会员、购买记录、推荐商品、售后工单",
        gateway_url=ws.resources["gateway_url"], tools=tools),
)
print("record", record.get("recordId"), "created", created)
st = reg.wait_record_settled(client, rid, record["recordId"])
print("settled", st["status"])
reg.submit_record(client, rid, record["recordId"])
st = reg.wait_record_settled(client, rid, record["recordId"])
print("submitted", st["status"])
reg.approve_record(client, rid, record["recordId"])
print("final", reg.get_record(client, rid, record["recordId"])["status"])
