# Live Portal/AWS validation

Date: 2026-08-04

Account: `434444145045`
Region: `us-west-2`
Registry Skill: `pirate-speak`

The approved Skill requires every activated response to start with `Arrr!`.

## Browser payload and layout

- Portal route: `http://127.0.0.1:5173/create`
- Browser plugin unavailable; used repository `playwright-cli` with Chromium.
- The zip create form displayed `SKILLS - REGISTRY & CUSTOM`.
- Selecting A2A retained the mounted `pirate-speak` chip and separately showed
  AgentCard skills `calculator` and `current time`.
- Mocked POST payload contained both independent fields:
  - `skills: ["s3://.../skills/pirate-speak/"]`
  - `a2a_skills: [calculator, current-time]`
- Desktop 1280x720: no overlap or clipping in the changed form.
- At 390x844 the existing fixed application sidebar still crops the main
  workspace; the new Skill chips themselves wrap correctly.
- Console after a clean reload: zero application errors; two existing React
  Router future-flag warnings. The favicon 404 appeared only on initial load.
- Screenshot evidence stayed outside the repository:
  - `/tmp/zip-skill-a2a-desktop.png`
  - `/tmp/zip-skill-a2a-mobile.png`

## HTTP zip runtime

Agent: `zip-skill-http-0804`
Ledger id: `a95f387d597542aeba378841eac89c0c`
Job: `7b8f88e940014f2daa622954cfe9a8fe`

Evidence:

- Portal create request persisted protocol `http` and the selected S3 prefix.
- Package log: `skills bundled: pirate-speak (1 files, 0.2 KB)`.
- Package detail: `37.5MB ... skills: pirate-speak`.
- Runtime reached READY.
- Chat prompt: `Use the pirate-speak skill. Say hello in one short sentence.`
- Response: `Arrr! Ahoy there, matey!`
- Chat reported `memory.create_event - turn saved to short-term`.

## A2A packaging failure and resolver fix

First agent: `zip-skill-a2a-0804`
Ledger id: `694a80f147fb4ce298e21d25be85f488`

The first package failed before AWS resource creation because the resolver
locked sdist-only `greenlet==3.5.4` while pip installs ARM64 manylinux2014
wheels only. Recompiling the identical input with
`uv pip compile --only-binary=:all:` selected wheel-backed
`greenlet==3.2.5`. The shared lock command now applies that same constraint.

## A2A runtime

Agent: `zip-skill-a2a-bin-0804`
Ledger id: `7391640bf3a947059281dd22bd176e8c`
Runtime id: `zip_skill_a2a_bin_0804_515ca7-kcukOCGk7E`
Create job: `df3fe3ff933f416487fc821fa0f662b7`
Re-publish job: `7c0cd6789a7d46f28b069200099660c6`

Packaging/AWS evidence:

- Package log: `skills bundled: pirate-speak (1 files, 0.2 KB)`.
- Package detail: `46.0MB ... skills: pirate-speak`.
- Downloaded artifact contained:
  - `skills/pirate-speak/SKILL.md`
  - `requirements.lock` with `greenlet==3.2.5`
- AWS `GetAgentRuntime`: READY, version 1, `serverProtocol: A2A`.
- Registry record `N60F7daHs5nc` advertised only AgentCard skills
  `calculator` and `current-time`; mounted `pirate-speak` was absent.

The first version's invocation returned AgentCore 424. CloudWatch proved
current Strands calls `agent_factory("__agent_card__")` while constructing
`A2AServer`; the template tried to use that invalid Memory session id and
exited. The template now skips Memory for invalid/internal context ids while
retaining it for real platform context ids.

Portal Edit restored protocol A2A, mounted `pirate-speak`, and both AgentCard
skills. Re-publish updated the same Runtime to version 2.

Invocation evidence:

- Prompt: `Use the pirate-speak skill. Say hello in one short sentence.`
- Response: `Arrr! Ahoy there, matey!`
- Chat reported `memory.create_event - turn saved to short-term`.
- This proves A2A JSON-RPC, mounted Skill activation, and real-context Memory
  all work after the card-context guard.

## Harness conversion compatibility

Source Harness: `lab-fund-advisor`
Ledger id: `26f7707c0d964f988360e6a5b4f161e1`
Harness id: `lab_fund_advisor-9IoJvol1OL`

Before conversion:

- AWS status READY, version 3.
- ARN:
  `arn:aws:bedrock-agentcore:us-west-2:434444145045:harness/lab_fund_advisor-9IoJvol1OL`
- Skill:
  `s3://launchpad-artifacts-434444145045-us-west-2/skills/lab-fund-disclaimer/`
- Ledger `updated_at`: `2026-07-26 13:51:44.863254`.

Portal conversion created the independent `lab-fund-advisor-rt-2`:

- Ledger id: `a85936f67594479a9217191006347440`.
- Runtime id: `lab_fund_advisor_rt_2_334286-jjVonh2gSc`.
- Job id: `eb8776b0673a4a9ca3424e81844b10a5`.
- The converted spec carried the same Skill S3 prefix and the exported
  `skills/fetcher.py`.
- Package log staged 11 bundle files but contained no `skills bundled` event.
- The artifact contained `skills/fetcher.py` and no
  `skills/lab-fund-disclaimer/SKILL.md` snapshot.
- Generated `main.py` called `resolve_s3_skills` at request time and passed the
  resulting paths to `AgentSkills`.
- Chat returned the exact required statement:
  `声明：以上信息摘自基金产品资料，仅供专业投资者参考，过往业绩不代表未来表现。`

After conversion, AWS and ledger source values were byte-for-byte unchanged:
READY, version 3, same ARN, same Skill URI, and the same `updated_at`. This
proves conversion did not mutate or detach the original Harness Skill.

## Verification and cleanup

- `make verify`: PASS after the binary-only resolver change.
- Focused A2A tests: 26 passed after the Memory card-context fix.
- Follow-up lock probes: both HTTP and A2A base requirements resolved for
  Python 3.13 ARM64 with `--only-binary=:all:`; A2A selected
  `greenlet==3.2.5`.
- Follow-up regression: 184 focused backend tests passed across zip packaging,
  requirement pinning, A2A, Harness conversion, and the HTTP template.
- Final `make verify`: PASS (1,548 backend tests, 11 infra tests, frontend
  ESLint/TypeScript/Vite build, and i18n parity).
- Launchpad delete returned `aws_resource_deleted=true` for:
  - HTTP validation agent `a95f387d597542aeba378841eac89c0c`
  - failed A2A ledger agent `694a80f147fb4ce298e21d25be85f488`
  - successful A2A agent `7391640bf3a947059281dd22bd176e8c`
  - converted Runtime agent `a85936f67594479a9217191006347440`
- AWS `GetAgentRuntime` returned `ResourceNotFound` for all temporary Runtime
  ids; temporary IAM roles and S3 artifact prefixes were removed.
- Temporary Registry records `cDM8rnFjSXBK`, `N60F7daHs5nc`, and
  `ZKWXDvZMdD9X` were deleted and verified `ResourceNotFound`.
- The source `lab-fund-disclaimer` Registry record remained APPROVED with its
  original `updatedAt`.
- Source Harness `lab-fund-advisor` and Registry Skills were deliberately kept.
