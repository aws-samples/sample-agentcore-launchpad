# Launchpad App Guidelines (backend + frontend)

> Code-specs for the main AgentCore Launchpad app (`backend/`, `frontend/`).
> Vendor packages (lab4-interactive, strands_ui) have their own spec layers.

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Registry Skill Ingestion](./registry-skill-ingestion.md) | Multi-source skill pipeline: SkillBundle, inspect→import staging, git/url acquirers, reimport, record update (PUT) + register/edit sub-pages | Active |
| [Container Capabilities + Filesystem](./container-capabilities-filesystem.md) | Claude Agent SDK (container) method: registry MCP/skill wiring, attach-without-record skill sources (/api/agent-skills), filesystemConfigurations (session/S3 Files/EFS) + VPC + IAM inline policy | Active |
| [Claude SDK AgentCore Memory](./claude-sdk-agentcore-memory.md) | Request-local MemorySessionManager, automatic short-/long-term restore hook, exactly-once turn persistence, and shared runtime environment injection | Active |
| [Memory Console](./memory-console.md) | Read-only `/api/memory` projections: structural read-only guarantee, first-separator actor decode + batched name join, server-side `{actorId}` namespace resolution, harness message-envelope decoding (tool turns kept), and the two live-only preview bounds (`ListMemoryExtractionJobs` maxResults ≤ 50, status enum = `FAILED` only — why extraction has no console view) | Active |
| [Claude SDK Runtime Invocation](./claude-sdk-runtime-invocation.md) | Native Claude partial-event streaming, Runtime SSE normalization, sync compatibility, read timeout, and republish requirement | Active |
| [Evaluation Agent Eligibility](./evaluation-agent-eligibility.md) | Which methods are eval-supported + telemetry resolution: harness span identity (harness_{name}.DEFAULT, strands scope), backing-runtime log-group prefix discovery, InvokeHarness dispatch | Active |
| [Evaluation Cloud Dataset Runs](./evaluation-cloud-dataset-runs.md) | AWS cloud datasets + simulated personas as run scopes: ListDatasetExamples-driven execution (no AWS-side dataset source), SDK LLM-actor simulation w/ per-run actor_model_id, cloud: scope encoding, lazy GT detail endpoint | Active |
| [Managed Knowledge Bases](./managed-kb.md) | Managed KB CRUD + S3 sources + Playground; launchpad-kb-gw connector topology (per-KB Retrieve + per-agent AgenticRetrieveStream targets), harness-only attach, kb-role IAM, async create/ingest quirks | Active |
| [Experiment Stepwise Actions](./experiment-stepwise.md) | Separate Configuration A/B and Runtime Canary records/APIs, manual evidence gates, shared-Gateway mutex, resource ownership, progress polling, and legacy combined-row compatibility | Active |
| [Harness → Runtime Conversion](./harness-conversion.md) | `POST /agents/{id}/convert`: agentcore CLI export + mandatory config-bundle graft + AgentSpec.code_bundle multi-file deploy; fidelity policy (memory wired, KB gateway not), SSE flattening for streaming runtimes | Active |
| [A2A-Protocol Agents](./a2a-agents.md) | `AgentSpec.protocol=a2a` (zip only): A2AServer template + serverProtocol=A2A deploy (Update omit=RESET!), JSON-RPC invoke branch (Task artifacts, never history), real registry cards (a2a-jsonrpc transport), experiment exclusion | Active |
| [Evaluation Sub-page Interaction](./evaluation-subpage-interaction.md) | Shared table + URL-param selection for experiments/evaluators/datasets, including Configuration/Canary mode and handoff params (`exp`/`canary`/`champion`/`sourceExp`), editor rehydration, read-only variants, and testids | Active |
| [Console Authentication](./console-auth.md) | Optional local operator credentials, HMAC session-cookie contract, `/api` middleware boundary, `/v1` independence, and frontend expiry handling | Active |
| [Remote Production Deployment](./remote-production-deployment.md) | Workshop EC2 + CloudFront deployment contract: us-east-1 bootstrap, loopback services, nginx origin-key gate, CloudFront-only port 80, systemd Region override, seeding and verification | Active |
| [Existing Gateway Policy Management](./gateway-policy-management.md) | Live Gateway onboarding tags, Gateway-level Registry records, server-derived Harness auth, conservative Cedar lifecycle, audit, and operation contracts | Active |

**Language**: All documentation should be written in **English**.
