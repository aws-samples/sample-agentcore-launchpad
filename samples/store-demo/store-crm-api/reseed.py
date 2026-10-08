"""Reset the Store CRM demo data only (no Lambda / IAM / Gateway changes).

  cd backend && uv run python ../samples/store-demo/store-crm-api/reseed.py us-east-1

Deletes every row of the four `store-demo-*` tables (incl. tickets created by demo runs) and
reloads seed.json. Run it before every evaluation run or canary replay so each round starts
from the same CRM state (TK-8020 = 待处理 / 高).
"""

import json
import pathlib
import sys
from decimal import Decimal

import boto3

HERE = pathlib.Path(__file__).resolve().parent
PREFIX = "store-demo-"
TABLES = {"customers": ["customer_id"], "purchases": ["customer_id", "order_id"],
          "tickets": ["ticket_id"], "products": ["sku"]}

region = sys.argv[1]
seed = json.loads((HERE / "seed.json").read_text(), parse_float=Decimal)
ddb = boto3.Session(region_name=region).resource("dynamodb")
for name, keys in TABLES.items():
    table = ddb.Table(PREFIX + name)
    items, kwargs = [], {"ProjectionExpression": ", ".join(keys)}
    while True:
        page = table.scan(**kwargs)
        items += page["Items"]
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    with table.batch_writer() as bw:
        for item in items:
            bw.delete_item(Key={k: item[k] for k in keys})
    with table.batch_writer() as bw:
        for row in seed[name]:
            bw.put_item(Item={k: v for k, v in row.items() if v is not None})
    print(f"{name}: deleted {len(items)}, loaded {len(seed[name])}")
ticket = ddb.Table(PREFIX + "tickets").get_item(Key={"ticket_id": "TK-8020"})["Item"]
print("TK-8020", ticket.get("status"), ticket.get("priority"))
