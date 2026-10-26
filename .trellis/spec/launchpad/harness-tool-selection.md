# Explicit Harness tool selection

## Contract

Ordinary AgentSpec and architect ProposalContent expose native_tools as a unique,
bounded list of shell/file_operations, default empty. Nonempty native selections
are Harness-only. Native tools never become fake ToolConfig attachments.

allowed_tools=None now means derive selectors from final resolved ToolConfigs,
Skill loading and native choices; always send allowedTools, including [] for an
empty selection. Explicit expert patterns remain overrides, including a deliberate
'*'; existing KB support behavior is preserved. The editor displays existing
overrides and requires an explicit reset before selected-mode checkboxes control
access. Presets retain their server-owned explicit patterns.

The pure app/harness_tool_access.py helpers are shared by deployment and request
overrides. Derive exact @group selectors after Gateway alias resolution. Automatic
selection must not produce reserved @builtin or wildcard group access from a name.
User-authenticated Gateway calls preserve live configuration/auth, map renamed
groups without broadening suffixes, and use a matching invocation allowedTools.

## Review and evaluation

Proposals cannot provide arbitrary allowed_tools patterns. native_tools is visible
in review, copied to AgentSpec and covered by bindings/hash. The server-owned
resources.tool_access_policy=selected-v1 marks the new meaning. Historical rows are
not rewritten; changed pending bindings require review.

Catalog availability does not grant a native capability. Evaluation allowlists and
positive native-tool expectations must match native selections or explicit runtime
overrides. Named prohibitions remain valid. Main Skill loading is implicit; access
to extra files/scripts may require explicitly reviewed native tools.

## Evidence and checks

Live per-invocation tests in prod (us-east-1) used identical harmless printf requests:
[] exposed no shell; ['shell'] executed it; ['skills','@aws-knowledge',
'@launchpad_kb_gw'] exposed no shell. A business invocation under the latter list
successfully called skills, the selected KB Retrieve, and AWS documentation search.
No persistent Agent configuration changed during those probes.

Cover default/empty selections, native opt-in, explicit override retention, Gateway
alias remapping/collisions, approval binding changes, unselected-native evaluation
rejection, non-Harness exclusion, and frontend editing/legacy snapshots. Run full
make verify and verify the persisted live DEFAULT endpoint after deployment.
