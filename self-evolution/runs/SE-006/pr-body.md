## Summary

Six forms gated their primary action on a boolean and simply dimmed the button — Registry Register, Registry Edit, Knowledge Base Create, Strands Studio Publish, Online Evaluation Create, Workspace `RUN BOOTSTRAP` — with no `title`, no hint and no required marks, so the user had to guess which field unlocked it.

- `components/Btn.tsx` gains `disabledReason?: string`: while `disabled` and set, the button gets `title={reason}` and `aria-describedby` pointing at a sibling `.btn-hint` (mono, helper-text weight); when enabled, no hint element exists. The reason is derived from the same predicates that compute `disabled` (first unmet one is named), so *when* buttons disable is unchanged.
- Wired on the six forms with specific, localized reasons (en + zh-CN), e.g. "Name must be 3–64 chars: lowercase letters, digits, hyphens; starts with a letter", "Add a SKILL.md", "Add at least one file to upload", "No changes to save", "Add at least one node", "Choose an agent", "Bootstrap already ran — this workspace is READY". Busy states carry no reason (the label already says what is happening).
- `docs/architecture.md` + zh-CN twin record the pattern.

## Verification

- `make verify` PASS.
- Playwright on a worktree build, six URLs × en/zh-CN: every disabled `.btn.primary` has a non-empty `title` and a visible hint whose text equals the title; typing a valid name + URL on Register enables the button and removes the hint.

Self-evolution direction SE-006 (`ux` path).
