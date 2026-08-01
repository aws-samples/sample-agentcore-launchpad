# Implementation Plan

1. Add managed AgentCore CLI constants, installation, exact-version verification, and
   bootstrap summary output.
2. Route Harness project creation and export through the managed CLI resolver.
3. Replace the fixed `agent = get_or_create_agent(...)` text anchor with strict AST
   assignment discovery and line-based insertion.
4. Add hermetic bootstrap and conversion regression tests.
5. Update the Harness conversion spec plus English and Chinese setup documentation.
6. Run focused backend tests, Ruff, and the full `make verify` gate.
7. Review the diff for scope, archive the Trellis task, and commit only task files and
   requested implementation changes.

## Validation

- Managed npm installation: `@aws/agentcore@0.21.1` installed under
  `data/agentcore-cli`; managed resolver returned the same absolute path.
- Focused conversion/bootstrap tests: 46 passed during independent review.
- Full `make verify`: backend 999 passed, infra 9 passed, frontend lint/typecheck/build
  passed, and i18n parity passed.
