# 无人机门店客服助手 demo — brand-neutral copy of samples/dji-demo (for prod)

Same data/tools as dji-demo, every brand string removed (target `store-crm`, tables `store-demo-*`,
Lambda `launchpad-store-crm` + role `launchpad-store-crm-lambda`, inline `store-crm-invoke` on the gateway
role, Cedar `store_crm_demo_tools`, category/service 随心换, product names without the brand prefix).
Same scripts: `store-crm-api/provision.py <region> [--reseed]`, `register_record.py <region> <md>`,
`turn.py`, `chat.py` (copied to the prod box under samples/store-demo/).

## prod (us-east-1), 2026-10-08
target `GMH8PRNZKP` on launchpad-gw-0gqcfd9q8a (ENFORCE engine launchpad_gw_policy-_9zuu420j0);
permit `store_crm_demo_tools-ba1xlyohdd`; Registry record `mnRejoZPrYrl` APPROVED; data reseeded.
Remaining brand string the architect can see: KB UWU7WZGGIB name/description "DJI-drones-user-manual".
