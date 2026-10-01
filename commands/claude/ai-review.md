---
description: Start or reuse the independently configured reviewer for a Herdr Flow task
---

Coordinate independent review for task `$ARGUMENTS` (omit the argument to use the current task).

The user's intent is: independently verify that the implementation covers the complete task handoff, misses nothing material, breaks no existing behavior, leaves no unresolved gap, and is ready to commit and merge. This is a release gate, not a summary request or a request to trust the worker's report.

1. Parse the request for an optional reviewer selection: agent kind (`opencode` or `codex`), exact model ID, and optional variant/reasoning effort. Examples: “review with OpenCode using `provider/model-id`” or “review with Codex using an exact installed model ID and high effort.” Kimi always means a model inside OpenCode; do not invoke or install Kimi Code CLI.
2. Run `herdr-flow status --task <task-id>`.
3. Require phase `IMPLEMENTATION_DONE`.
4. Run `herdr-flow review --task <task-id>`, adding `--kind`, `--model`, and `--variant` when specified. A selection can change only before the persistent reviewer starts; never replace or silently reconfigure a started reviewer.
5. Confirm the reviewer is a separate persistent session from the worker and report its kind, exact model, and variant/reasoning effort. The controller's reviewer prompt requires requirement-by-requirement handoff coverage, independent diff/worktree inspection, validation evidence, regression/security/performance/maintainability review, explicit gaps or unverified checks, actionable findings, and a commit/merge-readiness decision.
6. Treat PASS strictly: all handoff requirements, acceptance criteria, and Definition of Done items must be covered; required validation must pass; no unresolved material finding or critical unverified check may remain. Otherwise require FAIL.
7. If review fails, run `herdr-flow fix --task <task-id>`. The controller must route the repair through the task's persisted policy: ordinary worker/fixer for the configured low-cost rounds, then the persistent escalation fixer after the threshold. Never choose or replace the repair session ad hoc.
8. Never copy the full review into a prompt; the controller references `review.md`.
