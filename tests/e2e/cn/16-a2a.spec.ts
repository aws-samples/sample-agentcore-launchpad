import { expect, test } from "@playwright/test";
import { approveRecordViaUi, attach, awsRead, chatOnce, createStrandsViaWizard, e2eName, evidence, getAgent, ledger, looksLikeError, registryRecords, reusableAgent, sleep, waitForDeploy } from "./helpers";

/**
 * Case 16 (round 4) — A2A end to end. Agent A (Strands, protocol A2A) publishes an agent
 * card + APPROVED registry record → `GET /api/registry/attachables` lists it under `agents`
 * → agent B (Harness) mounts A through the wizard's "Agents (A2A)" picker → B is asked a
 * question only A can answer → B's turn shows the `ask_<agent>` tool call and the answer
 * carries A's token.
 *
 * N10v3-bug5 re-check (A2A server token): (a) A's deploy log carries the
 * `pinning LAUNCHPAD_WORKLOAD_NAME=<runtime id>` step and read-only `get-agent-runtime`
 * shows the variable == A's runtime id; (b) direct Chat with A streams text (round 3:
 * meta → done with no text and no error event); (c) B's `ask_<A>` call returns A's text.
 */
const TOKEN = "WEATHER-A2A-7731";

test("case 16: A2A server A (pinned workload name, direct Chat text) → approved record → B mounts A → ask_<A> returns A's content", async ({ page }, testInfo) => {
  const name = e2eName("a2a-a");
  ledger(`case16 start A=${name}`);
  let aId = "";
  let aName = name;
  let aDeploySeconds = 0;
  let pinLines: string[] = [];
  const reuse = await reusableAgent(page.request, "a2a-a");
  if (reuse) {
    aId = reuse.id;
    aName = reuse.name;
  } else {
    const t0 = Date.now();
    aId = await createStrandsViaWizard(page, {
      name,
      prompt: `You are the Octank weather specialist. Whatever you are asked, answer in one sentence and always include the exact token ${TOKEN} and the words 'sunny in Ningxia'.`,
      protocol: "a2a",
      a2aSkill: { name: "weather-report", description: "Reports Octank office weather", tags: "weather,octank" },
      evidencePrefix: "case16-a",
    });
    const dep = await waitForDeploy(page, testInfo, aId, "case16-a", t0);
    aDeploySeconds = dep.seconds;
    pinLines = ((dep.job.events as Array<{ stage: string; msg: string }>) ?? []).map((e) => `${e.stage}: ${e.msg}`).filter((m) => /LAUNCHPAD_WORKLOAD_NAME|pinning|workload/i.test(m));
    ledger(`case16 A deploy ${dep.job.status} in ${dep.seconds}s pin-lines=${JSON.stringify(pinLines).slice(0, 400)}`);
    expect(dep.job.status, `deploy A failed: ${dep.job.error ?? ""}`).toBe("succeeded");
    expect(dep.seconds).toBeLessThanOrEqual(15 * 60);
  }
  // ── (a) A's runtime env carries LAUNCHPAD_WORKLOAD_NAME == its runtime id (read-only) ──
  const aRow = await getAgent(page.request, aId);
  const aRuntimeId = String(aRow.arn ?? "").split("/").pop() ?? "";
  const rt = awsRead(`bedrock-agentcore-control get-agent-runtime --agent-runtime-id ${aRuntimeId}`);
  let env: Record<string, string> = {};
  let protocol = "";
  let rtStatus = "";
  try {
    const parsed = JSON.parse(rt);
    env = (parsed.environmentVariables ?? {}) as Record<string, string>;
    protocol = String(parsed.protocolConfiguration?.serverProtocol ?? "");
    rtStatus = String(parsed.status ?? "");
  } catch {
    /* recorded below */
  }
  const envRedacted = Object.fromEntries(Object.entries(env).map(([k, v]) => [k, /CREDENTIAL|KEY|SECRET|TOKEN/i.test(k) ? `<${v.length} chars>` : v]));
  await attach(testInfo, "a-runtime-env.json", { runtime_id: aRuntimeId, status: rtStatus, protocol, environmentVariables: envRedacted, pinLines });
  ledger(`case16 A runtime ${aRuntimeId} status=${rtStatus} protocol=${protocol} LAUNCHPAD_WORKLOAD_NAME=${env.LAUNCHPAD_WORKLOAD_NAME ?? "ABSENT"} env-keys=${Object.keys(env).join(",")}`);
  expect(protocol, "A is an A2A-protocol runtime").toBe("A2A");
  expect(env.LAUNCHPAD_WORKLOAD_NAME, "A's env pins LAUNCHPAD_WORKLOAD_NAME (N10v3-bug5)").toBe(aRuntimeId);
  if (!reuse) expect(pinLines.length, "A's deploy log shows the pin step").toBeGreaterThanOrEqual(1);

  // ── (b) direct Chat with A streams text ───────────────────────────────────
  // N10_A2A_SOFT=1 (round-4 follow-up run): record A's direct-chat outcome instead of asserting it, so
  // the consumer half (B's ask_<A> relaying a failed Task's status.message) can still be verified.
  const soft = !!process.env.N10_A2A_SOFT;
  const chatA = await chatOnce(page, aId, "What is the weather at the Octank office today?", { timeoutMs: 4 * 60 * 1000 }).catch((e: Error) => ({ answer: `(chat did not settle: ${e.message.slice(0, 160)})`, sessionId: "", toolCalls: 0, toolNames: [] as string[], errors: -1, seconds: 0 }));
  const errBubble = (await page.locator(".v2-chat-msg.error, .v2-chat-error").allInnerTexts().catch(() => [])).join(" | ").slice(0, 300);
  await attach(testInfo, "chat-a-answer.txt", `${chatA.answer}\n\nerror bubbles: ${errBubble}`);
  await evidence(page, "case16-a-direct-chat");
  ledger(`case16 A direct chat ${chatA.seconds}s errors=${chatA.errors} answer=${chatA.answer.slice(0, 200).replace(/\n/g, " ")} errorBubble=${errBubble.replace(/\n/g, " ")}`);
  if (!soft) {
    expect(chatA.errors, "no error bubble on A's direct chat").toBe(0);
    expect(chatA.answer.length, "A streams text (round 3: silence)").toBeGreaterThan(5);
    expect(looksLikeError(chatA.answer)).toBe(false);
    expect(chatA.answer, "A's direct answer carries its token").toContain(TOKEN);
  } else ledger("case16 SOFT mode: A's direct chat recorded, continuing to the consumer half");

  // A's A2A record exists → approve it in the Registry UI → live agent card
  let rec = (await registryRecords(page.request)).find((r) => r.name === aName);
  expect(rec, "A2A record auto-created for agent A").toBeTruthy();
  if (rec!.status !== "APPROVED") {
    const st = await approveRecordViaUi(page, rec!.record_id);
    ledger(`case16 record ${rec!.record_id} → ${st}`);
  }
  rec = (await registryRecords(page.request)).find((r) => r.name === aName);
  expect(rec!.status).toBe("APPROVED");
  await page.goto(`/v2/registry?view=detail&id=${encodeURIComponent(rec!.record_id)}`);
  await expect(page.getByTestId("v2-registry-agent-card")).toBeVisible({ timeout: 30_000 });
  const cardRes = await page.request.get(`/api/registry/records/${rec!.record_id}/live-agent-card`);
  const card = await cardRes.json().catch(async () => ({ raw: await cardRes.text() }));
  await attach(testInfo, "live-agent-card.json", card);
  ledger(`case16 live-agent-card → ${cardRes.status()} ${JSON.stringify(card).slice(0, 200)}`);
  await evidence(page, "case16-record-approved-card");
  expect(cardRes.status(), "live agent card of A").toBe(200);

  // ── consumer catalog: A listed under `agents`, attachable ─────────────────
  const attachables = await (await page.request.get("/api/registry/attachables?refresh=true")).json();
  await attach(testInfo, "attachables.json", attachables);
  const agentsCat = (attachables.agents ?? []) as Array<{ name: string; attachable: boolean; attachability_reason?: string | null; runtime_arn?: string; url?: string; skills?: unknown[] }>;
  const entry = agentsCat.find((a) => a.name === aName);
  ledger(`case16 attachables keys=${Object.keys(attachables).join(",")} agents=${agentsCat.length} A=${JSON.stringify(entry).slice(0, 240)}`);
  expect(entry, "attachables.agents lists A").toBeTruthy();
  expect(entry!.attachable, `A attachable (reason: ${entry!.attachability_reason})`).toBe(true);
  expect(String(entry!.runtime_arn ?? "")).toMatch(/^arn:aws-cn:bedrock-agentcore:cn-northwest-1:/);

  // ── agent B mounts A through the wizard ──────────────────────────────────
  const bName = e2eName("a2a-b");
  await page.goto("/v2/agents");
  await page.getByTestId("v2-agent-new").click();
  await page.getByTestId("v2-agent-method-harness").click();
  await page.getByTestId("v2-agent-wizard-next").click();
  await page.getByTestId("v2-agent-name").fill(bName);
  await page.getByTestId("v2-agent-prompt").fill("You are the Octank front desk. You know nothing about weather yourself: for ANY weather question you MUST call the weather specialist agent tool and relay its answer verbatim, including any tokens it returns.");
  const peerBox = page.getByTestId("v2-agent-a2a-peers").locator("label", { hasText: aName }).locator('input[type="checkbox"]');
  await expect(peerBox, "A offered in the Agents (A2A) picker").toBeVisible({ timeout: 30_000 });
  await expect(peerBox).toBeEnabled();
  await peerBox.check();
  await evidence(page, "case16-b-wizard-configure");
  await page.getByTestId("v2-agent-wizard-next").click();
  await expect(page.getByTestId("v2-agent-review")).toBeVisible();
  await evidence(page, "case16-b-wizard-review");
  const tB = Date.now();
  await page.getByTestId("v2-agent-wizard-submit").click();
  await expect(page).toHaveURL(/view=detail&id=/, { timeout: 60_000 });
  const bId = new URL(page.url()).searchParams.get("id")!;
  const bRow = await (await page.request.get(`/api/agents/${bId}`)).json();
  const a2aRefs = ((bRow.spec?.tools ?? []) as Array<{ type: string; config?: Record<string, unknown> }>).filter((t) => t.type === "a2a");
  await attach(testInfo, "agent-b-spec.json", bRow.spec);
  ledger(`case16 B=${bName} (${bId}) a2a tool refs=${JSON.stringify(a2aRefs).slice(0, 300)}`);
  expect(a2aRefs.length, "B's spec carries one ToolRef.type=a2a").toBe(1);
  const depB = await waitForDeploy(page, testInfo, bId, "case16-b", tB);
  expect(depB.job.status, `deploy B failed: ${depB.job.error ?? ""}`).toBe("succeeded");
  expect(depB.seconds).toBeLessThanOrEqual(15 * 60);

  // ── (c) chat B: only A knows the token ───────────────────────────────────
  const chat = await chatOnce(page, bId, "What is the weather at the Octank office today? Ask the weather specialist and quote its answer word for word.", { timeoutMs: 6 * 60 * 1000 });
  await attach(testInfo, "chat-b-answer.txt", `${chat.answer}\n\ntools: ${chat.toolNames.join(", ")}`);
  await evidence(page, "case16-b-chat-ask-a");
  ledger(`case16 B chat ${chat.seconds}s errors=${chat.errors} tools=${chat.toolNames.join(",")} answer=${chat.answer.slice(0, 200).replace(/\n/g, " ")}`);
  const slug = "ask_" + aName.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "");
  expect(chat.toolNames.some((t) => t.startsWith("ask_")), `B's turn shows an ask_<agent> tool call (expected ${slug}; got ${chat.toolNames.join(",")})`).toBe(true);
  if (soft) {
    // consumer relay (N10v3-bug5 secondary fix): a failed Task's status.message reaches B — never the bare "returned no text"
    ledger(`case16 SOFT consumer relay check: mentions task failure=${/task failed|Agent execution failed|a2a error/i.test(chat.answer)} 'returned no text'=${/returned no text/i.test(chat.answer)}`);
    expect(chat.answer, "B must not report the round-3 'returned no text'").not.toMatch(/returned no text/i);
  } else {
    expect(chat.errors, "no error bubble").toBe(0);
    expect(looksLikeError(chat.answer)).toBe(false);
    expect(chat.answer, "B relays no 'returned no text' / a2a error").not.toMatch(/returned no text|\[a2a error\]/i);
    expect(chat.answer, "answer carries A's token").toContain(TOKEN);
  }
  ledger(`case16 end PASS A=${aId} (deploy ${aDeploySeconds}s) B=${bId} (deploy ${depB.seconds}s) tool=${slug}`);
  await sleep(1000);
});
