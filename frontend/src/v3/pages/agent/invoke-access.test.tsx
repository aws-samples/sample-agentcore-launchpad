import { renderToStaticMarkup } from "react-dom/server";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { type AgentInvokeAccess, policyText } from "../../../lib/api/invokeAccess";
import { InvokeAccessView } from "./InvokeAccessCard";

const ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/lab-AbC123";
const POLICY = {
  Version: "2012-10-17",
  Statement: [{ Sid: "InvokeRuntimeLab", Effect: "Allow" as const, Action: ["bedrock-agentcore:InvokeAgentRuntime"],
    Resource: [ARN, `${ARN}/runtime-endpoint/live`] }],
};

const access = (over: Partial<AgentInvokeAccess>): AgentInvokeAccess => ({
  agent_id: "a1", name: "lab", method: "zip_runtime", kind: "runtime", mode: "iam",
  account: "111122223333", region: "us-west-2", partition: "aws", arn: ARN,
  qualifiers: ["DEFAULT", "live"], available_qualifiers: ["DEFAULT", "live"],
  production_qualifier: "live", sample_qualifier: "live", include: [],
  resources: [
    { kind: "runtime", arn: ARN },
    { kind: "runtime_endpoint", qualifier: "DEFAULT", arn: `${ARN}/runtime-endpoint/DEFAULT`, production: false },
    { kind: "runtime_endpoint", qualifier: "live", arn: `${ARN}/runtime-endpoint/live`, production: true },
  ],
  policy: POLICY, jwt: null,
  samples: { cli: `aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn '${ARN}'`, python: "invoke_agent_runtime(", ts: "InvokeAgentRuntimeCommand" },
  notes: ["cross_account"],
  actions: { runtime: "bedrock-agentcore:InvokeAgentRuntime", harness: "bedrock-agentcore:InvokeHarness", harness_verified: false },
  ...over,
});

const render = (data: AgentInvokeAccess, lang: "cli" | "python" | "ts" = "cli") =>
  renderToStaticMarkup(
    <InvokeAccessView data={data} scope="all" onScope={() => undefined} include={[]} onToggle={() => undefined}
      lang={lang} onLang={() => undefined} docKey="k" onCopy={() => undefined} />,
  );

// `&amp;` last: decoding it first would turn a literal `&amp;lt;` into `<`
const unescape = (html: string) => html.replace(/&quot;/g, '"').replace(/&#x27;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("V3 'Call it from your app' card", () => {
  it("shows the backend's policy verbatim for an IAM agent, with copy and download", () => {
    const html = render(access({}));
    expect(html).toContain('data-testid="v3-invoke-access-policy"');
    const pre = /data-testid="v3-invoke-access-policy">([\s\S]*?)<\/pre>/.exec(html)?.[1] ?? "";
    expect(unescape(pre)).toBe(policyText(POLICY));
    expect(html).toContain("Download");
    expect(html).toContain("lab-invoke-policy.json");
    expect(html).toContain(`${ARN}/runtime-endpoint/live`);
    expect(html).toContain("production traffic");
    expect(html).toContain("Calling from another account");
  });

  it("explains Bearer instead of a policy for a JWT agent", () => {
    const html = render(access({
      mode: "jwt", policy: null,
      jwt: { discovery_url: "https://idp.example.com/.well-known/openid-configuration", issuer: "https://idp.example.com",
        allowed_clients: ["client-1"], allowed_audience: [], allowed_scopes: ["agent/invoke"], external: false },
      notes: ["jwt_no_iam", "cross_account"],
    }));
    expect(html).not.toContain("v3-invoke-access-policy");
    expect(html).toContain("IAM permissions have no effect");
    expect(html).toContain("https://idp.example.com");
    expect(html).toContain("client-1");
  });

  it("renders the chosen sample with the ARN filled in and flags an unverified Harness action", () => {
    const html = render(access({ kind: "harness", notes: ["cross_account", "harness_action_unverified"] }), "cli");
    expect(unescape(html)).toContain(`--agent-runtime-arn '${ARN}'`);
    expect(html).toContain("still to be verified against the service authorization reference");
    // a Harness offers no session-stop grant
    expect(html).toMatch(/<input type="checkbox" disabled=""/);
  });
});
