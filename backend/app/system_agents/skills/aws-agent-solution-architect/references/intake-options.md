# Interactive requirement clarification — option library

Do not send the whole questionnaire as one wall of text. Ask in two compact rounds of
3–4 numbered questions; each question offers default options plus a free-text answer.
Tell the customer both are acceptable.

## New-conversation boundary (mandatory)

Every new conversation starts from a fresh baseline. Only what the customer typed or
re-confirmed in this conversation counts as answered; earlier conversations, persistent
memory, old documents, old design contracts and old test sets never prefill answers.
Even for an identical project name, rounds one, two and three are completed again here.

Fields covered by the customer's opening message count as answered; ask only what is
still open and would change architecture or cost. Usually two rounds of 3–4 questions,
at most six options each. Mark multi-select questions. Option notes explain the impact,
not just restate the label.

## Round one: business scope and access

### 1. Initial scope (multi-select)
Which capabilities should the first release cover?
- Knowledge Q&A — answers from policies, manuals, FAQs or business documents
- Personalized lookups — reads personal or business data under the current user's permissions
- Business actions — creates tickets, submits requests, updates records or calls system actions
- Approval collaboration — starts, queries or advances human-approved workflows
- Analytics and reporting — aggregates data, produces insights or periodic reports
- Not sure yet — design for "knowledge Q&A + low-risk lookups"

### 2. User scale (single-select)
What is the expected first-release scale and peak?
- PoC / pilot — fewer than 100 users, feasibility first
- Small — 100–1,000 users, roughly 100–1,000 requests per day
- Medium — 1,000–10,000 users, roughly 1,000–10,000 requests per day
- Large — more than 10,000 users or pronounced peak concurrency
- Not sure yet — design for medium with horizontal scaling

### 3. Access channels (multi-select)
Where will users reach the agent?
- Enterprise chat platforms — WeCom, DingTalk, Feishu, Teams, Slack bots
- Internal web portal — browser-based enterprise application
- Mobile app — embedded in an existing iOS / Android app
- API — called by existing business systems
- Not sure yet — design web + API, decoupled

### 4. Data sources and existing systems (multi-select)
Which systems must the agent read or operate?
- Document knowledge — PDF, Word, S3, Confluence, SharePoint
- Core business systems — HRIS, ERP, CRM, finance or industry systems
- Workflow / ticketing — approvals, service desk, task systems
- Databases / data lake — RDS, DynamoDB, Redshift, data warehouse
- External SaaS / APIs — third-party services or internet APIs
- Not sure yet — design for document knowledge, reserve tool interfaces

## Round two: deployment, compliance and delivery

### 5. Deployment and data residency (single-select)
Which infrastructure and data-residency constraints apply?
- AWS global Regions — full Bedrock / AgentCore capability set, to be verified per Region
- AWS China Regions — data stays in China; service capabilities verified for those Regions
- Existing data center / other cloud — integration with on-premises or other-cloud systems
- Hybrid — dedicated line, VPN or private connectivity between AWS and the data center
- Not sure yet — describe the global-Region and China-Region differences

### 6. Data and compliance (multi-select)
Which data classes or compliance requirements apply?
- Internal general data — no sensitive personal information
- Personal information / PII — names, contacts, employee ids; minimization and masking
- Sensitive personal information — salary, identity documents, health, performance; strict authorization and audit
- Regulated data — finance, healthcare, government or industry regulation
- Data residency — storage, processing and logs are Region-restricted
- Not sure yet — design to the PII standard and list open items

### 7. Project constraints (single-select)
Which delivery rhythm is closest to the plan?
- Fast PoC — validate within 4 weeks, cost first, strictly narrowed scope
- Balanced pilot — 1–3 months; effectiveness, governance and scalability
- Production project — 3–6 months; security review, evaluation system, operations handover
- Quality first — relaxed time and budget; quality and full capability first
- Not sure yet — propose a 1–3 month balanced pilot roadmap

## Round three: current state and pain points (mandatory, never skipped)

After rounds one and two, read `painpoint-workflow.md`. Confirm whether a production
agent, a pilot or a legacy service process exists, and collect real obstacles, failure
cases and evidence sources. The customer may pick default pain-point categories and add
concrete cases in free text.

After this round, generate the `pain point → metric → golden test` conversion table and
ask for confirmation. No final architecture or formal document before confirmation. With
no live agent, still generate an industry-assumption test set marked
`industry_assumption` and get it confirmed.

## Interaction rules

1. Each round starts with one line saying options can be picked or a custom answer typed.
2. "Not sure yet" takes the default and is logged as an assumption pending validation.
3. Never re-ask what the customer already answered in this conversation; answers from
   earlier conversations, memory or old documents do not count. Regroup the remaining
   baseline questions into at most two rounds.
4. After rounds one and two, always proceed to round three; never generate the design
   directly.
5. A free-text answer wins; if a default option was also picked, merge the two.
6. The conversion table is confirmed by the customer, or the customer explicitly chooses
   industry assumptions, before design starts.
