---
description: Fix current findings in the repair session selected by Herdr Flow policy
---

Use the current Herdr Flow task.

1. Read `state.json`, `handoff.md`, `review.md`, `final-review.md`, and `implementation.md` from `.ai/workflow/<task-id>/`.
2. Confirm from `state.json` that your role matches `repair.active_role` (`worker`, `fixer`, or `escalation_fixer`). Fix every applicable finding in the current worktree. Do not create another agent session and do not merely explain fixes.
3. Rerun relevant validation and original Definition of Done gates.
4. Update `implementation.md`, set `## Status` to `READY`, and run `herdr-flow finish --task <task-id>` yourself. The controller starts review automatically; an idle event is the fallback. Do not ask the user to run the next command.
5. Remain alive for another review cycle.
