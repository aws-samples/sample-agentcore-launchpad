"""Gateway Lambda adapter to the private persistent ITSM service."""

import json
import os
import urllib.request

import boto3


def call(path, payload):
    req = urllib.request.Request(
        os.environ["SERVICE_URL"] + path,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + os.environ["SERVICE_TOKEN"],
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def handler(event, context):
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full_name = custom.get("bedrockAgentCoreToolName", "")
    tool = full_name.split("___")[-1]
    payload = {"tool": tool, "arguments": event}
    result = call("/call", payload)
    if result.get("ok") and tool == "publish_article":
        article = result["result"]
        # The service names the requester's own knowledge source; absent = the shared one.
        target = article.get("publish_target")
        if target:
            bucket, _, prefix = target[len("s3://"):].partition("/")
        else:
            bucket, prefix = os.environ["KB_BUCKET"], os.environ["KB_PREFIX"]
        prefix = prefix.strip("/")
        key = (prefix + "/" if prefix else "") + article["id"] + ".md"
        text = "# " + article["title"] + "\n\n" + article["body"]
        text += "\n\n来源工单：" + "、".join(article["source_ticket_ids"]) + "\n"
        boto3.client("s3").put_object(
            Bucket=bucket,
            Key=key,
            Body=text.encode(),
            ContentType="text/markdown; charset=utf-8",
        )
        payload["arguments"] = dict(event, s3_uri="s3://" + bucket + "/" + key)
        result = call("/confirm-publish", payload)
        result["next_step"] = "在知识库数据源点击立即同步，等待 COMPLETE 后检索新文章。"
    return result
